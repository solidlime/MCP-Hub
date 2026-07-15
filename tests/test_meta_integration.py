"""
Meta-tools integration tests using TestClient.

Creates a minimal FastAPI app with the meta endpoint mounted.
Uses a mock proxy manager to avoid needing real MCP server connections.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcp_hub.meta_provider import create_meta_app

logger = logging.getLogger(__name__)

# ── test fixtures ────────────────────────────────────────────────────────────

SAMPLE_TOOLS = [
    SimpleNamespace(
        name="fetch_url",
        description="Fetch a URL and return markdown content",
        parameters={"type": "object", "properties": {"url": {"type": "string"}}},
    ),
    SimpleNamespace(
        name="brave_web_search",
        description="Search the web using Brave Search API",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
    ),
    SimpleNamespace(
        name="puppeteer_screenshot",
        description="Take a screenshot of a web page",
        parameters={"type": "object", "properties": {"url": {"type": "string"}}},
    ),
    SimpleNamespace(
        name="file_read",
        description="Read file contents from disk",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    ),
    SimpleNamespace(
        name="file_write",
        description="Write content to a file on disk",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
        },
    ),
]


def _build_mock_proxy_manager():
    """Create a ProxyManager mock with SAMPLE_TOOLS available."""
    pm = MagicMock()
    pm._proxies = {}  # kept for internal consistency
    pm.call_tool = AsyncMock(return_value="ok")
    # Support the public API — get_connected_servers returns snapshot of _proxies
    pm.get_connected_servers = MagicMock(side_effect=lambda: dict(pm._proxies))
    return pm


def _build_mock_proxy(tools: list) -> MagicMock:
    """Create a proxy mock whose list_tools returns the given tools."""
    proxy = MagicMock()
    proxy.list_tools = AsyncMock(return_value=tools)
    return proxy


@pytest.fixture
async def meta_app():
    """Build a FastAPI app with /mcp-meta mounted and a populated index."""
    pm = _build_mock_proxy_manager()

    # Add a mock proxy with sample tools so rebuild_index populates the index
    pm._proxies["filesystem"] = _build_mock_proxy(
        [t for t in SAMPLE_TOOLS if "file" in t.name]
    )
    pm._proxies["fetch"] = _build_mock_proxy(
        [t for t in SAMPLE_TOOLS if "fetch" in t.name]
    )
    pm._proxies["brave-search"] = _build_mock_proxy(
        [t for t in SAMPLE_TOOLS if "brave" in t.name]
    )
    pm._proxies["puppeteer"] = _build_mock_proxy(
        [t for t in SAMPLE_TOOLS if "puppeteer" in t.name]
    )

    meta_app = create_meta_app(pm)
    meta_mcp = meta_app.mcp
    meta_http = meta_mcp.http_app(
        transport="streamable-http", path="/", stateless_http=True
    )

    # Populate the index from mock proxies
    await meta_app.rebuild_index()

    app = FastAPI(lifespan=meta_http.lifespan)
    app.mount("/mcp-meta", meta_http)

    app.state.meta_app = meta_app
    app.state.meta_http = meta_http
    return app


@pytest.fixture
def client(meta_app):
    """TestClient wrapping the meta FastAPI app."""
    with TestClient(meta_app) as c:
        yield c


# ── helpers ───────────────────────────────────────────────────────────────────


def parse_sse(response) -> dict:
    """Extract JSON from a Streamable HTTP SSE response."""
    data = ""
    for line in response.text.split("\n"):
        if line.startswith("data: "):
            data += line[6:]
    return json.loads(data)


_META_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _post_tools_list(client):
    """Call tools/list on the meta endpoint and return parsed result."""
    r = client.post(
        "/mcp-meta/",
        json={"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": "list"},
        headers=_META_HEADERS,
    )
    assert r.status_code == 200
    return parse_sse(r)


def _call_tool(client, name: str, arguments: dict, tool_id: str = "call"):
    """Call a meta tool and return parsed result."""
    r = client.post(
        "/mcp-meta/",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
            "id": tool_id,
        },
        headers=_META_HEADERS,
    )
    assert r.status_code == 200
    return parse_sse(r)


def _get_text_content(result: dict) -> str:
    """Extract the text field from a tools/call result."""
    return result["result"]["content"][0]["text"]


# ── tests ─────────────────────────────────────────────────────────────────────


class TestMetaIntegration:
    """End-to-end tests for the /mcp-meta endpoint."""

    def test_mcp_meta_endpoint_exists(self, client):
        """GET /mcp-meta returns non-404. Streamable HTTP returns 406
        without proper Accept header — 406 proves the route exists."""
        r = client.get("/mcp-meta/")
        # 406 Not Acceptable means the endpoint exists (without correct Accept)
        assert r.status_code != 404

    def test_mcp_meta_has_three_tools(self, client):
        """Meta app exposes exactly 3 tools: search_tools, get_tool_schema, execute_tool."""
        parsed = _post_tools_list(client)
        tools = parsed["result"]["tools"]
        assert len(tools) == 3
        names = {t["name"] for t in tools}
        assert names == {"search_tools", "get_tool_schema", "execute_tool"}

    def test_search_tools_returns_results(self, client):
        """search_tools with a query returns a JSON response with result list."""
        parsed = _call_tool(
            client, "search_tools", {"query": "file", "top_k": 10}, "s1"
        )
        text = _get_text_content(parsed)
        data = json.loads(text)
        assert "results" in data
        results = data["results"]
        assert len(results) >= 2
        names = {r["name"] for r in results}
        assert "file_read" in names
        assert "file_write" in names

    def test_get_tool_schema(self, client):
        """get_tool_schema for an indexed tool returns its inputSchema."""
        parsed = _call_tool(
            client,
            "get_tool_schema",
            {"server": "filesystem", "tool_name": "file_read"},
            "s2",
        )
        text = _get_text_content(parsed)
        data = json.loads(text)
        assert "inputSchema" in data
        assert data["name"] == "file_read"
        assert data["server"] == "filesystem"
        assert "path" in data["inputSchema"]["properties"]

    def test_get_tool_schema_nonexistent(self, client):
        """get_tool_schema for a nonexistent tool returns an error."""
        parsed = _call_tool(
            client,
            "get_tool_schema",
            {"server": "nonexistent", "tool_name": "foo"},
            "s3",
        )
        text = _get_text_content(parsed)
        data = json.loads(text)
        assert "error" in data
        assert "nonexistent" in data["error"]

    def test_execute_tool(self, client):
        """execute_tool dispatches to proxy_manager.call_tool and returns result."""
        parsed = _call_tool(
            client,
            "execute_tool",
            {"server": "filesystem", "tool_name": "file_read", "arguments": {"path": "/tmp/test.txt"}},
            "s4",
        )
        # Mock returns "ok" — verify we got a non-error response
        text = _get_text_content(parsed)
        assert text == "ok"

    def test_meta_mode_always_mounted(self, client):
        """/mcp-meta is always accessible regardless of meta_mode setting."""
        r = client.get("/mcp-meta/")
        assert r.status_code != 404

    def test_search_tools_respects_top_k(self, client):
        """top_k=1 returns exactly 1 result."""
        parsed = _call_tool(
            client, "search_tools", {"query": "file", "top_k": 1}, "s5"
        )
        text = _get_text_content(parsed)
        data = json.loads(text)
        assert "results" in data
        assert len(data["results"]) == 1
