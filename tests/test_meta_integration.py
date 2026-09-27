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
    pm.server_description = MagicMock(return_value="")
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

    meta_app = await create_meta_app(pm)
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


async def test_index_text_keeps_server_prefix_display_drops_it():
    """索引テキストはサーバー説明を前置し _INDEX_DESC_CHARS で切る。

    表示 description は前置なし・_DISPLAY_DESC_CHARS 上限。full_description は
    無制限。サーバー説明が無いサーバーは索引テキストも素のツール説明のまま。
    """
    from mcp_hub.meta_provider import _INDEX_DESC_CHARS

    pm = _build_mock_proxy_manager()
    pm.server_description = MagicMock(
        side_effect=lambda s: {
            "filesystem": "  ローカルファイル操作  ",
            "fetch": "Web 取得",
        }.get(s, "")
    )
    pm._proxies["filesystem"] = _build_mock_proxy(
        [
            SimpleNamespace(
                name="file_read",
                description="  Read file contents from disk  ",
                parameters={},
            )
        ]
    )
    pm._proxies["fetch"] = _build_mock_proxy(
        [SimpleNamespace(name="fetch_url", description="y" * 500, parameters={})]
    )
    pm._proxies["brave-search"] = _build_mock_proxy(
        [
            SimpleNamespace(
                name="brave_web_search",
                description="Brave web search",
                parameters={},
            )
        ]
    )

    app = await create_meta_app(pm, use_embeddings=False)
    await app.rebuild_index()

    docs = {d["name"]: d for d in app.index._documents}
    # 索引テキスト: サーバー説明を前置し、ツール説明は strip + 400 字で切る
    assert (
        docs["file_read"]["index_text"]
        == "ローカルファイル操作 Read file contents from disk"
    )
    assert docs["fetch_url"]["index_text"] == "Web 取得 " + "y" * _INDEX_DESC_CHARS
    # サーバー説明なし → 索引テキストも前置なし・余分な空白なし
    assert docs["brave_web_search"]["index_text"] == "Brave web search"
    # 表示 description: サーバー前置なし
    assert docs["file_read"]["description"] == "Read file contents from disk"
    assert docs["brave_web_search"]["description"] == "Brave web search"
    # 600 字以下は切り詰めなし（"…" を付けない）
    assert docs["fetch_url"]["description"] == "y" * 500
    # full_description は無制限
    assert docs["file_read"]["full_description"] == "Read file contents from disk"
    assert docs["fetch_url"]["full_description"] == "y" * 500


async def test_display_description_truncates_with_ellipsis():
    """600 字超の表示 description は "…" 付きで切る。索引は 400 字、full は無制限。"""
    from mcp_hub.meta_provider import _DISPLAY_DESC_CHARS, _INDEX_DESC_CHARS

    pm = _build_mock_proxy_manager()
    long = "z" * 700
    pm._proxies["big"] = _build_mock_proxy(
        [SimpleNamespace(name="big_tool", description=long, parameters={})]
    )

    app = await create_meta_app(pm, use_embeddings=False)
    await app.rebuild_index()

    doc = next(d for d in app.index._documents if d["name"] == "big_tool")
    assert doc["description"] == "z" * _DISPLAY_DESC_CHARS + "…"
    assert doc["index_text"] == "z" * _INDEX_DESC_CHARS
    assert doc["full_description"] == long


async def test_get_schema_returns_full_description():
    """get_schema は表示用に切られない full_description を返す。"""
    pm = _build_mock_proxy_manager()
    long = "q" * 700
    pm._proxies["big"] = _build_mock_proxy(
        [
            SimpleNamespace(
                name="big_tool", description=long, parameters={"type": "object"}
            )
        ]
    )

    app = await create_meta_app(pm, use_embeddings=False)
    await app.rebuild_index()

    schema = app.index.get_schema("big", "big_tool")
    assert schema is not None
    assert schema["description"] == long


