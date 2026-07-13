# Progressive Discovery Meta-Tools — Implementation Plan

> **For agentic workers:** Execute steps sequentially. Each commit is self-contained.

**Goal:** Add a `/mcp-meta` endpoint with 3 meta-tools (search_tools, get_tool_schema, execute_tool) using BM25 search, reducing context pressure by 95-99%.

**Architecture:** A separate FastMCP instance at `/mcp-meta` coexists with the existing `/mcp`. No mode switching — the user picks which endpoint to connect their LLM to.

**Tech Stack:** Python 3.12+, FastMCP 3.4+ (<3.5.0), rank_bm25, pytest

---

## Design

```
Client → /mcp-meta (streamable-http)
    ↓
FastMCP("MCP Hub Meta")
    ├── search_tools(query, top_k=10) → BM25 search
    ├── get_tool_schema(server, tool_name) → full inputSchema
    └── execute_tool(server, tool_name, arguments) → proxy call

Existing /mcp — unchanged (full direct access)
```

**BM25 Search Engine:**
- Index rebuilt on startup and after proxy add/remove
- Each document = `{server_name} {tool_name} {description}`
- Tokenized by whitespace splitting (simple, fast, no NLP deps)
- Returns top_k results with server name, tool name, description, and BM25 score

**Tag Filter Integration (future work):**
- Tag-aware filtering in `search_tools` is deferred to a follow-up PR.
- `ToolIndex` does not yet store server tags — full-text search across all servers for now.

---

## Files

- Create: `src/mcp_hub/meta_provider.py` — MetaProvider + BM25 engine (~250 lines)
- Modify: `src/mcp_hub/main.py` — mount second FastMCP at `/mcp-meta` (~30 lines)
- Modify: `pyproject.toml` — add rank_bm25 dependency
- Create: `tests/test_meta.py` — 12 tests

---

## Task 1: Create BM25 Search Engine

**File:** `src/mcp_hub/meta_provider.py`

```python
"""
Progressive Discovery meta-tools with BM25 search.
Exposes 3 tools instead of all child server tools.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools import Tool
from rank_bm25 import BM25Okapi

logger = logging.getLogger(__name__)

MCP_HUB_TAGS_HEADER = "X-MCP-Hub-Tags"


class ToolIndex:
    """BM25 search over all proxied tools. Thread-safe via asyncio.Lock."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._documents: list[dict] = []  # [{server, name, description, inputSchema}, ...]
        self._bm25: BM25Okapi | None = None
        self._corpus: list[list[str]] = []

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return text.lower().split()

    async def rebuild(self, documents: list[dict]) -> None:
        """Rebuild index from pre-built tool documents.
        Each document: {server, name, description, inputSchema}.
        Caller is responsible for building the document list.
        """
        async with self._lock:
            self._documents = documents
            self._corpus = [
                self._tokenize(f"{d['server']} {d['name']} {d.get('description', '')}")
                for d in documents
            ]
            self._bm25 = BM25Okapi(self._corpus) if self._corpus else None
        logger.info("ToolIndex rebuilt: %d tools indexed", len(documents))

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        """Search tools by keyword. Returns list of {server, name, description, score}.
        Read-only — does not modify shared state, safe without lock."""
        if not self._bm25 or not self._corpus:
            return []
        tokens = self._tokenize(query)
        scores = self._bm25.get_scores(tokens)
        # Local snapshot for thread safety
        docs = self._documents
        ranked = sorted(
            range(len(scores)), key=lambda i: scores[i], reverse=True
        )
        results = []
        for idx in ranked[:top_k]:
            if scores[idx] <= 0:
                break
            doc = docs[idx]
            results.append({
                "server": doc["server"],
                "name": doc["name"],
                "description": doc.get("description", ""),
                "score": round(float(scores[idx]), 4),
            })
        return results

    def get_schema(self, server: str, tool_name: str) -> dict | None:
        """Get full inputSchema for a tool. Read-only, safe without lock."""
        for doc in self._documents:
            if doc["server"] == server and doc["name"] == tool_name:
                return {
                    "name": doc["name"],
                    "description": doc.get("description", ""),
                    "server": server,
                    "inputSchema": doc.get("inputSchema", {}),
                }
        return None

    def list_servers(self) -> list[str]:
        """List all indexed server names."""
        return sorted(set(d["server"] for d in self._documents))
```

