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
from mcp_hub.state import request_tags

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

    async def _list_tools(tags=None):
        from mcp_hub.state import request_tags
        if tags is None:
            tags = request_tags.get(None)
        result = {}
        for name, proxy in pm._proxies.items():
            if tags:
                server_tags = pm.server_tags(name)
                if not any(t in server_tags for t in tags):
                    continue
            tools = await proxy.list_tools()
            result[name] = [
                {"name": t.name, "description": t.description or ""} for t in tools
            ]
        return result

    async def _list_tools_for_server(name, proxy):
        return await proxy.list_tools()

    pm.list_tools = AsyncMock(side_effect=_list_tools)
    pm.list_tools_for_server = AsyncMock(side_effect=_list_tools_for_server)
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
    app.state.proxy_manager = pm
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

    def test_mcp_meta_has_expected_tools(self, client):
        """Meta app exposes 3 tools: search_tools, execute_tool, list_upstream_tools."""
        parsed = _post_tools_list(client)
        tools = parsed["result"]["tools"]
        assert len(tools) == 3
        names = {t["name"] for t in tools}
        assert names == {"search_tools", "execute_tool", "list_upstream_tools"}

    def test_search_tools_returns_results(self, client):
        """search_tools with a query returns a JSON response with result list."""
        parsed = _call_tool(
            client, "search_tools", {"query": "file", "top_k": 3}, "s1"
        )
        text = _get_text_content(parsed)
        data = json.loads(text)
        assert "results" in data
        results = data["results"]
        assert len(results) >= 1
        names = {r["name"] for r in results}
        # At least one of file_read/file_write should be in results
        assert "file_read" in names or "file_write" in names

    def test_search_tools_compact_output(self, client):
        """inputSchema rides only on the first result (compact output)."""
        parsed = _call_tool(
            client, "search_tools", {"query": "file", "top_k": 3}, "s6"
        )
        data = json.loads(_get_text_content(parsed))
        results = data["results"]
        assert len(results) >= 1
        assert "inputSchema" in results[0]
        assert all("inputSchema" not in r for r in results[1:])

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


class TestMetaTagFiltering:
    """Regression tests for issue #1: servers carrying multiple tags (e.g.
    [librarian, search]) must NOT be excluded when the client requests a
    single matching tag (librarian). Tag matching is plain OR."""

    TAGS = {
        "filesystem": ["dev"],
        "fetch": ["librarian", "search"],  # multi-tag server (issue #1 case)
        "brave-search": ["search"],
        "puppeteer": ["librarian"],
    }

    def _set_server_tags(self, client):
        pm = client.app.state.proxy_manager
        pm.server_tags.side_effect = lambda name: self.TAGS.get(name, [])

    def _call_list_upstream(self, client, tags):
        """Set request_tags, call list_upstream_tools, return parsed JSON dict."""
        request_tags.set(tags)
        try:
            parsed = _call_tool(client, "list_upstream_tools", {}, "t1")
        finally:
            request_tags.set(None)
        return json.loads(_get_text_content(parsed))

    def test_librarian_tag_keeps_multi_tag_servers(self, client):
        """Issue #1: requesting 'librarian' alone must include servers tagged
        ['librarian', 'search'] — they were wrongly excluded in the reporter's
        environment (stale container build)."""
        self._set_server_tags(client)
        data = self._call_list_upstream(client, ["librarian"])
        servers = set(data["tools_by_server"].keys())
        assert "fetch" in servers        # [librarian, search] matches 'librarian'
        assert "puppeteer" in servers    # [librarian] matches 'librarian'
        assert "brave-search" not in servers  # [search] only
        assert "filesystem" not in servers    # [dev] only

    def test_search_tag_matches_search_servers(self, client):
        """Requesting 'search' includes all servers with the search tag."""
        self._set_server_tags(client)
        data = self._call_list_upstream(client, ["search"])
        servers = set(data["tools_by_server"].keys())
        assert "fetch" in servers        # [librarian, search] matches 'search'
        assert "brave-search" in servers
        assert "puppeteer" not in servers
        assert "filesystem" not in servers


