"""_create_proxy URL mode: env → Authorization: Bearer header derivation tests."""
import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

from mcp_hub.proxy_manager import ProxyManager


class _MockProxy:
    def __init__(self, name="mock"):
        self.name = name


def _make_manager():
    mcp = type("MCP", (), {"mount": lambda self, p, namespace=None: None})()
    return ProxyManager(mcp, {})


class TestCreateProxyUrlEnv:
    def test_unique_token_env_derives_bearer_header(self):
        pm = _make_manager()
        with patch("mcp_hub.proxy_manager.create_proxy", return_value=_MockProxy("m")), \
             patch("mcp_hub.proxy_manager.Client") as client_cls:
            proxy = pm._create_proxy("map", {
                "url": "https://example.com/mcp",
                "env": {"MAPBOX_ACCESS_TOKEN": "abc"},
            })
        client_cls.assert_called_once()
        transport = client_cls.call_args.kwargs["transport"]
        assert transport.headers["Authorization"] == "Bearer abc"
        assert proxy.name == "m"

    def test_explicit_headers_take_precedence(self):
        pm = _make_manager()
        with patch("mcp_hub.proxy_manager.create_proxy", return_value=_MockProxy("m")), \
             patch("mcp_hub.proxy_manager.Client") as client_cls:
            pm._create_proxy("map", {
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer explicit"},
                "env": {"MAPBOX_ACCESS_TOKEN": "abc"},
            })
        transport = client_cls.call_args.kwargs["transport"]
        assert transport.headers["Authorization"] == "Bearer explicit"

    def test_ambiguous_env_falls_back_to_url_proxy(self):
        pm = _make_manager()
        with patch("mcp_hub.proxy_manager.create_proxy", return_value=_MockProxy("m")) as cp, \
             patch("mcp_hub.proxy_manager.Client") as client_cls:
            pm._create_proxy("map", {
                "url": "https://example.com/mcp",
                "env": {"TOKEN": "t", "SECRET": "s"},
            })
        cp.assert_called_once_with("https://example.com/mcp", name="map")
        client_cls.assert_not_called()


class TestListTools:
    """list_tools() must not hang the whole /admin/api/servers response.

    Error-state servers are skipped (no proxy.list_tools() call), and a
    single hanging server must not block results for the others.
    """

    def _manager_with_servers(self, names):
        pm = _make_manager()
        pm._proxies = {n: _MockProxy(n) for n in names}
        pm._server_configs = {n: {} for n in names}
        pm._status = {n: "connected" for n in names}
        return pm

    def test_error_server_skipped_no_cache(self):
        pm = self._manager_with_servers(["dead", "alive"])
        pm._status["dead"] = "error"
        called = []

        async def fake_list_tools(name, proxy):
            called.append(name)
            return []

        pm.list_tools_for_server = fake_list_tools
        result = asyncio.run(pm.list_tools())

        # proxy.list_tools() must never be invoked for the error server
        assert called == ["alive"]
        assert result["dead"] == []
        assert "alive" in result

    def test_error_server_serves_fresh_cache(self):
        pm = self._manager_with_servers(["dead"])
        pm._status["dead"] = "error"
        tool = SimpleNamespace(name="t1", description="d1")
        pm._tool_cache["dead"] = (time.monotonic(), [tool])

        async def fake_list_tools(name, proxy):
            raise AssertionError("proxy.list_tools must not be called for error servers")

        pm.list_tools_for_server = fake_list_tools
        result = asyncio.run(pm.list_tools())

        assert result["dead"] == [{"name": "t1", "description": "d1"}]

    def test_hanging_server_times_out_others_returned(self):
        pm = self._manager_with_servers(["slow", "fast"])
        tool = SimpleNamespace(name="ok", description=None)

        async def fake_list_tools(name, proxy):
            if name == "slow":
                await asyncio.sleep(5)  # simulate unresponsive upstream
            return [tool]

        pm.list_tools_for_server = fake_list_tools
        with patch.dict(os.environ, {"MCP_HUB_LIST_TOOLS_TIMEOUT": "0.1"}):
            result = asyncio.run(pm.list_tools())

        assert result["slow"] == []
        assert result["fast"] == [{"name": "ok", "description": ""}]