async def test_search_tools_servers_map_lists_only_result_servers():
    """search_tools の servers は結果に現れたサーバーだけを（説明付きで）含む。"""
    pm = _build_mock_proxy_manager()
    pm.server_description = MagicMock(
        side_effect=lambda s: {
            "filesystem": "ローカル FS",
            "fetch": "Web 取得",
        }.get(s, "")
    )
    pm._proxies["filesystem"] = _build_mock_proxy(
        [SimpleNamespace(name="file_read", description="Read files", parameters={})]
    )
    pm._proxies["fetch"] = _build_mock_proxy(
        [SimpleNamespace(name="fetch_url", description="Fetch a URL", parameters={})]
    )

    app = await create_meta_app(pm, use_embeddings=False)
    await app.rebuild_index()

    data = json.loads(await app.meta_tools.search_tools("file", top_k=5))
    assert {r["server"] for r in data["results"]} == {"filesystem"}
    # 結果に出たサーバーのみ。未ヒットの fetch は含めない。
    assert data["servers"] == {"filesystem": "ローカル FS"}


async def test_rebuild_reflects_full_description_change():
    """回帰(#003): index_text 不変でも full_description の変更は rebuild に反映される。

    raw 説明の 600 字以降だけを v1→v2 に変える ⇒ index_text（raw[:400]）も
    表示 description（raw[:600]+"…"）も不変。旧ハッシュは index_text のみを
    対象にしていたため short-circuit し、get_schema が古い full_description を
    serving し続けた。ハッシュ key に full_description を含めて修正。
    """
    pm = _build_mock_proxy_manager()
    base = "x" * 650
    pm._proxies["docs"] = _build_mock_proxy(
        [SimpleNamespace(name="doc_tool", description=base + "FULL-v1", parameters={})]
    )

    app = await create_meta_app(pm, use_embeddings=False)
    await app.rebuild_index()
    assert app.index.get_schema("docs", "doc_tool")["description"] == base + "FULL-v1"

    pm._proxies["docs"].list_tools = AsyncMock(
        return_value=[
            SimpleNamespace(name="doc_tool", description=base + "FULL-v2", parameters={})
        ]
    )
    await app.rebuild_index()

    assert app.index.get_schema("docs", "doc_tool")["description"] == base + "FULL-v2"


async def test_rebuild_reflects_display_description_change():
    """回帰(#003): index_text 不変でも表示 description の変更は search 結果に反映される。

    raw 説明の 400 字以降だけを変える ⇒ index_text（raw[:400]）は不変だが
    表示 description は変化する。旧ハッシュは index_text のみ対象だったため
    short-circuit し、search 結果が古い文面を返し続けた。
    """
    pm = _build_mock_proxy_manager()
    base = "y" * 400
    pm._proxies["docs"] = _build_mock_proxy(
        [
            SimpleNamespace(
                name="disp_tool", description=base + "DISPLAY-v1", parameters={}
            )
        ]
    )

    app = await create_meta_app(pm, use_embeddings=False)
    await app.rebuild_index()

    async def _search_desc() -> str:
        data = json.loads(await app.meta_tools.search_tools("disp_tool", top_k=5))
        return next(r for r in data["results"] if r["name"] == "disp_tool")["description"]

    assert await _search_desc() == base + "DISPLAY-v1"

    pm._proxies["docs"].list_tools = AsyncMock(
        return_value=[
            SimpleNamespace(
                name="disp_tool", description=base + "DISPLAY-v2", parameters={}
            )
        ]
    )
    await app.rebuild_index()

    assert await _search_desc() == base + "DISPLAY-v2"


