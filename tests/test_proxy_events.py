"""ProxyManager on_change event tests — success & failure firing."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from mcp_hub.proxy_manager import ProxyManager
from mcp_hub.state import app_state


@pytest.fixture(autouse=True)
def clean_buffer():
    app_state.clear_logs()
    yield
    app_state.clear_logs()


@pytest.fixture
def pm():
    mcp = type("MCP", (), {"mount": lambda self, p, namespace=None: None})()
    registry = AsyncMock()
    registry.list_servers = AsyncMock(return_value=[])
    mgr = ProxyManager(mcp, registry)
    return mgr


class TestOnChangeSignature:
    def test_callback_receives_name_event_detail(self, pm):
        received = []

        def cb(name, event, detail=None):
            received.append((name, event, detail))

        pm.on_change(cb)

        # 直接 _notify_change を呼んで検証
        asyncio.run(pm._notify_change("fetch", "connected", {"tool_count": 3}))
        assert received == [("fetch", "connected", {"tool_count": 3})]

    def test_callback_exception_is_swallowed(self, pm):
        def bad_cb(name, event, detail=None):
            raise RuntimeError("boom")

        pm.on_change(bad_cb)
        # 例外が伝播せず終了する
        asyncio.run(pm._notify_change("fetch", "spawn_failed", {"error": "x"}))
        assert True


class TestEventFiring:
    def test_connect_success_fires_connected(self, pm):
        events = []
        pm.on_change(lambda name, event, detail=None: events.append((name, event)))

        # 実フロー（load_all/register_server）では呼び出し前に必ず設定される
        pm._server_configs["fetch"] = {"command": "uvx", "args": []}
        fake_proxy = AsyncMock()
        fake_proxy.list_tools = AsyncMock(return_value=[type("T", (), {"name": "fetch", "description": ""})()])
        with patch.object(pm, "_create_proxy", return_value=fake_proxy):
            asyncio.run(pm._connect_and_mount("fetch", {"command": "uvx", "args": []}))

        assert ("fetch", "connected") in events
        assert pm._status.get("fetch") == "connected"

    def test_connect_failure_fires_spawn_failed(self, pm):
        events = []
        pm.on_change(lambda name, event, detail=None: events.append((name, event)))

        with patch.object(pm, "_create_proxy", side_effect=RuntimeError("no such command")):
            asyncio.run(pm._connect_and_mount("fetch", {"command": "uvx", "args": []}))

        assert ("fetch", "spawn_failed") in events
        assert pm._status.get("fetch") == "error"
