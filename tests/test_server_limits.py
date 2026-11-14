"""サーバー単位の timeout / 同時実行 / レート制限（#5）。

グローバル既定（env）に対するサーバー単位 `limits` の上書きを検証する。
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from fastmcp.exceptions import ToolError

from mcp_hub import config as hub_config
from mcp_hub import validators
from mcp_hub.main import create_app
from mcp_hub.middleware import LimitsMiddleware
from mcp_hub.proxy_manager import ProxyManager, RateLimitExceeded


def _make_manager(server_configs: dict | None = None) -> ProxyManager:
    mcp = SimpleNamespace(
        mount=lambda p, namespace=None: None,
        local_provider=object(),
        providers=[],
    )
    pm = ProxyManager(mcp, {})
    if server_configs:
        pm._server_configs.update(server_configs)
    return pm


class TestValidateLimits:
    def test_accepts_full_set(self):
        out = validators.validate_limits({
            "call_timeout": 5,
            "max_concurrent": 2,
            "rate_limit": 3,
            "rate_burst": 5,
            "rate_window": 2,
        })
        assert out == {
            "call_timeout": 5.0,
            "max_concurrent": 2,
            "rate_limit": 3.0,
            "rate_burst": 5,
            "rate_window": 2.0,
        }

    def test_empty_is_valid(self):
        assert validators.validate_limits({}) == {}

    @pytest.mark.parametrize("bad", [
        {"call_timeout": 0},
        {"call_timeout": -1},
        {"call_timeout": "abc"},
        {"max_concurrent": 0},
        {"max_concurrent": -3},
        {"max_concurrent": 2.5},
        {"rate_limit": 0},
        {"rate_burst": 0},
        {"rate_window": 0},
    ])
    def test_rejects_out_of_range(self, bad):
        with pytest.raises(validators.ValidationError):
            validators.validate_limits(bad)

    def test_rejects_unknown_key(self):
        with pytest.raises(validators.ValidationError):
            validators.validate_limits({"call_timout": 5})

    def test_rejects_non_dict(self):
        with pytest.raises(validators.ValidationError):
            validators.validate_limits(["nope"])


class TestServerLimitsResolution:
    def test_unset_limits_yields_none(self):
        pm = _make_manager({"s": {"url": "http://x"}})
        lim = pm.server_limits("s")
        assert lim["call_timeout"] is None
        assert lim["max_concurrent"] is None
        assert lim["rate_limit"] is None
        assert lim["rate_window"] == 1.0

    def test_reads_config_values(self):
        pm = _make_manager({"s": {"limits": {
            "call_timeout": 7, "max_concurrent": 3,
            "rate_limit": 10, "rate_burst": 4, "rate_window": 2,
        }}})
        lim = pm.server_limits("s")
        assert lim == {
            "call_timeout": 7.0,
            "max_concurrent": 3,
            "rate_limit": 10.0,
            "rate_burst": 4,
            "rate_window": 2.0,
        }

    def test_garbage_values_are_ignored(self):
        """手書き hub.config.json の壊れた値で落ちない。"""
        pm = _make_manager({"s": {"limits": {"call_timeout": "x", "max_concurrent": 0}}})
        lim = pm.server_limits("s")
        assert lim["call_timeout"] is None
        assert lim["max_concurrent"] is None

    def test_global_env_timeout_is_fallback(self, monkeypatch):
        monkeypatch.setenv("MCP_HUB_CALL_TOOL_TIMEOUT", "12")
        pm = _make_manager({"s": {"url": "http://x"}})
        assert pm.server_call_timeout("s") == 12.0

    def test_server_timeout_overrides_env(self, monkeypatch):
        monkeypatch.setenv("MCP_HUB_CALL_TOOL_TIMEOUT", "12")
        pm = _make_manager({"s": {"limits": {"call_timeout": 3}}})
        assert pm.server_call_timeout("s") == 3.0

    def test_env_zero_disables_timeout(self, monkeypatch):
        monkeypatch.setenv("MCP_HUB_CALL_TOOL_TIMEOUT", "0")
        pm = _make_manager({"s": {"url": "http://x"}})
        assert pm.server_call_timeout("s") is None


class TestConcurrencyAndRate:
    async def test_max_concurrent_caps_in_flight(self):
        pm = _make_manager({"s": {"limits": {"max_concurrent": 1}}})
        in_flight = 0
        peak = 0

        async def call():
            nonlocal in_flight, peak
            async with pm.call_slot("s", "t"):
                in_flight += 1
                peak = max(peak, in_flight)
                await asyncio.sleep(0.02)
                in_flight -= 1

        await asyncio.gather(call(), call(), call())
        assert peak == 1

    async def test_unlimited_when_not_configured(self):
        pm = _make_manager({"s": {}})
        in_flight = 0
        peak = 0

        async def call():
            nonlocal in_flight, peak
            async with pm.call_slot("s", "t"):
                in_flight += 1
                peak = max(peak, in_flight)
                await asyncio.sleep(0.02)
                in_flight -= 1

        await asyncio.gather(call(), call(), call())
        assert peak == 3

    async def test_rate_limit_rejects_when_wait_exceeds_window(self):
        pm = _make_manager({"s": {"limits": {
            "rate_limit": 1, "rate_burst": 1, "rate_window": 30,
        }}})
        async with pm.call_slot("s", "t"):
            pass
        with pytest.raises(RateLimitExceeded) as exc:
            async with pm.call_slot("s", "t"):
                pass
        assert "rate" in str(exc.value).lower()
        assert "s" in str(exc.value)

    async def test_rate_limit_waits_within_window(self):
        pm = _make_manager({"s": {"limits": {
            "rate_limit": 2, "rate_burst": 1, "rate_window": 0.2,
        }}})
        loop = asyncio.get_running_loop()
        start = loop.time()
        async with pm.call_slot("s", "t"):
            pass
        async with pm.call_slot("s", "t"):
            pass
        assert loop.time() - start >= 0.04  # 1/2 * 0.2s 待たされる

    async def test_burst_lets_spike_through(self):
        pm = _make_manager({"s": {"limits": {
            "rate_limit": 1, "rate_burst": 3, "rate_window": 60,
        }}})
        loop = asyncio.get_running_loop()
        start = loop.time()
        for _ in range(3):
            async with pm.call_slot("s", "t"):
                pass
        assert loop.time() - start < 0.05


class TestLimitsMiddleware:
    async def test_timeout_override_raises_clear_error(self):
        pm = _make_manager({"s": {"limits": {"call_timeout": 0.05}}})
        mw = LimitsMiddleware(pm)
        ctx = SimpleNamespace(message=SimpleNamespace(name="s_tool", arguments={}))

        async def slow_next(_ctx):
            await asyncio.sleep(0.5)
            return "done"

        with pytest.raises(ToolError) as exc:
            await mw.on_call_tool(ctx, slow_next)
        msg = str(exc.value)
        assert "timed out" in msg
        assert "0.05" in msg

    async def test_no_timeout_when_disabled(self, monkeypatch):
        monkeypatch.setenv("MCP_HUB_CALL_TOOL_TIMEOUT", "0")
        pm = _make_manager({"s": {}})
        mw = LimitsMiddleware(pm)
        ctx = SimpleNamespace(message=SimpleNamespace(name="s_tool", arguments={}))

        async def slow_next(_ctx):
            await asyncio.sleep(0.05)
            return "done"

        assert await mw.on_call_tool(ctx, slow_next) == "done"

    async def test_passes_through_result(self):
        pm = _make_manager({"s": {}})
        mw = LimitsMiddleware(pm)
        ctx = SimpleNamespace(message=SimpleNamespace(name="s_tool", arguments={}))

        async def ok(_ctx):
            return "ok"

        assert await mw.on_call_tool(ctx, ok) == "ok"

    async def test_meta_execute_tool_resolves_server(self):
        pm = _make_manager({"s": {"limits": {"call_timeout": 0.05}}})
        mw = LimitsMiddleware(pm)
        ctx = SimpleNamespace(message=SimpleNamespace(
            name="execute_tool", arguments={"server": "s", "tool_name": "t"},
        ))

        async def slow_next(_ctx):
            await asyncio.sleep(0.5)

        with pytest.raises(ToolError):
            await mw.on_call_tool(ctx, slow_next)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as c:
        c.data_dir = tmp_path
        yield c


class TestAdminLimitsApi:
    def test_register_persists_limits(self, client):
        r = client.post("/admin/api/servers", json={
            "name": "lim",
            "config": {
                "url": "http://localhost:9999",
                "disabled": True,
                "limits": {"call_timeout": 4, "max_concurrent": 2},
            },
        })
        assert r.status_code == 201, r.text
        listed = client.get("/admin/api/servers").json()["servers"]
        cfg = [s for s in listed if s["name"] == "lim"][0]["config"]
        assert cfg["limits"] == {"call_timeout": 4.0, "max_concurrent": 2}

        stored = json.loads((client.data_dir / "hub.config.json").read_text())
        assert stored["mcpServers"]["lim"]["limits"]["call_timeout"] == 4.0

    def test_register_rejects_invalid_limits(self, client):
        r = client.post("/admin/api/servers", json={
            "name": "lim",
            "config": {
                "url": "http://localhost:9999",
                "disabled": True,
                "limits": {"max_concurrent": 0},
            },
        })
        assert r.status_code == 422

    def test_patch_limits_endpoint_merges(self, client):
        client.post("/admin/api/servers", json={
            "name": "lim",
            "config": {"url": "http://localhost:9999", "disabled": True,
                       "limits": {"call_timeout": 4}},
        })
        r = client.patch("/admin/api/servers/lim/limits", json={"max_concurrent": 3})
        assert r.status_code == 200, r.text
        assert r.json()["limits"] == {"call_timeout": 4.0, "max_concurrent": 3}

        # 部分更新でも既存キーが消えない
        r = client.patch("/admin/api/servers/lim/limits", json={"call_timeout": None})
        assert r.status_code == 200
        assert r.json()["limits"] == {"max_concurrent": 3}

    def test_patch_limits_endpoint_validates(self, client):
        client.post("/admin/api/servers", json={
            "name": "lim",
            "config": {"url": "http://localhost:9999", "disabled": True},
        })
        r = client.patch("/admin/api/servers/lim/limits", json={"rate_window": -1})
        assert r.status_code == 422

    def test_patch_limits_unknown_server_404(self, client):
        r = client.patch("/admin/api/servers/nope/limits", json={"max_concurrent": 2})
        assert r.status_code == 404

    def test_clear_all_limits_removes_key(self, client):
        client.post("/admin/api/servers", json={
            "name": "lim",
            "config": {"url": "http://localhost:9999", "disabled": True,
                       "limits": {"call_timeout": 4}},
        })
        r = client.patch("/admin/api/servers/lim/limits", json={"call_timeout": None})
        assert r.status_code == 200
        assert "limits" not in r.json()["limits"] or r.json()["limits"] == {}
        stored = json.loads((client.data_dir / "hub.config.json").read_text())
        assert "limits" not in stored["mcpServers"]["lim"]


class TestConfigLoader:
    def test_load_config_keeps_limits(self, tmp_path):
        cfg = tmp_path / "hub.config.json"
        cfg.write_text(json.dumps({
            "version": 1,
            "mcpServers": {"s": {"url": "http://x", "limits": {"max_concurrent": 2}}},
        }))
        loaded = hub_config.load_config(str(cfg))
        assert loaded.servers["s"]["limits"]["max_concurrent"] == 2

    def test_load_config_drops_invalid_limits(self, tmp_path):
        cfg = tmp_path / "hub.config.json"
        cfg.write_text(json.dumps({
            "version": 1,
            "mcpServers": {"s": {"url": "http://x", "limits": {"max_concurrent": 0}}},
        }))
        loaded = hub_config.load_config(str(cfg))
        assert "limits" not in loaded.servers["s"]
