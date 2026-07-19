"""
Tests for connection retry logic in ProxyManager.

Lock discipline for these tests:
- IO/retry happens OUTSIDE self._lock
- State mutation (_proxies, _server_configs, _status) happens INSIDE self._lock
"""

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


class TestRetryEnv:
    def test_retry_env_defaults(self):
        """No env vars set → returns (3, 1.0)."""
        max_r, delay = ProxyManager._retry_env()
        assert max_r == 3
        assert delay == 1.0

    def test_retry_env_custom(self, monkeypatch):
        """Env vars override defaults."""
        monkeypatch.setenv("MCP_HUB_RETRY_MAX", "5")
        monkeypatch.setenv("MCP_HUB_RETRY_DELAY", "2.5")
        max_r, delay = ProxyManager._retry_env()
        assert max_r == 5
        assert delay == 2.5


class TestConnectServer:
    @pytest.mark.asyncio
    async def test_connect_server_retries_on_transient_error(self, manager):
        """Retries on ConnectionError, eventually succeeds."""
        proxy_ok = _MockProxy("srv")
        manager._create_proxy = MagicMock(
            side_effect=[ConnectionError("fail1"), ConnectionError("fail2"), proxy_ok]
        )

        proxy = await manager._connect_server("srv", {})

        assert proxy is not None
        assert proxy.name == "srv"
        assert manager._create_proxy.call_count == 3

    @pytest.mark.asyncio
    async def test_connect_server_exhausts_retries(self, manager, monkeypatch):
        """Returns None after max retries exhausted."""
        monkeypatch.setenv("MCP_HUB_RETRY_MAX", "1")
        manager._create_proxy = MagicMock(side_effect=ConnectionError("fail"))

        proxy = await manager._connect_server("srv", {})

        assert proxy is None
        # 2 calls: initial + 1 retry (max_retries=1)
        assert manager._create_proxy.call_count == 2

    @pytest.mark.asyncio
    async def test_connect_server_no_retry_on_valueerror(self, manager):
        """ValueError is not retried — breaks out immediately."""
        manager._create_proxy = MagicMock(side_effect=ValueError("bad config"))

        proxy = await manager._connect_server("srv", {})

        assert proxy is None
        assert manager._create_proxy.call_count == 1




class TestRegisterServer:
    @pytest.mark.asyncio
    async def test_register_server_saves_and_returns_immediately(self, manager):
        """register_server saves to DB, starts background connect, returns immediately."""
        manager.registry.add_server = AsyncMock()
        manager._create_proxy = MagicMock(return_value=_MockProxy("srv1"))

        result = await manager.register_server("srv1", {"url": "http://localhost:9999"})

        assert result["name"] == "srv1"
        assert result["status"] == "connecting"
        assert result["config"] == {"url": "http://localhost:9999"}
        # Background task not yet awaited — proxy not in _proxies immediately
        assert "srv1" not in manager._proxies
        assert manager._status["srv1"] == "connecting"

    @pytest.mark.asyncio
    async def test_register_server_disabled_server_no_connect(self, manager):
        """register_server with disabled=True skips background connection."""
        manager.registry.add_server = AsyncMock()

        result = await manager.register_server("srv1", {"url": "http://localhost:9999", "disabled": True})

        assert result["name"] == "srv1"
        assert result["status"] == "disabled"
        assert "srv1" not in manager._proxies
        assert manager._status["srv1"] == "disabled"