class TestMetaIntegration:
    """End-to-end tests for the /mcp-meta endpoint."""

    def test_mcp_meta_endpoint_exists(self, client):
        """GET /mcp-meta returns non-404. Streamable HTTP returns 406
        without proper Accept header — 406 proves the route exists."""
        r = client.get("/mcp-meta/")
        # 406 Not Acceptable means the endpoint exists (without correct Accept)
        assert r.status_code != 404

    def test_mcp_meta_has_expected_tools(self, client):
        """Meta app exposes 3 tools: search_tools, execute_tool, get_schema."""
        parsed = _post_tools_list(client)
        tools = parsed["result"]["tools"]
        assert len(tools) == 3
        names = {t["name"] for t in tools}
        assert names == {"search_tools", "execute_tool", "get_schema"}

    def test_get_schema_tool_returns_full_schema(self, client):
        """get_schema は完全な inputSchema（パラメータ型付き）を返す。"""
        parsed = _call_tool(
            client,
            "get_schema",
            {"server": "filesystem", "tool_name": "file_read"},
            "gs-endpoint",
        )
        data = json.loads(_get_text_content(parsed))
        assert data["name"] == "file_read"
        assert data["inputSchema"]["properties"]["path"]["type"] == "string"

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

    def _search(self, client, query, tags, tool_id):
        request_tags.set(tags)
        try:
            parsed = _call_tool(client, "search_tools", {"query": query, "top_k": 10}, tool_id)
        finally:
            request_tags.set(None)
        return json.loads(_get_text_content(parsed))

    def test_matching_tag_keeps_server_results(self, client):
        """Requesting 'dev' keeps filesystem tools in search results."""
        self._set_server_tags(client)
        data = self._search(client, "file", ["dev"], "t1")
        assert {r["server"] for r in data["results"]} == {"filesystem"}

    def test_non_matching_tag_filters_server_out(self, client):
        """Issue #1: requesting 'librarian' must not block multi-tag servers —
        but a server tagged only ['dev'] is filtered out."""
        self._set_server_tags(client)
        data = self._search(client, "file", ["librarian"], "t2")
        servers = {r["server"] for r in data.get("results", [])}
        assert "filesystem" not in servers  # [dev] only


class TestLiveProxyListing:
    """execute_tool reads from the live proxy manager, not the (possibly
    stale / partially rebuilt) index."""

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

    def test_available_servers_capped(self, client):
        """案E: available_servers は全列挙せず先頭10件 + 残数サマリに留まる。"""
        pm = client.app.state.proxy_manager
        for i in range(15):
            pm._proxies[f"pad{i:02d}"] = _build_mock_proxy([])
        parsed = _call_tool(
            client,
            "execute_tool",
            {"server": "ghost", "tool_name": "whatever", "arguments": {}},
            "t-ghost-cap",
        )
        avail = json.loads(_get_text_content(parsed))["available_servers"]
        # 既存4 + pad15 = 19 サーバー → 先頭10件 + "... and 9 more"
        assert len(avail) == 11
        assert avail[-1] == "... and 9 more"

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