---

## Task 2: Create MetaProvider with 3 Tools

**File:** `src/mcp_hub/meta_provider.py` (continued)

```python
class MetaTools:
    """Manages meta-tool definitions and execution."""

    def __init__(
        self,
        tool_index: ToolIndex,
        execute_tool_fn: Callable[[str, str, dict], Any],
    ):
        self._index = tool_index
        self._execute_tool = execute_tool_fn

    async def search_tools(self, query: str, top_k: int = 10) -> str:
        """Search across all upstream server tools by keyword or capability.

        Use this FIRST to discover available tools before calling get_tool_schema or execute_tool.

        Args:
            query: Natural language description of what you want to do (e.g. "read files", "search web")
            top_k: Max results to return (default 10)
        """
        results = self._index.search(query, top_k)
        if not results:
            return json.dumps({"message": "No matching tools found", "hint": "Try broader keywords or check server connections."}, ensure_ascii=False, indent=2)
        return json.dumps({"results": results}, ensure_ascii=False, indent=2)

    async def get_tool_schema(self, server: str, tool_name: str) -> str:
        """Get the full input schema for a specific tool on a specific server.

        ALWAYS call this after search_tools to learn the exact parameters required
        before calling execute_tool.

        Args:
            server: Server name from search_tools results
            tool_name: Tool name from search_tools results
        """
        schema = self._index.get_schema(server, tool_name)
        if schema is None:
            return json.dumps({"error": f"Tool '{tool_name}' not found on server '{server}'"})
        return json.dumps(schema, ensure_ascii=False, indent=2)

    async def execute_tool(self, server: str, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Execute a tool on any upstream server.

        Args:
            server: Server name from search_tools results
            tool_name: Tool name from search_tools results
            arguments: Tool parameters (use get_tool_schema first to learn the format)
        """
        try:
            result = await self._execute_tool(server, tool_name, arguments)
            return result
        except ValueError as e:
            return json.dumps({"error": str(e)})
        except Exception as e:
            return json.dumps({"error": f"Tool execution failed: {e}"})

    def get_tool_definitions(self) -> list[dict]:
        """Return tool definitions for FastMCP registration."""
        return [
            {
                "name": "search_tools",
                "description": "SEARCH FIRST. Find available tools across all servers by describing what you want to do. Returns matching tool names, descriptions, and which server they belong to. Use natural language queries like 'read files', 'search web', 'run browser'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural language query describing what you want to do"},
                        "top_k": {"type": "integer", "description": "Max results (default 10)", "default": 10},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "get_tool_schema",
                "description": "Get the full input schema (parameters and types) for a specific tool. ALWAYS call this after search_tools, before execute_tool, to learn the exact parameters needed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "server": {"type": "string", "description": "Server name from search_tools result"},
                        "tool_name": {"type": "string", "description": "Tool name from search_tools result"},
                    },
                    "required": ["server", "tool_name"],
                },
            },
            {
                "name": "execute_tool",
                "description": "Execute a specific tool on a specific server. Use get_tool_schema first to learn the correct arguments format.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "server": {"type": "string", "description": "Server name from search_tools result"},
                        "tool_name": {"type": "string", "description": "Tool name from search_tools result"},
                        "arguments": {"type": "object", "description": "Tool parameters matching the input schema from get_tool_schema"},
                    },
                    "required": ["server", "tool_name", "arguments"],
                },
            },
        ]


def create_meta_app(
    proxy_manager,  # ProxyManager instance
) -> FastMCP:
    """Create a FastMCP app with meta-tools."""
    mcp = FastMCP("MCP Hub Meta")
    index = ToolIndex()

    # Build initial index from all connected proxy tools
    async def rebuild_index():
        tools_map = await proxy_manager.list_tools()
        all_tools = []
        for server_name, tools in tools_map.items():
            for t in tools:
                all_tools.append({
                    "server": server_name,
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": getattr(t, "inputSchema", getattr(t, "parameters", {})),
                })
        index.rebuild(all_tools)

    meta = MetaTools(
        tool_index=index,
        execute_tool_fn=lambda s, t, a: proxy_manager.call_tool(s, t, a),
    )

    # Register meta tools via FastMCP tool decorator
    @mcp.tool()
    async def search_tools(query: str, top_k: int = 10) -> str:
        return await meta.search_tools(query, top_k)

    @mcp.tool()
    async def get_tool_schema(server: str, tool_name: str) -> str:
        return await meta.get_tool_schema(server, tool_name)

    @mcp.tool()
    async def execute_tool(server: str, tool_name: str, arguments: dict[str, Any]) -> Any:
        return await meta.execute_tool(server, tool_name, arguments)

    # Rebuild index after server changes
    mcp._index = index
    mcp._meta = meta
    mcp.rebuild_index = rebuild_index

    return mcp
```