class TestLiveProxyListing:
    """list_upstream_tools / execute_tool read from the live proxy manager,
    not the (possibly stale / partially rebuilt) index."""

    def test_list_upstream_tools_sees_new_server_without_rebuild(self, client):
        """A server added after the last rebuild still appears in the listing."""
        pm = client.app.state.proxy_manager
        pm._proxies["fresh-server"] = _build_mock_proxy(
            [SimpleNamespace(name="fresh_tool", description="", parameters={})]
        )
        # Note: no rebuild_index() call — this is the point.
        data = self._call_list_upstream(client, None)
        servers = set(data["tools_by_server"].keys())
        assert "fresh-server" in servers
        assert "filesystem" in servers  # original servers still listed

    def test_execute_tool_works_for_server_not_in_index(self, client):
        """execute_tool must not depend on the index — a server that failed
        to rebuild still executes (previously rejected with 'tool not found')."""
        pm = client.app.state.proxy_manager
        pm._proxies["fresh-server"] = _build_mock_proxy(
            [SimpleNamespace(name="fresh_tool", description="", parameters={})]
        )
        parsed = _call_tool(
            client,
            "execute_tool",
            {"server": "fresh-server", "tool_name": "fresh_tool", "arguments": {}},
            "t-fresh",
        )
        assert _get_text_content(parsed) == "ok"

    def test_execute_tool_rejects_missing_tool(self, client):
        """Unknown tool on a known server is still rejected."""
        parsed = _call_tool(
            client,
            "execute_tool",
            {"server": "filesystem", "tool_name": "no_such_tool", "arguments": {}},
            "t-missing",
        )
        text = _get_text_content(parsed)
        assert "not found" in text

    def test_execute_tool_rejects_wrong_tag(self, client):
        """Tag mismatch blocks execution (live server tags, not index)."""
        pm = client.app.state.proxy_manager
        pm.server_tags.side_effect = lambda name: {
            "filesystem": ["dev"],
            "fresh-server": ["search"],
        }.get(name, [])
        pm._proxies["fresh-server"] = _build_mock_proxy(
            [SimpleNamespace(name="fresh_tool", description="", parameters={})]
        )
        request_tags.set(["librarian"])
        try:
            parsed = _call_tool(
                client,
                "execute_tool",
                {"server": "fresh-server", "tool_name": "fresh_tool", "arguments": {}},
                "t-tag",
            )
        finally:
            request_tags.set(None)
        text = _get_text_content(parsed)
        assert "not available" in text

    def _call_list_upstream(self, client, tags):
        request_tags.set(tags)
        try:
            parsed = _call_tool(client, "list_upstream_tools", {}, "t-live")
        finally:
            request_tags.set(None)
        return json.loads(_get_text_content(parsed))