class TestCatalogBaking:
    """search_tools の description にサーバーカタログを焼き込む（発見可能性改善）。
    モデルが search_tools を自発的に叩かなくても、登録済みサーバーと一行概要を
    常時見られるようにする。"""

    async def _desc(self, meta_app) -> str:
        tool = await meta_app.mcp.get_tool("search_tools")
        assert tool is not None
        return tool.description

    def test_description_reaches_wire(self, client):
        """tools/list の search_tools description にカタログが乗る。"""
        parsed = _post_tools_list(client)
        st = next(t for t in parsed["result"]["tools"] if t["name"] == "search_tools")
        assert "Registered servers:" in st["description"]
        assert "- filesystem: tools: file_read, file_write" in st["description"]

    async def test_description_mutation_reaches_wire(self, meta_app):
        """description を設定 → rebuild で search_tools.description に反映される。"""
        pm = meta_app.state.proxy_manager
        pm.server_description.side_effect = lambda name: f"{name} server"
        await meta_app.state.meta_app.rebuild_index()
        desc = await self._desc(meta_app.state.meta_app)
        assert "Registered servers:" in desc
        assert "- filesystem: filesystem server" in desc

    async def test_catalog_empty(self):
        """proxy 0 件: description は base のみ、search_tools 自体は呼べる。"""
        pm = _build_mock_proxy_manager()
        m = await create_meta_app(pm)
        base = m.base_descriptions["search_tools"]
        await m.rebuild_index()
        tool = await m.mcp.get_tool("search_tools")
        assert tool is not None
        assert tool.description == base
        out = await m.meta_tools.search_tools("anything")
        assert "message" in json.loads(out)  # 呼べる（結果 0 件でもエラーにならない）

    async def test_catalog_fallback_tool_names(self, meta_app):
        """description 無しサーバーは tool 名ベースの行になる。"""
        pm = meta_app.state.proxy_manager
        pm.server_description.side_effect = lambda name: ""
        await meta_app.state.meta_app.rebuild_index()
        desc = await self._desc(meta_app.state.meta_app)
        assert "- filesystem: tools: file_read, file_write" in desc

    async def test_catalog_reflects_new_server(self, meta_app):
        """rebuild 後に新しいサーバーがカタログへ反映される。"""
        pm = meta_app.state.proxy_manager
        pm._proxies["fresh-server"] = _build_mock_proxy(
            [SimpleNamespace(name="fresh_tool", description="", parameters={})]
        )
        await meta_app.state.meta_app.rebuild_index()
        desc = await self._desc(meta_app.state.meta_app)
        assert "- fresh-server: tools: fresh_tool" in desc

    async def test_catalog_length_cap(self, meta_app):
        """多数サーバーでも description は base + 1200 字 + 余白に収まる。"""
        pm = meta_app.state.proxy_manager
        for i in range(50):
            pm._proxies[f"srv{i:03d}"] = _build_mock_proxy(
                [SimpleNamespace(name=f"t{i}", description="", parameters={})]
            )
        m = meta_app.state.meta_app
        base = m.base_descriptions["search_tools"]
        await m.rebuild_index()
        desc = await self._desc(m)
        assert len(desc) <= len(base) + 1200 + 80

    async def test_catalog_idempotent(self, meta_app):
        """2 回 rebuild しても base description が複製されない。"""
        m = meta_app.state.meta_app
        base = m.base_descriptions["search_tools"]
        await m.rebuild_index()
        first = await self._desc(m)
        await m.rebuild_index()
        second = await self._desc(m)
        assert first == second
        assert second.count(base) == 1


# ── ②④ search_tools 圧縮 / get_schema（設計 02-04 §5）────────────────────────


def _rich_schema() -> dict:
    """圧縮契約の検証用 schema。短/長の enum・default と深いネストを持つ。

    呼ぶたびに新しい dict を返す（非破壊テストで元 dict と比較するため）。
    """
    return {
        "type": "object",
        "required": ["mode"],
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["a", "b", "c"],
                "description": "short enum " + "m" * 150,
            },
            "many_enum": {
                "type": "string",
                "enum": [f"e{i}" for i in range(6)],
                "description": "too many enum values " + "n" * 150,
            },
            "wide_enum": {
                "type": "string",
                "enum": ["x" * 30, "y" * 30, "z" * 30],
                "description": "long enum values " + "w" * 150,
            },
            "count": {
                "type": "integer",
                "default": 7,
                "description": "short numeric default " + "c" * 150,
            },
            "flag": {
                "type": "boolean",
                "default": True,
                "description": "short bool default " + "f" * 150,
            },
            "label": {
                "type": "string",
                "default": "ok",
                "description": "short string default " + "l" * 150,
            },
            "custom": {
                "type": "string",
                "default": "d" * 50,
                "description": "long default " + "p" * 150,
            },
            "options": {
                "type": "object",
                "description": "nested object " + "o" * 150,
                "properties": {"inner": {"type": "string", "description": "inner"}},
            },
            "tags": {
                "type": "array",
                "description": "array " + "t" * 150,
                "items": {"type": "string", "description": "d", "enum": ["p", "q"]},
            },
        },
    }