---

## Task 3: Integrate with main.py

**File:** `src/mcp_hub/main.py`

The `/mcp-meta` mount MUST mirror the existing `/mcp` pattern exactly:
`http_app()` → discover `StreamableHTTPASGIApp` → `FastMCPStreamableHTTPSessionManager` → `lifespan_manager()` + `sm.run()`.

```python
# === After existing /mcp mount setup (around line 93 in main.py) ===

from .meta_provider import create_meta_app

# Create and mount meta endpoint (same pattern as /mcp)
meta_mcp = create_meta_app(proxy_manager)
meta_http = meta_mcp.http_app(transport="streamable-http", path="/")
app.mount("/mcp-meta", meta_http)

# Discover StreamableHTTPASGIApp for meta (same pattern as /mcp)
meta_inner_app: StreamableHTTPASGIApp | None = None
for route in meta_http.routes:
    if isinstance(getattr(route, "endpoint", None), StreamableHTTPASGIApp):
        meta_inner_app = route.endpoint
        break

if meta_inner_app is None:
    raise RuntimeError("Could not find StreamableHTTPASGIApp in meta routes")

meta_sm = FastMCPStreamableHTTPSessionManager(
    app=meta_mcp._mcp_server,
)
meta_inner_app.session_manager = meta_sm

# === Rebuild index after load_all() ===
await proxy_manager.load_all()
await meta_mcp.rebuild_index()

# === Lifespan: BOTH session managers run together ===
# Python 3.12+ parenthesized context managers
async with (
    mcp_server._lifespan_manager(),
    sm.run(),
    meta_mcp._lifespan_manager(),
    meta_sm.run(),
):
    proxy_manager.start_health_monitor()
    logger.info("MCP Hub started on %s:%s", HOST, PORT)
    yield

# Shutdown: stop health monitor (existing pattern continues after yield)
await proxy_manager.stop_health_monitor()
```

**IMPORTANT:** The `async with` block combines all four context managers into a single `yield` point. Python 3.12+'s parenthesized context managers (`async with (a, b, c, d):`) is required — this ensures all lifespan managers run together and clean up together.

**Key points:**
- `meta_mcp._mcp_server` and `meta_mcp._lifespan_manager()` follow the same private API pattern as the main MCP server (documented with NOTE comments about FastMCP internal API)
- `meta_http_app` uses the same `transport="streamable-http"` and `path="/"` (root because mount point is `/mcp-meta`)
- After `load_all()`, `await meta_mcp.rebuild_index()` populates the BM25 index
- On register/unregister/refresh proxy events, call `meta_mcp.rebuild_index()` (see Task 4)

---

## Task 4: Wire rebuild_index to proxy changes

**File:** `src/mcp_hub/proxy_manager.py`

