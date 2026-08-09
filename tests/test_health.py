"""
Tests for background health monitoring in ProxyManager.

Lock discipline for these tests:
- IO/retry happens OUTSIDE self._lock
- Health monitor reads config under lock, does IO outside, writes status under lock
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP

from mcp_hub.proxy_manager import ProxyManager


class _MockProxy:
    """Duck-type mock for FastMCPProxy."""

    def __init__(self, name="mock"):
        self.name = name
        self.list_tools = AsyncMock(return_value=[])


class _MockStore:
    """Minimal mock for JsonStore."""

    async def list_servers(self):
        return []

    async def add_server(self, name, config):
        pass

    async def get_server(self, name):
        return None

    async def remove_server(self, name):
        return True


@pytest.fixture
def manager():
    mcp = FastMCP("test")
    store = _MockStore()
    pm = ProxyManager(mcp, store)
    pm.mcp.mount = MagicMock(return_value=None)
    return pm


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_detects_failure(self, manager, monkeypatch):
        """Error status on list_tools failure (after max_failures reached)."""
        monkeypatch.setenv("MCP_HUB_HEALTH_MAX_FAILURES", "1")
        proxy = _MockProxy("srv1")
        proxy.list_tools = AsyncMock(side_effect=ConnectionError("fail"))
        manager._proxies["srv1"] = proxy
        manager._server_configs["srv1"] = {"url": "http://localhost:9999"}
        manager._status["srv1"] = "connected"

        await manager._health_check()

        assert manager._status["srv1"] == "error"

    @pytest.mark.asyncio
    async def test_health_check_detects_recovery(self, manager):
        """Status transitions error → recovering → connected."""
        proxy = _MockProxy("srv1")
        proxy.list_tools = AsyncMock(return_value=[])  # succeeds
        manager._proxies["srv1"] = proxy
        manager._server_configs["srv1"] = {"url": "http://localhost:9999"}
        manager._status["srv1"] = "error"

        new_proxy = _MockProxy("srv1")
        manager._connect_server = AsyncMock(return_value=new_proxy)

        await manager._health_check()

        assert manager._status["srv1"] == "connected"
        assert manager._proxies["srv1"] is new_proxy

    @pytest.mark.asyncio
    async def test_health_check_skips_disabled(self, manager):
        """Disabled servers are left untouched."""
        proxy = _MockProxy("srv1")
        manager._proxies["srv1"] = proxy
        manager._server_configs["srv1"] = {"url": "http://localhost:9999", "disabled": True}
        manager._status["srv1"] = "error"

        await manager._health_check()

        proxy.list_tools.assert_not_called()
        assert manager._status["srv1"] == "error"

    @pytest.mark.asyncio
    async def test_health_check_timeout(self, manager, monkeypatch):
        """asyncio.wait_for timeout marks status error."""
        monkeypatch.setenv("MCP_HUB_HEALTH_TIMEOUT", "1")
        monkeypatch.setenv("MCP_HUB_HEALTH_MAX_FAILURES", "1")

        async def never_return():
            await asyncio.sleep(3600)

        proxy = _MockProxy("srv1")
        proxy.list_tools = AsyncMock(side_effect=never_return)
        manager._proxies["srv1"] = proxy
        manager._server_configs["srv1"] = {"url": "http://localhost:9999"}
        manager._status["srv1"] = "connected"

        await manager._health_check()

        assert manager._status["srv1"] == "error"


class TestHealthFailureTolerance:
    """Transient failures must not flip a server to error immediately."""

    def _fail_proxy(self, manager, name="srv1"):
        proxy = _MockProxy(name)
        proxy.list_tools = AsyncMock(side_effect=ConnectionError("fail"))
        manager._proxies[name] = proxy
        manager._server_configs[name] = {"url": "http://localhost:9999"}
        manager._status[name] = "connected"
        manager._tool_cache.pop(name, None)  # bypass list_tools_for_server cache
        return proxy

    def _set_proxy_tools(self, manager, proxy, tools=None, name="srv1"):
        """Replace list_tools result and bypass the 60s tool cache."""
        if tools is None:
            proxy.list_tools = AsyncMock(side_effect=ConnectionError("fail"))
        else:
            proxy.list_tools = AsyncMock(return_value=tools)
        manager._tool_cache.pop(name, None)
        return proxy

    @pytest.mark.asyncio
    async def test_single_failure_keeps_connected(self, manager):
        """One failure (below max_failures=3) does not mark the server error."""
        self._fail_proxy(manager)

        await manager._health_check()

        assert manager._status["srv1"] == "connected"
        assert manager._health_failures["srv1"] == 1

    @pytest.mark.asyncio
    async def test_threshold_reached_marks_error(self, manager, monkeypatch):
        """max_failures=2: two consecutive failures mark the server error."""
        monkeypatch.setenv("MCP_HUB_HEALTH_MAX_FAILURES", "2")
        self._fail_proxy(manager)

        await manager._health_check()
        assert manager._status["srv1"] == "connected"

        await manager._health_check()
        assert manager._status["srv1"] == "error"
        # counter reset after marking error — next failure needs 2 again
        assert manager._health_failures["srv1"] == 0

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self, manager):
        """A success between failures resets the counter (no error on next fail)."""
        proxy = self._fail_proxy(manager)

        await manager._health_check()
        assert manager._health_failures["srv1"] == 1

        self._set_proxy_tools(manager, proxy, tools=[])  # now succeeds
        await manager._health_check()
        assert "srv1" not in manager._health_failures

        self._set_proxy_tools(manager, proxy, tools=None)  # fails again
        await manager._health_check()
        # failure 1 of 3 again — still connected
        assert manager._status["srv1"] == "connected"
        assert manager._health_failures["srv1"] == 1


class TestHealthMonitorLifecycle:
    @pytest.mark.asyncio
    async def test_health_monitor_loop_survives_exceptions(self, manager):
        """Loop continues after _health_check raises an exception."""
        call_count = 0

        async def flaky_check():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("boom")
            # On second call, cancel the task to stop the loop
            manager._health_task.cancel()

        manager._health_check = flaky_check

        manager.start_health_monitor(interval=0.01)

        await asyncio.sleep(0.2)

        assert call_count >= 2
        assert manager._health_task.done()

    @pytest.mark.asyncio
    async def test_health_monitor_no_zombie_on_restart(self, manager):
        """Restarting cancels old task — prevents zombie tasks."""
        manager.start_health_monitor(interval=60)
        old_task = manager._health_task

        manager.start_health_monitor(interval=60)
        # Yield to event loop so old_task processes cancellation
        await asyncio.sleep(0)
        new_task = manager._health_task

        assert new_task is not old_task
        assert old_task.done()

    @pytest.mark.asyncio
    async def test_start_stop_health_monitor(self, manager):
        """start_health_monitor creates task; stop_health_monitor cancels it."""
        assert manager._health_task is None

        manager.start_health_monitor(interval=60)
        assert manager._health_task is not None
        assert not manager._health_task.done()

        await manager.stop_health_monitor()
        assert manager._health_task is None