def _big_schema(n: int = 20) -> dict:
    """応答サイズ回帰用: パラメータ数が多く、各々長い説明を持つ schema。"""
    return {
        "type": "object",
        "properties": {
            f"param_{i}": {"type": "string", "description": "d" * 200}
            for i in range(n)
        },
    }


async def _app_from(servers: dict):
    """{server: [tools]} から埋め込み無しの meta app を構築して返す。"""
    pm = _build_mock_proxy_manager()
    for name, tools in servers.items():
        pm._proxies[name] = _build_mock_proxy(tools)
    app = await create_meta_app(pm, use_embeddings=False)
    await app.rebuild_index()
    return pm, app


class TestCompactSchemaContract:
    """② search_tools の inputSchema は名前/型/必須へ圧縮され、完全な仕様は
    get_schema に逃がす（設計 02-04 §5）。"""

    async def test_inputschema_compaction_contract(self):
        schema = _rich_schema()
        _pm, app = await _app_from(
            {
                "srv": [
                    SimpleNamespace(
                        name="rich_tool", description="Rich tool", parameters=schema
                    )
                ]
            }
        )

        data = json.loads(await app.meta_tools.search_tools("rich_tool", top_k=5, detail="all"))
        result = next(r for r in data["results"] if r["name"] == "rich_tool")
        compact = result["inputSchema"]

        # type / required は保持される
        assert compact["type"] == "object"
        assert compact["required"] == ["mode"]

        props = compact["properties"]
        assert set(props) == set(schema["properties"])
        # 各 property は type を持ち、description を持たない
        for name, prop in props.items():
            assert "type" in prop, name
            assert "description" not in prop, name

        # 短い enum は残り、長い enum（個数 or 連結長）は落ちる
        assert props["mode"]["enum"] == ["a", "b", "c"]
        assert "enum" not in props["many_enum"]
        assert "enum" not in props["wide_enum"]

        # 短い default（数値・真偽・40 字以下の文字列）は残り、長いものは落ちる
        assert props["count"]["default"] == 7
        assert props["flag"]["default"] is True
        assert props["label"]["default"] == "ok"
        assert "default" not in props["custom"]

        # 深いネストは {"type": ...} に畳まれる
        assert props["options"] == {"type": "object"}
        assert props["tags"]["items"] == {"type": "string"}


def _searchable(n: int) -> list:
    """クエリ "search tool" が n 件ヒットするツール群。"""
    return [
        SimpleNamespace(
            name=f"tool_{i}",
            description=f"search tool number {i}",
            parameters={"type": "object", "properties": {"q": {"type": "string"}}},
        )
        for i in range(n)
    ]


class TestSearchDetailLevels:
    """MCP 公式 3 層準拠: 既定 brief / schema は上位1件のみ / all は従来互換。"""

    async def test_brief_is_default_and_has_no_schema(self):
        _pm, app = await _app_from({"srv": _searchable(3)})
        data = json.loads(await app.meta_tools.search_tools("search tool"))
        assert data["results"]
        for r in data["results"]:
            assert "inputSchema" not in r
            assert set(r) == {
                "server",
                "name",
                "description",
                "search_desc",
                "score",
            }
        # note は get_schema へ誘導する
        assert "get_schema" in data["note"]

    async def test_default_detail_equals_brief(self):
        _pm, app = await _app_from({"srv": _searchable(3)})
        assert await app.meta_tools.search_tools(
            "search tool"
        ) == await app.meta_tools.search_tools("search tool", detail="brief")

    async def test_schema_attaches_schema_only_to_top_hit(self):
        _pm, app = await _app_from({"srv": _searchable(3)})
        data = json.loads(
            await app.meta_tools.search_tools("search tool", detail="schema")
        )
        assert len(data["results"]) >= 2
        assert "inputSchema" in data["results"][0]
        # 圧縮形（param の description は落ちる）
        for prop in data["results"][0]["inputSchema"]["properties"].values():
            assert "description" not in prop
        for r in data["results"][1:]:
            assert "inputSchema" not in r

    async def test_all_keeps_legacy_shape(self):
        _pm, app = await _app_from({"srv": _searchable(3)})
        data = json.loads(await app.meta_tools.search_tools("search tool", detail="all"))
        assert all("inputSchema" in r for r in data["results"])
        # all は従来互換（検索応答の旧 note を維持し、search_desc は載せない）
        assert data["note"] == (
            "inputSchema は要約です。完全な仕様は get_schema(server, tool_name) で"
            "取得してください。"
        )
        assert all("search_desc" not in r for r in data["results"])

    async def test_default_top_k_is_three(self):
        _pm, app = await _app_from({"srv": _searchable(5)})
        data = json.loads(await app.meta_tools.search_tools("search tool"))
        assert len(data["results"]) == 3
        # 明示指定は従来どおり効く
        five = json.loads(await app.meta_tools.search_tools("search tool", top_k=5))
        assert len(five["results"]) == 5


