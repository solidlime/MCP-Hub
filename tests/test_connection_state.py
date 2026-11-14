"""接続状態の可視化: ProxyManager のヘルス追跡と admin API への露出。"""

import asyncio
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from mcp_hub.main import create_app
from mcp_hub.proxy_manager import ProxyManager
from mcp_hub.state import app_state


class _MockProxy:
    def __init__(self, tools=None, error=None):
        self._tools = tools or []
        self._error = error

    async def list_tools(self):
        if self._error is not None:
            raise self._error
        return self._tools


def _tool(name):
    return SimpleNamespace(name=name, description="")


def _make_manager():
    mcp = SimpleNamespace(
        mount=lambda *args, **kwargs: None,
        local_provider=object(),
        providers=[],
    )
    return ProxyManager(cast(Any, mcp), cast(Any, {}))


def _wire(pm, name="srv", config=None, status="connected"):
    pm._server_configs[name] = config or {"url": "http://localhost:9999"}
    pm._status[name] = status


@pytest.fixture(autouse=True)
def _clear_history(monkeypatch):
    monkeypatch.setenv("MCP_HUB_LIST_TOOLS_RETRY_DELAY", "0")
    app_state.clear_health_history()
    yield
    app_state.clear_health_history()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as c:
        yield c


class TestConnectionTracking:
    def test_success_records_last_success_and_tool_count(self):
        pm = _make_manager()
        _wire(pm)
        proxy = _MockProxy([_tool("a"), _tool("b")])
        pm._proxies["srv"] = cast(Any, proxy)

        tools = asyncio.run(pm.list_tools_for_server("srv", cast(Any, proxy), cache_ttl=0))
        state = pm.get_connection_state("srv")

        assert len(tools) == 2
        assert state["tool_count"] == 2
        assert state["consecutive_failures"] == 0
        assert state["last_success_at"] is not None
        assert state["effective_state"] == "healthy"
        assert [h["event"] for h in state["health_history"]][:1] == ["ok"]

    def test_failure_records_reason_message_and_increments(self):
        pm = _make_manager()
        _wire(pm)
        proxy = _MockProxy(error=ConnectionError("boom"))
        pm._proxies["srv"] = cast(Any, proxy)

        with pytest.raises(ConnectionError):
            asyncio.run(pm.list_tools_for_server("srv", cast(Any, proxy), cache_ttl=0))
        state = pm.get_connection_state("srv")

        assert state["consecutive_failures"] == 1
        assert state["last_error"]["reason"] == "list_tools"
        assert "boom" in state["last_error"]["message"]
        assert state["last_error"]["at"] > 0

        with pytest.raises(ConnectionError):
            asyncio.run(pm.list_tools_for_server("srv", cast(Any, proxy), cache_ttl=0))
        assert pm.get_connection_state("srv")["consecutive_failures"] == 2

    def test_success_resets_consecutive_failures(self):
        pm = _make_manager()
        _wire(pm)
        pm._consecutive_failures["srv"] = 3
        proxy = _MockProxy([_tool("a")])
        pm._proxies["srv"] = cast(Any, proxy)

        asyncio.run(pm.list_tools_for_server("srv", cast(Any, proxy), cache_ttl=0))
        assert pm.get_connection_state("srv")["consecutive_failures"] == 0

    def test_effective_state_mapping_and_disabled_priority(self):
        pm = _make_manager()
        _wire(pm, "ok", status="connected")
        assert pm.get_connection_state("ok")["effective_state"] == "healthy"
        _wire(pm, "init", status="connecting")
        assert pm.get_connection_state("init")["effective_state"] == "initializing"
        _wire(pm, "rec", status="recovering")
        assert pm.get_connection_state("rec")["effective_state"] == "recovering"
        _wire(pm, "bad", status="error")
        assert pm.get_connection_state("bad")["effective_state"] == "error"
        _wire(pm, "off", config={"url": "http://x", "disabled": True}, status="connected")
        assert pm.get_connection_state("off")["effective_state"] == "disabled"

    def test_error_exposes_backoff_and_next_retry(self, monkeypatch):
        monkeypatch.setenv("MCP_HUB_RECOVERY_COOLDOWN", "60")
        pm = _make_manager()
        _wire(pm, "srv", status="error")
        pm._last_recovery_fail["srv"] = time.monotonic()

        state = pm.get_connection_state("srv")

        assert state["retry_backoff_seconds"] == 60
        assert state["retry_in_seconds"] is not None
        assert 0 <= state["retry_in_seconds"] <= 60
        assert state["next_retry_at"] is not None
        assert state["next_retry_at"] >= time.time()

    def test_health_history_is_bounded(self):
        pm = _make_manager()
        _wire(pm, "srv", status="error")
        for _ in range(30):
            pm._record_failure("srv", "health_check", "nope")

        history = pm.get_connection_state("srv")["health_history"]
        assert len(history) == 20
        assert history[0]["event"] == "failure"

    def test_catalog_counts_are_recorded(self):
        pm = _make_manager()
        _wire(pm)
        pm.record_catalog_counts("srv", resources=4, prompts=2)

        state = pm.get_connection_state("srv")
        assert state["resource_count"] == 4
        assert state["prompt_count"] == 2

    def test_servers_info_includes_connection_state(self):
        pm = _make_manager()
        _wire(pm)
        info = pm.get_servers_info()[0]
        assert info["effective_state"] == "healthy"
        assert "last_success_at" in info
        assert "consecutive_failures" in info


class TestAdminConnectionState:
    def test_servers_endpoint_exposes_connection_fields(self, client):
        client.post("/admin/api/servers", json={
            "name": "off", "config": {"url": "http://localhost:9999", "disabled": True},
        })
        r = client.get("/admin/api/servers")
        assert r.status_code == 200
        srv = r.json()["servers"][0]
        for key in (
            "effective_state", "last_success_at", "last_error",
            "consecutive_failures", "retry_backoff_seconds",
            "next_retry_at", "retry_in_seconds",
            "tool_count", "resource_count", "prompt_count", "health_history",
        ):
            assert key in srv, key
        assert srv["effective_state"] == "disabled"

    def test_servers_endpoint_exposes_error_details(self, client):
        client.post("/admin/api/servers", json={
            "name": "broken", "config": {"url": "http://localhost:9999", "disabled": True},
        })
        pm = app_state.proxy_manager
        assert pm is not None
        pm._status["broken"] = "error"
        pm._consecutive_failures["broken"] = 4
        pm._last_error["broken"] = {
            "reason": "list_tools", "message": "connection refused", "at": time.time(),
        }

        srv = client.get("/admin/api/servers").json()["servers"][0]

        assert srv["effective_state"] == "error"
        assert srv["consecutive_failures"] == 4
        assert srv["last_error"]["message"] == "connection refused"
        assert srv["next_retry_at"] is not None
