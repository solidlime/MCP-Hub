"""ツール結果キャッシュ（任意機能）のテスト。

対象: tool_cache.ToolResultCache, ToolLogMiddleware 経由のヒット/ミス、
管理API（設定/クリア/メトリクス）、サーバー再接続時の無効化。
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from mcp_hub.state import app_state
from mcp_hub.tool_cache import ToolResultCache, normalize_config


# --- helpers ---------------------------------------------------------------


class _Result:
    """ToolResult 互換の最小オブジェクト（is_error/content のみ）。"""

    def __init__(self, text: str = "ok", is_error: bool = False):
        self.is_error = is_error
        self.content = [type("C", (), {"type": "text", "text": text})()]
        self.structured_content = None
        self.meta = None

    def model_copy(self, deep: bool = False):
        return _Result(
            text=self.content[0].text, is_error=self.is_error
        )


class _DummyContext:
    def __init__(self, name, arguments=None):
        self.message = type("Msg", (), {"name": name, "arguments": arguments or {}})()


def _pm(servers=("fetch",)):
    return type(
        "PM", (), {"get_connected_servers": lambda self: {s: object() for s in servers}}
    )()


@pytest.fixture(autouse=True)
def _reset_cache():
    app_state.tool_cache = ToolResultCache()
    app_state.clear_logs()
    yield
    app_state.tool_cache = ToolResultCache()
    app_state.clear_logs()


# --- config ----------------------------------------------------------------


class TestConfig:
    def test_defaults_are_disabled(self):
        cfg = normalize_config(None)
        assert cfg["enabled"] is False
        assert cfg["ttl"] == 60.0
        assert cfg["max_entries"] == 500
        assert cfg["allow"] == [] and cfg["deny"] == []

    def test_partial_merge(self):
        cfg = normalize_config({"enabled": True, "ttl": 5})
        assert cfg["enabled"] is True
        assert cfg["ttl"] == 5.0
        assert cfg["max_entries"] == 500

    @pytest.mark.parametrize(
        "raw",
        [
            {"enabled": "yes"},
            {"ttl": 0},
            {"ttl": -1},
            {"ttl": 999999},
            {"max_entries": 0},
            {"max_entries": "10"},
            {"max_bytes": -1},
            {"allow": "fetch_*"},
            {"allow": [1]},
            {"deny": [""]},
        ],
    )
    def test_invalid_raises(self, raw):
        with pytest.raises(ValueError):
            normalize_config(raw)


class TestCacheability:
    def test_disabled_by_default(self):
        c = ToolResultCache()
        assert c.is_cacheable("fetch", "fetch") is False

    def test_enabled_without_allow_caches_all(self):
        c = ToolResultCache({"enabled": True})
        assert c.is_cacheable("fetch", "fetch") is True

    def test_allow_list_is_opt_in(self):
        c = ToolResultCache({"enabled": True, "allow": ["fetch_fetch", "search_*"]})
        assert c.is_cacheable("fetch", "fetch") is True
        assert c.is_cacheable("search", "query") is True
        assert c.is_cacheable("fs", "read") is False

    def test_deny_wins(self):
        c = ToolResultCache(
            {"enabled": True, "allow": ["fetch_*"], "deny": ["fetch_write"]}
        )
        assert c.is_cacheable("fetch", "read") is True
        assert c.is_cacheable("fetch", "write") is False

    def test_deny_whole_server(self):
        c = ToolResultCache({"enabled": True, "deny": ["browser"]})
        assert c.is_cacheable("browser", "click") is False


class TestKey:
    def test_argument_order_is_canonical(self):
        c = ToolResultCache({"enabled": True})
        k1 = c.make_key("s", "t", {"a": 1, "b": 2})
        k2 = c.make_key("s", "t", {"b": 2, "a": 1})
        assert k1 == k2

    def test_different_server_or_tool_differs(self):
        c = ToolResultCache({"enabled": True})
        assert c.make_key("s1", "t", {"a": 1}) != c.make_key("s2", "t", {"a": 1})
        assert c.make_key("s", "t1", {"a": 1}) != c.make_key("s", "t2", {"a": 1})


class TestCacheStore:
    def test_put_get_and_stats(self):
        c = ToolResultCache({"enabled": True})
        key = c.make_key("s", "t", {"a": 1})
        assert c.get(key) is None
        c.put(key, _Result("v1"))
        assert c.get(key).content[0].text == "v1"
        assert c.stats()["hits"] == 1
        assert c.stats()["misses"] == 1
        assert c.stats()["entries"] == 1

    def test_ttl_expiry(self):
        clock = [100.0]
        c = ToolResultCache({"enabled": True, "ttl": 10}, clock=lambda: clock[0])
        key = c.make_key("s", "t", {})
        c.put(key, _Result())
        clock[0] += 9.9
        assert c.get(key) is not None
        clock[0] += 0.2
        assert c.get(key) is None
        assert c.stats()["entries"] == 0

    def test_max_entries_evicts_oldest(self):
        c = ToolResultCache({"enabled": True, "max_entries": 2})
        for i in range(3):
            c.put(c.make_key("s", "t", {"i": i}), _Result(str(i)))
        assert c.stats()["entries"] == 2
        assert c.stats()["evictions"] == 1

    def test_get_refreshes_lru_order(self):
        c = ToolResultCache({"enabled": True, "max_entries": 2})
        k0 = c.make_key("s", "t", {"i": 0})
        k1 = c.make_key("s", "t", {"i": 1})
        c.put(k0, _Result("0"))
        c.put(k1, _Result("1"))
        assert c.get(k0) is not None  # k0 が最近使用に
        c.put(c.make_key("s", "t", {"i": 2}), _Result("2"))
        assert c.get(k0) is not None
        assert c.get(k1) is None  # k1 が追い出される

    def test_invalidate_server(self):
        c = ToolResultCache({"enabled": True})
        c.put(c.make_key("a", "t", {}), _Result())
        c.put(c.make_key("b", "t", {}), _Result())
        assert c.invalidate_server("a") == 1
        assert c.stats()["entries"] == 1

    def test_reconfigure_disable_clears(self):
        c = ToolResultCache({"enabled": True})
        c.put(c.make_key("s", "t", {}), _Result())
        c.configure({"enabled": False})
        assert c.stats()["entries"] == 0
        assert c.is_cacheable("s", "t") is False


# --- middleware integration ------------------------------------------------


class TestMiddlewareCache:
    def _mw(self):
        from mcp_hub.middleware import ToolLogMiddleware

        app_state.tool_cache = ToolResultCache({"enabled": True})
        return ToolLogMiddleware(_pm())

    def test_second_call_is_cache_hit(self):
        mw = self._mw()
        calls = []

        async def call_next(ctx):
            calls.append(ctx.message.arguments)
            return _Result("payload")

        args = {"url": "https://example.com"}
        ctx = _DummyContext("fetch_fetch", args)
        r1 = asyncio.run(mw.on_call_tool(ctx, call_next))
        r2 = asyncio.run(mw.on_call_tool(ctx, call_next))
        assert len(calls) == 1
        assert r1.content[0].text == r2.content[0].text == "payload"
        logs = [e for e in app_state.snapshot_logs() if e.status == "cache_hit"]
        assert len(logs) == 1 and logs[0].tool == "fetch_fetch"
        assert app_state.tool_cache.stats()["hits"] == 1

    def test_meta_execute_tool_is_cached_by_inner_tool(self):
        mw = self._mw()
        calls = []

        async def call_next(ctx):
            calls.append(ctx.message.arguments)
            return _Result("payload")

        ctx = _DummyContext(
            "execute_tool",
            {"server": "fetch", "tool_name": "fetch", "arguments": {"url": "x"}},
        )
        ctx2 = _DummyContext(
            "execute_tool",
            {"server": "fetch", "tool_name": "fetch", "arguments": {"url": "x"}},
        )
        asyncio.run(mw.on_call_tool(ctx, call_next))
        asyncio.run(mw.on_call_tool(ctx2, call_next))
        assert len(calls) == 1

    def test_error_results_are_not_cached(self):
        mw = self._mw()
        calls = []

        async def call_next(ctx):
            calls.append(1)
            return _Result('{"error": "Tool not found"}')

        ctx = _DummyContext(
            "execute_tool", {"server": "fetch", "tool_name": "missing", "arguments": {}}
        )
        asyncio.run(mw.on_call_tool(ctx, call_next))
        asyncio.run(mw.on_call_tool(ctx, call_next))
        assert len(calls) == 2
        assert app_state.tool_cache.stats()["entries"] == 0

    def test_is_error_results_are_not_cached(self):
        mw = self._mw()
        calls = []

        async def call_next(ctx):
            calls.append(1)
            return _Result("boom", is_error=True)

        ctx = _DummyContext("fetch_fetch", {})
        asyncio.run(mw.on_call_tool(ctx, call_next))
        asyncio.run(mw.on_call_tool(ctx, call_next))
        assert len(calls) == 2

    def test_exceptions_are_not_cached(self):
        mw = self._mw()
        calls = []

        async def call_next(ctx):
            calls.append(1)
            raise RuntimeError("boom")

        ctx = _DummyContext("fetch_fetch", {})
        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio.run(mw.on_call_tool(ctx, call_next))
        assert len(calls) == 2
        assert app_state.tool_cache.stats()["entries"] == 0

    def test_denied_tool_bypasses_cache(self):
        mw = self._mw()
        app_state.tool_cache.configure({"enabled": True, "deny": ["fetch_*"]})
        calls = []

        async def call_next(ctx):
            calls.append(1)
            return _Result()

        ctx = _DummyContext("fetch_fetch", {})
        asyncio.run(mw.on_call_tool(ctx, call_next))
        asyncio.run(mw.on_call_tool(ctx, call_next))
        assert len(calls) == 2


# --- proxy manager invalidation --------------------------------------------


class TestInvalidation:
    def test_unregister_drops_cache_entries(self, tmp_path, monkeypatch):
        from mcp_hub.proxy_manager import ProxyManager
        from mcp_hub.store import JsonStore

        async def run():
            store = JsonStore(str(tmp_path))
            await store.init()
            pm = ProxyManager.__new__(ProxyManager)
            from fastmcp import FastMCP

            ProxyManager.__init__(pm, FastMCP("t"), store)
            app_state.tool_cache.put(
                app_state.tool_cache.make_key("gone", "t", {}), _Result()
            )
            await store.add_server("gone", {"url": "http://x"})
            pm._server_configs["gone"] = {"url": "http://x"}
            assert await pm.unregister_server("gone") is True
            assert app_state.tool_cache.stats()["entries"] == 0

        asyncio.run(run())


# --- admin API --------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    from mcp_hub.main import create_app

    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as c:
        yield c


class TestAdminAPI:
    def test_settings_exposes_tool_cache(self, client):
        r = client.get("/admin/api/settings")
        assert r.status_code == 200
        tc = r.json()["tool_cache"]
        assert tc["enabled"] is False
        assert "stats" in tc

    def test_patch_tool_cache_persists_and_applies(self, client):
        r = client.patch(
            "/admin/api/settings",
            json={"tool_cache": {"enabled": True, "ttl": 30, "allow": ["fetch_*"]}},
        )
        assert r.status_code == 200
        tc = r.json()["tool_cache"]
        assert tc["enabled"] is True
        assert tc["ttl"] == 30.0
        assert tc["allow"] == ["fetch_*"]
        # 実効キャッシュにも反映
        assert app_state.tool_cache.enabled is True
        assert app_state.tool_cache.is_cacheable("fetch", "read") is True
        # 永続化
        r2 = client.get("/admin/api/settings")
        assert r2.json()["tool_cache"]["ttl"] == 30.0

    def test_patch_tool_cache_invalid_is_422(self, client):
        r = client.patch(
            "/admin/api/settings", json={"tool_cache": {"ttl": -1}}
        )
        assert r.status_code == 422

    def test_clear_cache(self, client):
        client.patch("/admin/api/settings", json={"tool_cache": {"enabled": True}})
        app_state.tool_cache.put(
            app_state.tool_cache.make_key("s", "t", {}), _Result()
        )
        r = client.delete("/admin/api/cache")
        assert r.status_code == 200
        assert r.json()["cleared"] >= 1
        assert app_state.tool_cache.stats()["entries"] == 0

    def test_metrics_include_cache_stats(self, client):
        r = client.get("/admin/api/metrics")
        assert r.status_code == 200
        assert "tool_cache" in r.json()
        assert {"hits", "misses", "evictions", "entries"} <= set(
            r.json()["tool_cache"]
        )