Add a callback system. When servers are registered/unregistered/refreshed, notify the meta index to rebuild.

```python
# In ProxyManager.__init__:
self._on_change_callbacks: list[Callable] = []

# Add method:
def on_change(self, callback: Callable) -> None:
    self._on_change_callbacks.append(callback)

# In register_server, unregister_server, refresh_server:
# After state mutation, call:
for cb in self._on_change_callbacks:
    await cb()
```

**File:** `src/mcp_hub/main.py`

```python
proxy_manager.on_change(meta_mcp.rebuild_index)
```

---

## Task 5: ProxyManager.call_tool — already exists ✅

`call_tool(server_name, tool_name, arguments)` already exists in `proxy_manager.py` L164-170. No changes needed.

---

## Task 6: Compression Toggle (WebUI + API)

### Design

Meta-tools mode is optional and toggleable. When disabled, MCP-Hub exposes all tools directly via `/mcp` (current behavior). When enabled, only 3 meta-tools are exposed via `/mcp-meta`. The mode is stored in `hub.config.json` as `"meta_mode": true/false` and toggled from WebUI.

### Files:
- Modify: `src/mcp_hub/admin_router.py` (settings endpoint)
- Modify: `src/mcp_hub/static/index.html` (toggle UI)
- Modify: `src/mcp_hub/store.py` (meta_mode field in config)

### Step 6.1: Add meta_mode to hub.config.json schema

```python
# store.py DEFAULT_CONFIG — add top-level field
DEFAULT_CONFIG = {
    "version": 1,
    "log_level": "info",
    "meta_mode": False,  # ← new: Progressive Discovery off by default
    "mcpServers": { ... }
}
```

### Step 6.2: Add set_meta_mode to JsonStore (atomic read-modify-write)

```python
# store.py — add to JsonStore
async def set_meta_mode(self, enabled: bool) -> None:
    """Atomically update meta_mode. Uses the same lock-protected
    _add_or_update pattern to prevent read-modify-write races."""
    async with self._lock:
        data = await self._read()
        data["meta_mode"] = enabled
        await self._write(data)
```

### Step 6.3: Add settings API endpoints

```python
# admin_router.py
@router.get("/admin/api/settings")
async def get_settings():
    registry = _get_registry()
    data = await registry._read()
    return {
        "meta_mode": data.get("meta_mode", False),
    }

@router.patch("/admin/api/settings")
async def update_settings(body: dict):
    registry = _get_registry()
    if "meta_mode" in body:
        await registry.set_meta_mode(bool(body["meta_mode"]))
    data = await registry._read()
    return {"meta_mode": data.get("meta_mode", False)}
```

### Step 6.4: Connection URL switching in WebUI

/mcp-meta is always mounted (Task 3 handles the lifespan). The toggle controls
which connection URL the WebUI recommends. When `meta_mode` is on, the
connection URL switches to `/mcp-meta` with the 3 meta-tools. When off,
the URL shows `/mcp` with all tools directly.

```javascript
// index.html — loadSettings() in DOMContentLoaded or loadServers
async function loadSettings() {
    try {
        const resp = await fetch('/admin/api/settings');
        const data = await resp.json();
        document.getElementById('metaModeToggle').checked = data.meta_mode || false;
        updateConnectionUrl(data.meta_mode);
    } catch (e) { /* settings unavailable — hide toggle */ }
}

function updateConnectionUrl(enabled) {
    const urlEl = document.getElementById('connectionUrl');
    if (enabled) {
        urlEl.textContent = `${location.origin}/mcp-meta`; // 3 meta-tools
    } else {
        urlEl.textContent = `${location.origin}/mcp`; // all tools
    }
}
```

### Step 6.5: Add WebUI toggle

```html
<!-- index.html — settings panel or header area -->
<div class="settings-bar">
    <label class="toggle-switch" title="プログレッシブ検索モード">
        <span>圧縮モード</span>
        <input type="checkbox" id="metaModeToggle" onchange="toggleMetaMode(this.checked)">
        <span class="toggle-track"></span>
        <span class="toggle-thumb"></span>
    </label>
    <span class="toggle-label">ツール数が多いときに3つの検索ツールに圧縮</span>
</div>
```

