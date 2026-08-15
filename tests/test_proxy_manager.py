"""_create_proxy URL mode: env → Authorization: Bearer header derivation tests."""
import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from mcp_hub.proxy_manager import ProxyManager


class _MockProxy:
    def __init__(self, name="mock"):
        self.name = name

    async def list_tools(self):
        return []


class _MockProxyFactory:
    """Callable stand-in for FastMCPProxy: records call args, returns a mock proxy."""

    def __init__(self, name="mock"):
        self.name = name
        self.call_args = None

    def __call__(self, *args, **kwargs):
        self.call_args = (args, kwargs)
        return _MockProxy(self.name)


def _make_manager():
    mcp = type("MCP", (), {"mount": lambda self, p, namespace=None: None})()
    return ProxyManager(mcp, {})


class TestCreateProxyUrlEnv:
    def test_unique_token_env_derives_bearer_header(self):
        pm = _make_manager()
        factory = _MockProxyFactory("m")
        with patch("mcp_hub.proxy_manager.FastMCPProxy", factory), \
             patch("mcp_hub.proxy_manager.Client", autospec=True) as client_cls:
            proxy, _ = asyncio.run(pm._create_proxy("map", {
                "url": "https://example.com/mcp",
                "env": {"MAPBOX_ACCESS_TOKEN": "abc"},
            }))
        client_cls.assert_called_once()
        transport = client_cls.call_args.kwargs["transport"]
        assert transport.headers["Authorization"] == "Bearer abc"
        assert proxy.name == "m"

    def test_explicit_headers_take_precedence(self):
        pm = _make_manager()
        with patch("mcp_hub.proxy_manager.FastMCPProxy", _MockProxyFactory("m")), \
             patch("mcp_hub.proxy_manager.Client", autospec=True) as client_cls:
            asyncio.run(pm._create_proxy("map", {
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer explicit"},
                "env": {"MAPBOX_ACCESS_TOKEN": "abc"},
            }))
        transport = client_cls.call_args.kwargs["transport"]
        assert transport.headers["Authorization"] == "Bearer explicit"

    def test_ambiguous_env_no_bearer_header_derived(self):
        pm = _make_manager()
        factory = _MockProxyFactory("m")
        with patch("mcp_hub.proxy_manager.FastMCPProxy", factory), \
             patch("mcp_hub.proxy_manager.Client", autospec=True) as client_cls:
            asyncio.run(pm._create_proxy("map", {
                "url": "https://example.com/mcp",
                "env": {"TOKEN": "t", "SECRET": "s"},
            }))
        # 曖昧な env からは Authorization を導出しない → headers なしの素の Client
        client_cls.assert_called_once()
        transport = client_cls.call_args.kwargs["transport"]
        assert not transport.headers
        assert factory.call_args is not None


class TestRenameServer:
    def _manager_with_server(self):
        pm = _make_manager()
        proxy = _MockProxy("old")
        pm._proxies = {"old": proxy}
        pm._clients = {"old": AsyncMock()}  # 接続済み client
        pm._server_configs = {"old": {"url": "http://x"}}
        pm._status = {"old": "connected"}
        pm._tool_counts = {"old": 3}
        pm._tool_cache = {"old": (1.0, ["t"])}
        return pm, proxy

    def test_rename_rekeys_state_and_reuses_proxy(self):
        pm, proxy = self._manager_with_server()
        old_client = pm._clients["old"]
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
        # 接続済み client も rekey（接続維持）
        assert "old" not in pm._clients
        assert pm._clients["new"] is old_client
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

        async def fake_create_proxy(name, config):
            return _MockProxy(name), AsyncMock()

        with patch.object(pm, "_create_proxy", side_effect=fake_create_proxy):
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

        async def fake_create_proxy(name, config):
            # Phase 1 と Phase 2 の間にリネーム/削除された状態を再現
            pm._server_configs.pop(name, None)
            return _MockProxy("m"), AsyncMock()

        with patch.object(pm, "_create_proxy", side_effect=fake_create_proxy):
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