class TestSearchDescField:
    """search_desc: tool_search_desc（日本語1文）を brief に載せる。"""

    async def test_search_desc_carried_in_brief(self):
        pm = _build_mock_proxy_manager()
        pm._proxies["srv"] = _build_mock_proxy(
            [
                SimpleNamespace(
                    name="alpha_tool",
                    description="Alpha tool",
                    parameters={"type": "object"},
                )
            ]
        )
        pm.server_tool_search_desc = MagicMock(
            return_value={"alpha_tool": "アルファの説明。"}
        )
        app = await create_meta_app(pm, use_embeddings=False)
        await app.rebuild_index()

        data = json.loads(await app.meta_tools.search_tools("alpha_tool"))
        r = next(x for x in data["results"] if x["name"] == "alpha_tool")
        assert r["search_desc"] == "アルファの説明。"

    async def test_search_desc_empty_when_absent(self):
        _pm, app = await _app_from({"srv": _searchable(2)})
        data = json.loads(await app.meta_tools.search_tools("search tool"))
        assert all(r["search_desc"] == "" for r in data["results"])


class TestGetSchemaCompact:
    """get_schema(compact=True) は検索応答と同じ圧縮スキーマを返す。"""

    async def test_compact_returns_compressed_schema(self):
        _pm, app = await _app_from(
            {
                "srv": [
                    SimpleNamespace(
                        name="rich_tool",
                        description="Rich",
                        parameters=_rich_schema(),
                    )
                ]
            }
        )
        full = json.loads(await app.meta_tools.get_schema("srv", "rich_tool"))
        comp = json.loads(
            await app.meta_tools.get_schema("srv", "rich_tool", compact=True)
        )
        # 既定 (compact=False) は完全形
        assert full["inputSchema"]["properties"]["mode"]["description"].startswith(
            "short enum"
        )
        # compact は圧縮形で、完全形より小さい
        assert comp["inputSchema"]["required"] == ["mode"]
        for prop in comp["inputSchema"]["properties"].values():
            assert "description" not in prop
        assert len(json.dumps(comp, ensure_ascii=False)) < len(
            json.dumps(full, ensure_ascii=False)
        )

    async def test_compact_does_not_mutate_index(self):
        _pm, app = await _app_from(
            {
                "srv": [
                    SimpleNamespace(
                        name="rich_tool",
                        description="Rich",
                        parameters=_rich_schema(),
                    )
                ]
            }
        )
        await app.meta_tools.get_schema("srv", "rich_tool", compact=True)
        still = app.index.get_schema("srv", "rich_tool")
        assert still["inputSchema"]["properties"]["mode"]["description"].startswith(
            "short enum"
        )