class TestFlattenedCallCompat:
    """Compatibility shim: some LLM clients flatten ALL execute_tool params
    inside `arguments` (prod logs, 5 occurrences):
    {"arguments": {"numResults": 8, "query": "...", "server": "Exa",
    "tool_name": "web_search_exa"}} — pydantic rejected these before our code
    ran. server/tool_name are lifted from `arguments` ONLY when the top-level
    value is missing, so correct callers are byte-for-byte unaffected."""

    def test_flattened_call_lifts_server_and_tool_name(self, client):
        """Flat call reaches the executor with lifted server/tool_name and
        the meta keys removed from arguments."""
        pm = client.app.state.proxy_manager
        parsed = _call_tool(
            client,
            "execute_tool",
            {"arguments": {"server": "filesystem", "tool_name": "file_read",
                           "path": "/tmp/x.txt"}},
            "t-flat",
        )
        assert _get_text_content(parsed) == "ok"
        pm.call_tool.assert_awaited_once_with(
            "filesystem", "file_read", {"path": "/tmp/x.txt"}
        )

    def test_normal_call_does_not_steal_legit_server_param(self, client):
        """Top-level present → arguments dict passed through untouched, even
        if the upstream tool legitimately has a 'server' parameter."""
        pm = client.app.state.proxy_manager
        parsed = _call_tool(
            client,
            "execute_tool",
            {"server": "filesystem", "tool_name": "file_read",
             "arguments": {"path": "/p", "server": "not-mine"}},
            "t-legit",
        )
        assert _get_text_content(parsed) == "ok"
        pm.call_tool.assert_awaited_once_with(
            "filesystem", "file_read", {"path": "/p", "server": "not-mine"}
        )

    def test_partial_lift_fills_only_the_missing_field(self, client):
        """server at top level, tool_name buried in arguments → lift only tool_name."""
        pm = client.app.state.proxy_manager
        parsed = _call_tool(
            client,
            "execute_tool",
            {"server": "filesystem",
             "arguments": {"tool_name": "file_read", "path": "/p"}},
            "t-partial",
        )
        assert _get_text_content(parsed) == "ok"
        pm.call_tool.assert_awaited_once_with(
            "filesystem", "file_read", {"path": "/p"}
        )

    def test_both_missing_returns_friendly_error(self, client):
        """Nothing to lift → JSON error naming both params and search_tools."""
        pm = client.app.state.proxy_manager
        parsed = _call_tool(
            client, "execute_tool", {"arguments": {"query": "hi"}}, "t-empty"
        )
        text = _get_text_content(parsed)
        assert "server" in text and "tool_name" in text
        assert "search_tools" in text
        pm.call_tool.assert_not_awaited()

    def test_flattened_call_resolves_case_insensitively(self, client):
        """ora-1: "Exa" (as LLMs send it) reaches the proxy registered as
        lowercase "exa", and call_tool gets the canonical registered name."""
        pm = client.app.state.proxy_manager
        pm._proxies["exa"] = _build_mock_proxy(
            [SimpleNamespace(name="web_search_exa", description="", parameters={})]
        )
        parsed = _call_tool(
            client,
            "execute_tool",
            {"arguments": {"query": "hi", "server": "Exa",
                           "tool_name": "web_search_exa"}},
            "t-case",
        )
        assert _get_text_content(parsed) == "ok"
        pm.call_tool.assert_awaited_once_with(
            "exa", "web_search_exa", {"query": "hi"}
        )

    def test_ambiguous_case_passthrough_keeps_existing_error(self, client):
        """Both "EXA" and "exa" live → no unique resolution; pass through so
        the existing not-found error fires (never guess)."""
        meta = client.app.state.meta_app.meta_tools
        meta._list_servers = lambda: ["EXA", "exa"]
        assert meta._resolve_server_name("Exa") == "Exa"
        assert meta._resolve_server_name("exa") == "exa"  # exact match wins
        assert meta._resolve_server_name("nope") == "nope"


class TestServerResolutionErrors:
    """execute_tool error paths after case-insensitive resolution: an absent
    server must say "not found" (with the live list) rather than masquerading
    as a tag-filter rejection, and a real tag rejection must expose the
    server's actual tags for debugging."""

    def test_unknown_server_no_tags_returns_not_found(self, client):
        """No tag header + unknown server → 'not found' (not a misleading tag
        error) and the available server list is attached."""
        parsed = _call_tool(
            client,
            "execute_tool",
            {"server": "ghost", "tool_name": "whatever", "arguments": {}},
            "t-ghost",
        )
        data = json.loads(_get_text_content(parsed))
        assert "not found" in data["error"]
        assert "filesystem" in data["available_servers"]
        assert "tag filter" not in data["error"]

    def test_tag_mismatch_exposes_server_tags(self, client):
        """Connected server whose tags miss the filter → tag-filter error that
        includes the server's real tags so the mismatch is debuggable."""
        pm = client.app.state.proxy_manager
        pm.server_tags.side_effect = lambda name: {
            "brave-search": ["search"],
        }.get(name, [])
        request_tags.set(["librarian"])
        try:
            parsed = _call_tool(
                client,
                "execute_tool",
                {"server": "brave-search", "tool_name": "brave_web_search",
                 "arguments": {}},
                "t-tagdbg",
            )
        finally:
            request_tags.set(None)
        data = json.loads(_get_text_content(parsed))
        assert "tag filter" in data["error"]
        assert data["server_tags"] == ["search"]


class TestRebuildIndex:
    """rebuild_index must report servers whose tools could not be fetched,
    so the caller (main.py) can retry with backoff instead of silently
    dropping them from the index forever."""

    async def test_returns_failed_server_names(self, meta_app):
        """A server whose list_tools raises is reported as failed."""
        pm = meta_app.state.proxy_manager
        broken = _build_mock_proxy([])
        broken.list_tools = AsyncMock(side_effect=RuntimeError("boom"))
        pm._proxies["broken"] = broken

        failed = await meta_app.state.meta_app.rebuild_index()
        assert "broken" in failed
        # Healthy servers are not reported as failed
        assert "filesystem" not in failed
        assert "fetch" not in failed

    async def test_returns_empty_list_on_full_success(self, meta_app):
        """When every server lists tools, no failures are reported."""
        failed = await meta_app.state.meta_app.rebuild_index()
        assert failed == []
