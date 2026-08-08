"""_create_proxy URL mode: env → Authorization: Bearer header derivation tests."""
import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

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


class TestRenameServer:
    def _manager_with_server(self):
        pm = _make_manager()
        proxy = _MockProxy("old")
        pm._proxies = {"old": proxy}
        pm._server_configs = {"old": {"url": "http://x"}}
        pm._status = {"old": "connected"}
        pm._tool_counts = {"old": 3}
        pm._tool_cache = {"old": (1.0, ["t"])}
        return pm, proxy

    def test_rename_rekeys_state_and_reuses_proxy(self):
        pm, proxy = self._manager_with_server()
        rebuild_called = []

        async def fake_rebuild():
            rebuild_called.append(True)

        pm._rebuild_mounts = fake_rebuild
        notified = []

        async def fake_notify(name, event, detail):
            notified.append((name, event, detail))

        pm._notify_change = fake_notify

        async def run():
            await pm.rename_server("old", "new", {"url": "http://x"})

        asyncio.run(run())

        # 同一プロキシインスタンスを再利用（接続維持）
        assert "old" not in pm._proxies
        assert pm._proxies["new"] is proxy
        assert "old" not in pm._server_configs
        assert pm._server_configs["new"] == {"url": "http://x"}
        assert pm._status["new"] == "connected"
        assert pm._tool_counts["new"] == 3
        assert pm._tool_cache["new"] == (1.0, ["t"])
        assert rebuild_called == [True]
        assert notified == [("new", "renamed", {"old_name": "old"})]
        assert "old" not in pm._refreshing
        assert "new" not in pm._refreshing

    def test_rename_collision_raises_value_error(self):
        pm, proxy = self._manager_with_server()
        pm._server_configs["new"] = {"url": "http://other"}

        async def run():
            with pytest.raises(ValueError):
                await pm.rename_server("old", "new", {"url": "http://x"})

        asyncio.run(run())

        # 状態は変わらず、_refreshing の後始末も完了している
        assert pm._proxies["old"] is proxy
        assert "old" not in pm._refreshing
        assert "new" not in pm._refreshing

    def test_rename_server_without_proxy_skips_rebuild(self):
        pm = _make_manager()
        pm._server_configs = {"old": {"disabled": True}}
        pm._status = {"old": "disabled"}
        rebuild_called = []

        async def fake_rebuild():
            rebuild_called.append(True)

        pm._rebuild_mounts = fake_rebuild

        async def run():
            await pm.rename_server("old", "new", {"disabled": True})

        asyncio.run(run())

        assert "old" not in pm._server_configs
        assert pm._server_configs["new"] == {"disabled": True}
        assert pm._status["new"] == "disabled"
        assert rebuild_called == []


class TestConnectMountRaceGuard:
    """リネーム/削除後の接続完了がゾンビ旧名を復活させないこと。"""

    def test_connect_and_mount_skips_when_name_gone(self):
        pm = _make_manager()
        pm._server_configs = {}  # リネーム/削除済み

        mounted = []

        async def fake_mount(p, namespace=None):
            mounted.append(namespace)

        pm.mcp.mount = fake_mount

        async def run():
            await pm._connect_and_mount("old", {"url": "http://x"})

        asyncio.run(run())

        assert mounted == []
        assert "old" not in pm._proxies
        assert pm._status.get("old") != "error"  # エラー扱いにしない

    def test_refresh_server_skips_mount_when_name_gone(self):
        pm = _make_manager()
        mounted = []

        async def fake_mount(p, namespace=None):
            mounted.append(namespace)

        pm.mcp.mount = fake_mount

        def fake_create_proxy(*args, name=None, **kwargs):
            # Phase 1 と Phase 2 の間にリネーム/削除された状態を再現
            pm._server_configs.pop(name, None)
            return _MockProxy("m")

        with patch("mcp_hub.proxy_manager.create_proxy", side_effect=fake_create_proxy):
            async def run():
                await pm.refresh_server("old", {"url": "http://x"})

            asyncio.run(run())

        assert mounted == []
        assert "old" not in pm._proxies
        assert "old" not in pm._server_configs
        assert "old" not in pm._refreshing
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