```javascript
// index.html JS
async function loadSettings() {
    const resp = await fetch('/admin/api/settings');
    const data = await resp.json();
    document.getElementById('metaModeToggle').checked = data.meta_mode;
}

async function toggleMetaMode(enabled) {
    await fetch('/admin/api/settings', {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({meta_mode: enabled})
    });
    showToast(
        enabled ? '圧縮モードを有効にしました /mcp-meta 接続URLに切替中' : '圧縮モードを無効にしました /mcp 接続URLに切替中',
        'info'
    );
}
```

### Step 6.6: Tests

```python
# tests/test_meta.py additions
- test_settings_get_meta_mode — returns default False
- test_settings_patch_meta_mode — updates meta_mode
- test_meta_app_always_available — /mcp-meta always 200 (toggle is UI-only)
```

---

## Task 7: Tests

**File:** `tests/test_meta.py`

```python
# 17 tests:

# ToolIndex tests (5):
- test_index_search_exact_match — search_tools("read file") returns filesystem tools
- test_index_search_partial_match — partial keyword match
- test_index_search_no_results — empty query returns []
- test_index_get_schema — returns full schema for known tool
- test_index_get_schema_missing — returns None for unknown tool

# MetaTools tests (4):
- test_search_tools_returns_json — returns valid JSON string
- test_get_tool_schema_missing — returns error JSON
- test_execute_tool_forwards — calls proxy_manager.call_tool correctly
- test_execute_tool_error — returns error JSON on failure

# Integration tests (3):
- test_mcp_meta_endpoint_exists — GET /mcp-meta returns 200
- test_mcp_meta_list_tools — returns exactly 3 tools
- test_index_rebuilds_on_register — index updates after registering new server

# Compression toggle tests (3):
- test_settings_get_meta_mode — returns default False
- test_settings_patch_meta_mode — updates meta_mode
- test_meta_app_always_available — /mcp-meta always 200 (toggle is UI-only)

# API endpoint tests (2):
- test_resources_endpoint — GET /{name}/resources
- test_prompts_endpoint — GET /{name}/prompts
```

---

## Task 8: Commit

```bash
git add src/mcp_hub/meta_provider.py src/mcp_hub/proxy_manager.py src/mcp_hub/main.py src/mcp_hub/admin_router.py src/mcp_hub/store.py src/mcp_hub/static/index.html pyproject.toml tests/test_meta.py
git commit -m "feat: Progressive Discovery meta-tools with BM25 search

- /mcp-meta endpoint with 3 meta-tools (search_tools, get_tool_schema, execute_tool)
- BM25 search engine indexes all proxied tools
- /mcp unchanged — full direct access still available
- WebUI toggle for compression mode (saved in hub.config.json)
- PATCH /admin/api/settings for meta_mode configuration
- Index auto-rebuilds on proxy add/remove
- Context reduction: N tools → 3 tools (95-99% token savings)"
```

---

## Verification

```bash
# All tests
pytest tests/ -v  # 62 existing + ~17 new = ~79

# Meta endpoint always accessible (mounted at startup)
curl -s http://localhost:26263/mcp-meta | head -5

# Settings API
curl http://localhost:26263/admin/api/settings  # {"meta_mode": false}
curl -X PATCH http://localhost:26263/admin/api/settings \
  -H 'Content-Type: application/json' \
  -d '{"meta_mode": true}'

# BM25 search works
# (call search_tools with test query via MCP client)

# WebUI: toggle compression switch, verify settings persist after refresh
```

## Risks

- `rank_bm25` is a new dependency. It's lightweight (single file, no sub-deps) and well-maintained.
- Meta mode toggle is UI-only (connection URL switching). /mcp-meta always available.
- `execute_tool` result format varies by server. Some servers return text, others return complex objects. JSON serialization may lose fidelity.