class TestConnectedClientKept:
    """_create_proxy が接続済み Client を client_factory に渡し、_clients に保持すること。"""

    def test_url_server_connects_and_keeps_client(self):
        pm = _make_manager()
        factory = _MockProxyFactory("m")
        with patch("mcp_hub.proxy_manager.FastMCPProxy", factory), \
             patch("mcp_hub.proxy_manager.Client", autospec=True) as client_cls:
            proxy, _client = asyncio.run(pm._create_proxy("map", {
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer x"},
            }))
        client_cls.return_value.__aenter__.assert_awaited_once()
        assert factory.call_args is not None
        kwargs = factory.call_args[1]
        assert kwargs["name"] == "map"
        # client_factory は error チェック付き（healthy なら接続済み client を返す）
        assert kwargs["client_factory"]() is client_cls.return_value
        assert pm._clients["map"] is client_cls.return_value
        assert proxy.name == "m"

    def test_stdio_server_connects_and_keeps_client(self):
        pm = _make_manager()
        factory = _MockProxyFactory("m")
        with patch("mcp_hub.proxy_manager.FastMCPProxy", factory), \
             patch("mcp_hub.proxy_manager.Client", autospec=True) as client_cls:
            asyncio.run(pm._create_proxy("srv", {
                "command": "uvx", "args": ["mcp-server"],
            }))
        client_cls.return_value.__aenter__.assert_awaited_once()
        assert factory.call_args is not None
        assert factory.call_args[1]["name"] == "srv"
        assert factory.call_args[1]["client_factory"]() is client_cls.return_value
        assert pm._clients["srv"] is client_cls.return_value


class TestClientCleanup:
    """proxy 破棄時に接続済み client が close され、_clients から消えること。"""

    def test_unregister_closes_and_removes_client(self):
        pm = _make_manager()
        pm.registry = SimpleNamespace(remove_server=AsyncMock(return_value=True))
        client = AsyncMock()
        pm._proxies = {"srv": _MockProxy("srv")}
        pm._clients = {"srv": client}
        pm._server_configs = {"srv": {"url": "http://x"}}
        pm._status = {"srv": "connected"}
        pm._tool_counts = {"srv": 1}
        pm._rebuild_mounts = AsyncMock()
        pm._notify_change = AsyncMock()

        assert asyncio.run(pm.unregister_server("srv")) is True

        client.close.assert_awaited_once()
        assert "srv" not in pm._clients

    def test_refresh_closes_old_client(self):
        pm = _make_manager()
        old_client = AsyncMock()
        new_client = AsyncMock()
        pm._proxies = {"srv": _MockProxy("srv")}
        pm._clients = {"srv": old_client}
        pm._server_configs = {"srv": {"url": "http://x"}}
        pm._status = {"srv": "connected"}
        pm._rebuild_mounts = AsyncMock()
        pm._notify_change = AsyncMock()

        async def fake_create_proxy(name, config):
            pm._clients[name] = new_client  # 実装と同じく新 client を登録
            return _MockProxy("srv"), new_client

        with patch.object(pm, "_create_proxy", side_effect=fake_create_proxy):
            asyncio.run(pm.refresh_server("srv", {"url": "http://x"}))

        old_client.close.assert_awaited_once()
        assert pm._clients["srv"] is new_client
        assert pm._proxies["srv"].name == "srv"

    def test_close_all_closes_every_client(self):
        pm = _make_manager()
        c1, c2 = AsyncMock(), AsyncMock()
        pm._clients = {"a": c1, "b": c2}

        asyncio.run(pm.close_all())

        c1.close.assert_awaited_once()
        c2.close.assert_awaited_once()
        assert pm._clients == {}

    def test_connect_and_mount_failure_closes_client(self):
        pm = _make_manager()
        pm._server_configs = {"srv": {"url": "http://x"}}
        client = AsyncMock()
        pm._clients = {"srv": client}
        pm._notify_change = AsyncMock()

        class _BrokenProxy:
            async def list_tools(self):
                raise RuntimeError("boom")

        async def fake_create_proxy(name, config):
            return _BrokenProxy(), client

        with patch.object(pm, "_create_proxy", side_effect=fake_create_proxy):
            asyncio.run(pm._connect_and_mount("srv", {"url": "http://x"}))

        client.close.assert_awaited_once()
        assert "srv" not in pm._clients

    def test_connect_and_mount_failure_keeps_others_client(self):
        """list_tools 検証中に別フローが _clients[name] を差し替えた場合、
        自分が作った client は close せず、差し替え後の client も維持する。"""
        pm = _make_manager()
        pm._server_configs = {"srv": {"url": "http://x"}}
        my_client = AsyncMock()
        other_client = AsyncMock()
        pm._clients = {"srv": other_client}  # 別フローが差し替え済み
        pm._notify_change = AsyncMock()

        class _BrokenProxy:
            async def list_tools(self):
                raise RuntimeError("boom")

        async def fake_create_proxy(name, config):
            return _BrokenProxy(), my_client

        with patch.object(pm, "_create_proxy", side_effect=fake_create_proxy):
            asyncio.run(pm._connect_and_mount("srv", {"url": "http://x"}))

        my_client.close.assert_not_awaited()
        other_client.close.assert_not_awaited()
        assert pm._clients["srv"] is other_client  # 差し替え後の client を維持

    def test_create_proxy_failure_closes_client(self):
        pm = _make_manager()
        with patch("mcp_hub.proxy_manager.FastMCPProxy",
                   side_effect=RuntimeError("boom")), \
             patch("mcp_hub.proxy_manager.Client", autospec=True) as client_cls:
            with pytest.raises(RuntimeError):
                asyncio.run(pm._create_proxy("srv", {"url": "http://x"}))
        client_cls.return_value.__aenter__.assert_awaited_once()
        client_cls.return_value.close.assert_awaited_once()
        assert "srv" not in pm._clients