class TestSearchToolsNonDestructive:
    """search_tools の圧縮は応答コピーに限定し、索引 doc を汚染しない。"""

    async def test_search_tools_does_not_mutate_index(self):
        tool = SimpleNamespace(
            name="rich_tool", description="Rich tool", parameters=_rich_schema()
        )
        _pm, app = await _app_from({"srv": [tool]})

        first = await app.meta_tools.search_tools("rich_tool", top_k=5)

        # 索引 doc / live ツールの schema は完全形のまま
        full = app.index.get_schema("srv", "rich_tool")
        assert full is not None
        assert full["inputSchema"]["properties"]["mode"]["description"].startswith(
            "short enum"
        )
        assert (
            full["inputSchema"]["properties"]["options"]["properties"]["inner"]["type"]
            == "string"
        )
        assert "description" in tool.parameters["properties"]["mode"]

        # 同じツールを 2 回検索しても結果は一致する
        second = await app.meta_tools.search_tools("rich_tool", top_k=5)
        assert first == second


class TestGetSchemaTool:
    """③ get_schema ツール: 完全な仕様を返し、未知/範囲外は not found。"""

    async def test_returns_full_schema_and_untruncated_description(self):
        long_desc = "q" * 700
        _pm, app = await _app_from(
            {
                "big": [
                    SimpleNamespace(
                        name="big_tool",
                        description=long_desc,
                        parameters=_rich_schema(),
                    )
                ]
            }
        )

        data = json.loads(await app.meta_tools.get_schema("big", "big_tool"))
        assert data["name"] == "big_tool"
        # full_description（切り詰めなし）— 表示用 description は 600 字 + "…"
        assert data["description"] == long_desc
        assert "…" not in data["description"]
        # 完全な inputSchema（param の description 付き）
        assert data["inputSchema"]["properties"]["mode"]["description"].startswith(
            "short enum"
        )

    async def test_unknown_and_empty_args_return_not_found(self):
        _pm, app = await _app_from(
            {
                "filesystem": [
                    SimpleNamespace(
                        name="file_read",
                        description="Read",
                        parameters={"type": "object"},
                    )
                ]
            }
        )
        mt = app.meta_tools
        for server, tool_name in [
            ("filesystem", "nope"),
            ("nope", "file_read"),
            ("", "file_read"),
            ("filesystem", ""),
        ]:
            data = json.loads(await mt.get_schema(server, tool_name))
            assert data["message"] == "Tool not found", (server, tool_name)

    async def test_tag_filtered_server_returns_not_found(self):
        pm, app = await _app_from(
            {
                "filesystem": [
                    SimpleNamespace(
                        name="file_read",
                        description="Read",
                        parameters={"type": "object"},
                    )
                ]
            }
        )
        pm.server_tags.side_effect = lambda name: {"filesystem": ["dev"]}.get(name, [])
        request_tags.set(["librarian"])
        try:
            data = json.loads(await app.meta_tools.get_schema("filesystem", "file_read"))
        finally:
            request_tags.set(None)
        assert data["message"] == "Tool not found"

    async def test_server_name_resolved_case_insensitively(self):
        _pm, app = await _app_from(
            {
                "filesystem": [
                    SimpleNamespace(
                        name="file_read",
                        description="Read",
                        parameters={"type": "object"},
                    )
                ]
            }
        )
        data = json.loads(await app.meta_tools.get_schema("FileSystem", "file_read"))
        assert data["name"] == "file_read"
        assert data["inputSchema"] == {"type": "object"}


class TestSearchResponseSize:
    """④ 多数パラメータのツールでも圧縮後の応答が生 schema より十分小さい。"""

    async def test_compaction_shrinks_schema_and_response(self):
        from mcp_hub.meta_provider import _compact_input_schema

        big = _big_schema()
        raw_size = len(json.dumps(big, ensure_ascii=False))
        compact_size = len(json.dumps(_compact_input_schema(big), ensure_ascii=False))
        assert compact_size < raw_size * 0.5

        _pm, app = await _app_from(
            {
                "big": [
                    SimpleNamespace(
                        name="big_tool",
                        description="Big tool",
                        parameters=_big_schema(),
                    )
                ]
            }
        )
        out = await app.meta_tools.search_tools("big_tool", top_k=5, detail="all")
        assert len(out) < raw_size * 0.5


