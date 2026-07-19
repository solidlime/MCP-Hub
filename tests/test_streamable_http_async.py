"""Integration tests for Streamable HTTP 202 async polling support.

Requires the async MCP test server from tests/test_servers/async_mcp_server.py.
Tests both:
1. Direct transport-level: FastMCP Client → async server (202→polling)
2. MCP-Hub proxy pipeline: MCP-Hub proxy → async server
"""

from __future__ import annotations

import asyncio
import threading
import pytest

from mcp_hub.streamable_http_patch import apply_patch, restore_patch

# Apply patch before any tests run
apply_patch()


@pytest.fixture(scope="module")
def async_server():
    """Start the async test server for the module's tests.

    Runs the server on a dedicated thread with its own event loop so that
    the uvicorn server task stays alive for the duration of the module.
    """
    from tests.test_servers.async_mcp_server import start_server, stop_server, get_port

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    future = asyncio.run_coroutine_threadsafe(start_server(), loop)
    future.result()  # wait for startup

    url = f"http://localhost:{get_port()}/mcp"
    yield url

    future = asyncio.run_coroutine_threadsafe(stop_server(), loop)
    future.result()  # wait for shutdown

    loop.call_soon_threadsafe(loop.stop)
    thread.join()


@pytest.fixture
def fresh_state(async_server):
    """Reset server state between tests."""
    from tests.test_servers.async_mcp_server import _reset_state
    _reset_state()
    yield async_server


class TestDirectTransport:
    """Test 202 polling at the transport level (Client → async server directly)."""

    @pytest.mark.asyncio
    async def test_list_tools_via_202_polling(self, fresh_state):
        """tools/list returns results after 202→polling."""
        from fastmcp.client import Client
        from fastmcp.client.transports.http import StreamableHttpTransport

        transport = StreamableHttpTransport(fresh_state)
        client = Client(transport)

        async with client:
            tools = await client.list_tools()
            tool_names = [t.name for t in tools]
            assert "search_async" in tool_names, (
                f"Expected 'search_async' in tools, got {tool_names}"
            )

    @pytest.mark.asyncio
    async def test_call_tool_after_polling(self, fresh_state):
        """Tool call works after successful 202 polling."""
        from fastmcp.client import Client
        from fastmcp.client.transports.http import StreamableHttpTransport

        transport = StreamableHttpTransport(fresh_state)
        client = Client(transport)

        async with client:
            tools = await client.list_tools()
            assert len(tools) > 0
            result = await client.call_tool("search_async", {"query": "test"})
            assert result is not None


class TestMCPHubProxy:
    """Test 202 polling through MCP-Hub's proxy pipeline."""

    @pytest.mark.asyncio
    async def test_proxy_list_tools_via_202(self, fresh_state):
        """Proxy correctly handles 202 polling from upstream server."""
        from fastmcp import FastMCP
        from mcp_hub.proxy_manager import ProxyManager
        from mcp_hub.store import JsonStore

        import tempfile, os
        mcp = FastMCP("test-hub")
        tmpdir = tempfile.mkdtemp()
        try:
            registry = JsonStore(data_dir=tmpdir)
            await registry.init()

            pm = ProxyManager(mcp, registry)
            config = {"url": fresh_state}

            # Register the async server as a proxy
            result = await pm.register_server("async-test", config)
            assert result["status"] == "connecting"

            # Wait for background connection to complete (polling takes ~2s)
            for _ in range(60):
                status = pm.get_all_status().get("async-test")
                if status == "connected":
                    break
                if status == "error":
                    break
                await asyncio.sleep(0.2)

            status = pm.get_all_status()["async-test"]
            assert status == "connected", (
                f"Expected 'connected', got {pm.get_all_status()}"
            )

            # Verify tools were discovered
            proxy = pm.get_proxy("async-test")
            assert proxy is not None, "get_proxy returned None"
            tools = await pm.list_tools_for_server("async-test", proxy)
            tool_names = [t.name for t in tools]
            assert "search_async" in tool_names

            # Cleanup
            await pm.unregister_server("async-test")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