class TestClientFactoryErrorGuard:
    """P1: status=error のサーバーでは client_factory が即時 raise する。

    FastMCPProxy._get_client() はリクエストごとに client_factory() を呼ぶ。
    error 状態で raise することで、死んだサーバーがリモート read_timeout
    でブロックするのを防ぐ（aggregate provider が即スキップする）。
    """

    def test_client_factory_raises_for_error_server(self):
        pm = _make_manager()
        pm._status = {"dead": "error"}
        factory = pm._make_client_factory("dead", AsyncMock())
        with pytest.raises(RuntimeError):
            factory()

    def test_client_factory_returns_client_for_healthy_server(self):
        pm = _make_manager()
        pm._status = {"alive": "connected"}
        client = AsyncMock()
        factory = pm._make_client_factory("alive", client)
        assert factory() is client


class TestListToolsFailureEscalation:
    """P2: list_tools の連続失敗で status=error に昇格、成功でカウンタリセット。"""

    def _manager_with_server(self, name="srv"):
        pm = _make_manager()
        pm._proxies = {name: _MockProxy(name)}
        pm._server_configs = {name: {"url": "http://x"}}
        pm._status = {name: "connected"}
        pm._notify_change = AsyncMock()
        return pm

    def test_consecutive_failures_mark_error_after_threshold(self):
        pm = self._manager_with_server()

        async def failing(name, proxy):
            raise RuntimeError("boom")

        pm.list_tools_for_server = failing
        with patch.dict(os.environ, {"MCP_HUB_HEALTH_MAX_FAILURES": "3"}):
            asyncio.run(pm.list_tools())
            assert pm._status["srv"] == "connected"  # 1回目: error 化しない
            asyncio.run(pm.list_tools())
            assert pm._status["srv"] == "connected"  # 2回目: まだ
            asyncio.run(pm.list_tools())
            assert pm._status["srv"] == "error"  # 3回目: error に昇格
        pm._notify_change.assert_awaited_with(
            "srv", "disconnected", {"error": "list_tools failed"}
        )

    def test_success_resets_failure_counter(self):
        pm = self._manager_with_server()
        pm._health_failures["srv"] = 1  # 失敗するとカウント2

        async def failing(name, proxy):
            raise RuntimeError("boom")

        async def succeeding(name, proxy):
            return [SimpleNamespace(name="t", description="d")]

        pm.list_tools_for_server = failing
        with patch.dict(os.environ, {"MCP_HUB_HEALTH_MAX_FAILURES": "3"}):
            asyncio.run(pm.list_tools())  # 失敗 → カウント2（error 化しない）
        assert pm._status["srv"] == "connected"
        assert pm._health_failures["srv"] == 2

        pm.list_tools_for_server = succeeding
        asyncio.run(pm.list_tools())  # 成功 → カウンタリセット
        assert "srv" not in pm._health_failures

        pm.list_tools_for_server = failing
        with patch.dict(os.environ, {"MCP_HUB_HEALTH_MAX_FAILURES": "3"}):
            asyncio.run(pm.list_tools())  # 失敗 → カウント1
            asyncio.run(pm.list_tools())  # 失敗 → カウント2
        assert pm._status["srv"] == "connected"  # リセット済みなので error 化しない