class TestSearchNoteFlag:
    """⑤ note は成功応答には付き、0 件応答には付かない。"""

    async def test_note_present_on_success_absent_on_zero_results(self):
        _pm, app = await _app_from(
            {
                "srv": [
                    SimpleNamespace(
                        name="rich_tool",
                        description="Rich",
                        parameters={"type": "object"},
                    )
                ]
            }
        )
        ok = json.loads(await app.meta_tools.search_tools("rich_tool", top_k=5))
        assert "note" in ok

        empty = json.loads(await app.meta_tools.search_tools("zzzznope", top_k=5))
        assert "message" in empty
        assert "note" not in empty


class TestCompactUnionType:
    """_compact_property は anyOf/oneOf の null 以外の型が1種類なら type を残す。"""

    def test_anyof_nullable_keeps_type(self):
        from mcp_hub.meta_provider import _compact_property

        prop = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
        assert _compact_property(prop) == {"type": "string", "default": None}

    def test_oneof_nullable_keeps_type(self):
        from mcp_hub.meta_provider import _compact_property

        prop = {"oneOf": [{"type": "null"}, {"type": "integer"}]}
        assert _compact_property(prop) == {"type": "integer"}

    def test_ambiguous_and_malformed_keep_no_type(self):
        from mcp_hub.meta_provider import _compact_property

        # 複数種類 → 現状維持（type なし）
        assert _compact_property({"anyOf": [{"type": "string"}, {"type": "integer"}]}) == {}
        # 0種類（null のみ）→ type なし
        assert _compact_property({"anyOf": [{"type": "null"}]}) == {}
        # 不正形でも例外を出さない
        assert _compact_property({"anyOf": "nope"}) == {}
        assert _compact_property({"anyOf": [None, 3]}) == {}
        # 要素側 type が配列でも null 以外が1種類なら残す
        assert _compact_property({"anyOf": [{"type": ["string", "null"]}]}) == {
            "type": "string"
        }

    def test_compact_input_schema_applies_union_type_at_top_level(self):
        from mcp_hub.meta_provider import _compact_input_schema

        # anyOf の null 以外が1種類 → トップレベルでも type を採る
        assert _compact_input_schema(
            {"anyOf": [{"type": "object"}, {"type": "null"}], "required": ["a"]}
        ) == {"type": "object", "required": ["a"]}
        # 複数種類 → type なし（誤った型を出さない）
        assert (
            _compact_input_schema({"anyOf": [{"type": "object"}, {"type": "string"}]})
            == {}
        )

    def test_branch_without_type_disables_salvage(self):
        from mcp_hub.meta_provider import _compact_property

        # $ref 要素は型を判断できない → 救済せず捏造を防ぐ
        assert _compact_property({"anyOf": [{"type": "string"}, {"$ref": "#/x"}]}) == {}
        # enum のみの要素も同様
        assert _compact_property({"anyOf": [{"type": "string"}, {"enum": [1, 2]}]}) == {}
        # 既存の救済ケース（null は type を持つので除外）は壊さない
        assert _compact_property({"anyOf": [{"type": "string"}, {"type": "null"}]}) == {
            "type": "string"
        }


class TestZeroResultShape:
    """0 件応答も hits と同形（results/servers を持つ）で KeyError を防ぐ。"""

    async def test_zero_result_has_results_list(self):
        _pm, app = await _app_from(
            {
                "srv": [
                    SimpleNamespace(
                        name="rich_tool",
                        description="Rich",
                        parameters={"type": "object"},
                    )
                ]
            }
        )
        empty = json.loads(await app.meta_tools.search_tools("zzzznope", top_k=5))
        assert empty["results"] == []
        assert empty["servers"] == {}
        assert "message" in empty
        assert "hint" in empty
        # 多言語キーワード併記の誘導が hint に含まれる
        assert "室温 temperature" in empty["hint"]
