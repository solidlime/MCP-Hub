"""Test that MCP-Hub works as an actual MCP server for LLM agents.
Simulates: search_tools -> execute_tool flow (2-hop discovery).
"""

import asyncio
import json
import os
import tempfile

import pytest
from fastmcp import FastMCP

from mcp_hub.meta_provider import create_meta_app
from mcp_hub.proxy_manager import ProxyManager
from mcp_hub.store import JsonStore
from mcp_hub.streamable_http_patch import apply_patch

apply_patch()

_SCRIPT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "test_servers", "stdio_echo_server.py")
)


async def _hub_with_echo(name: str = "stdio-echo"):
    """Create a fresh MCP-Hub with a connected echo server.

    Returns (proxy_manager, meta_app).
    """
    mcp = FastMCP("hub")
    tmpdir = tempfile.mkdtemp()
    registry = JsonStore(data_dir=tmpdir)
    await registry.init()
    pm = ProxyManager(mcp, registry)
    meta_app = create_meta_app(pm)
    await meta_app.rebuild_index()
    pm.on_change(lambda: meta_app.rebuild_index())

    await pm.register_server(name, {"command": "python3", "args": [_SCRIPT]})
    for _ in range(60):
        s = pm.get_all_status().get(name)
        if s != "connecting":
            break
        await asyncio.sleep(0.2)
    assert pm.get_all_status()[name] == "connected", pm.get_all_status()
    await meta_app.rebuild_index()
    return pm, meta_app


class TestLLMAgentFlow:
    """Simulate an LLM agent using MCP-Hub's meta tools."""

    @pytest.mark.asyncio
    async def test_search_and_execute_echo(self):
        """search_tools -> execute_tool: the 2-hop LLM discovery flow."""
        pm, meta_app = await _hub_with_echo("stdio-echo")

        # Step 1: search_tools discovers the echo tool
        results = json.loads(await meta_app.meta_tools.search_tools("echo"))
        assert len(results["results"]) > 0
        assert any("echo" in r["name"] for r in results["results"])

        # Step 2: execute_tool runs it
        result = await meta_app.meta_tools.execute_tool(
            "stdio-echo", "echo", {"message": "hello from llm"}
        )
        assert "hello from llm" in str(result)

        await pm.unregister_server("stdio-echo")

    @pytest.mark.asyncio
    async def test_list_upstream_tools(self):
        """list_upstream_tools returns tools grouped by server."""
        pm, meta_app = await _hub_with_echo("echo-srv")

        listing = json.loads(await meta_app.meta_tools.list_upstream_tools())
        assert "echo-srv" in listing["tools_by_server"]
        assert "echo" in listing["tools_by_server"]["echo-srv"]

        # Verify via index directly too
        by_server = meta_app.index.get_tools_by_server()
        assert "echo" in by_server["echo-srv"]

        await pm.unregister_server("echo-srv")
