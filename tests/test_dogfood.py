"""Dogfood tests: end-to-end proxy pipeline for all three transport protocols.

Tests verify that MCP-Hub's ProxyManager can:
1. Register a server (stdio, SSE, Streamable HTTP)
2. Connect to it
3. List tools
4. Call a tool
5. Verify the result
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from mcp_hub.streamable_http_patch import apply_patch

# Apply patch before any tests run (required for Streamable HTTP 202 polling)
apply_patch()


# ---------------------------------------------------------------------------
# Fixtures (module-scoped, for servers that need lifecycle)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sse_server():
    """Start the SSE echo test server for the module's tests.

    Uses thread + event_loop pattern identical to async_server fixture.
    """
    from tests.test_servers.sse_echo_server import start_server, stop_server, get_port

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    future = asyncio.run_coroutine_threadsafe(start_server(), loop)
    future.result(timeout=5)

    # Use /sse path so FastMCP's transport auto-detection selects
    # SSETransport (requires /sse in URL path).
    url = f"http://localhost:{get_port()}/sse"
    yield url

    future = asyncio.run_coroutine_threadsafe(stop_server(), loop)
    future.result(timeout=5)

    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def streamable_server():
    """Start the async MCP test server for the module's tests.

    Uses thread + event_loop pattern to keep uvicorn alive.
    """
    from tests.test_servers.async_mcp_server import (
        _reset_state,
        get_port,
        start_server,
        stop_server,
    )

    _reset_state()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    future = asyncio.run_coroutine_threadsafe(start_server(), loop)
    future.result(timeout=5)

    url = f"http://localhost:{get_port()}/mcp"
    yield url

    future = asyncio.run_coroutine_threadsafe(stop_server(), loop)
    future.result(timeout=5)

    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def setup_hub_and_register(config: dict, server_name: str = "test-server") -> tuple:
    """Create a fresh FastMCP + JsonStore + ProxyManager and register a server.

    Args:
        config: Server configuration dict (e.g. {"command": ..., "args": [...]}
                or {"url": ...}).
        server_name: Name to register the server under.

    Returns:
        Tuple of (ProxyManager, registration status string).
    """
    import tempfile

    from fastmcp import FastMCP
    from mcp_hub.proxy_manager import ProxyManager
    from mcp_hub.store import JsonStore

    mcp = FastMCP("test-hub")
    tmpdir = tempfile.mkdtemp()
    registry = JsonStore(data_dir=tmpdir)
    await registry.init()
    pm = ProxyManager(mcp, registry)
    result = await pm.register_server(server_name, config)
    return pm, result["status"]


# ---------------------------------------------------------------------------
# Stdio Dogfood Test
# ---------------------------------------------------------------------------


class TestStdioDogfood:
    """End-to-end test for stdio transport via ProxyManager."""

    @pytest.mark.asyncio
    async def test_stdio_echo_tool_call(self):
        """stdio: register -> connect -> call echo tool -> verify ECHO:hello"""
        import os

        script_path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__), "test_servers", "stdio_echo_server.py"
            )
        )
        config = {"command": "python3", "args": [script_path]}
        pm, status = await setup_hub_and_register(config, "stdio-echo")
        assert status == "connecting"

        # Wait for background connection to complete
        for _ in range(60):
            s = pm.get_all_status().get("stdio-echo")
            if s != "connecting":
                break
            await asyncio.sleep(0.2)

        assert pm.get_all_status()["stdio-echo"] == "connected", (
            f"Status: {pm.get_all_status()}"
        )

        # List tools
        proxy = pm.get_proxy("stdio-echo")
        assert proxy is not None, "get_proxy returned None"
        tools = await pm.list_tools_for_server("stdio-echo", proxy)
        tool_names = [t.name for t in tools]
        assert "echo" in tool_names, f"Tools: {tool_names}"

        # Call tool through proxy
        result = await pm.call_tool("stdio-echo", "echo", {"message": "hello"})
        result_str = str(result)
        assert "hello" in result_str, f"Result: {result_str}"

        # Cleanup
        await pm.unregister_server("stdio-echo")


# ---------------------------------------------------------------------------
# SSE Dogfood Test
# ---------------------------------------------------------------------------


class TestSSEDogfood:
    """End-to-end test for SSE transport via ProxyManager."""

    @pytest.mark.asyncio
    async def test_sse_echo_tool_call(self, sse_server):
        """SSE: register -> connect -> call sse_echo tool -> verify SSE_ECHO:hello"""
        config = {"url": sse_server}
        pm, status = await setup_hub_and_register(config, "sse-echo")
        assert status == "connecting"

        # Wait for background connection to complete
        for _ in range(60):
            s = pm.get_all_status().get("sse-echo")
            if s != "connecting":
                break
            await asyncio.sleep(0.2)

        assert pm.get_all_status()["sse-echo"] == "connected", (
            f"Status: {pm.get_all_status()}"
        )

        # List tools
        proxy = pm.get_proxy("sse-echo")
        assert proxy is not None, "get_proxy returned None"
        tools = await pm.list_tools_for_server("sse-echo", proxy)
        tool_names = [t.name for t in tools]
        assert "sse_echo" in tool_names, f"Tools: {tool_names}"

        # Call tool through proxy
        result = await pm.call_tool("sse-echo", "sse_echo", {"message": "hello"})
        result_str = str(result)
        assert "hello" in result_str, f"Result: {result_str}"

        # Cleanup
        await pm.unregister_server("sse-echo")


# ---------------------------------------------------------------------------
# Streamable HTTP Dogfood Test
# ---------------------------------------------------------------------------


class TestStreamableDogfood:
    """End-to-end test for Streamable HTTP transport via ProxyManager."""

    @pytest.mark.asyncio
    async def test_streamable_tool_call(self, streamable_server):
        """Streamable HTTP (202 polling): register -> connect -> call search_async -> verify result"""
        from tests.test_servers.async_mcp_server import _reset_state

        _reset_state()

        config = {"url": streamable_server}
        pm, status = await setup_hub_and_register(config, "streamable-test")
        assert status == "connecting"

        # Wait for background connection to complete
        for _ in range(60):
            s = pm.get_all_status().get("streamable-test")
            if s != "connecting":
                break
            await asyncio.sleep(0.2)

        assert pm.get_all_status()["streamable-test"] == "connected", (
            f"Status: {pm.get_all_status()}"
        )

        # List tools
        proxy = pm.get_proxy("streamable-test")
        assert proxy is not None, "get_proxy returned None"
        tools = await pm.list_tools_for_server("streamable-test", proxy)
        tool_names = [t.name for t in tools]
        assert "search_async" in tool_names, f"Tools: {tool_names}"

        # Call tool through proxy
        result = await pm.call_tool(
            "streamable-test", "search_async", {"query": "hello"}
        )
        assert result is not None, "Tool call returned None"

        # Cleanup
        await pm.unregister_server("streamable-test")
