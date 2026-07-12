# MCP-Hub: Config-Driven Architecture & WebUI Overhaul Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax. Each task self-contained with exact file paths.
> **Design tasks (@designer):** Visual polish, layout, interactions, responsive behavior. Copy reviewed by orchestrator afterward.

**Goal:** Replace hardcoded defaults with `hub.config.json`, add config-file-driven seed + tag filtering + connection params + WebUI management (enable/disable, tags, config editing, URL copy), visual refresh.

**Architecture:** Python 3.12+ / FastAPI / FastMCP 3.4+ / aiosqlite / Vanilla JS SPA. Config file `hub.config.json` replaces hardcoded `DEFAULT_SERVERS`. Connection-time tag filtering via query params (`/mcp?tags=`) AND HTTP headers (`X-MCP-Hub-Tags`), headers take priority.

**Tech Stack:** Python 3.12+, FastAPI, FastMCP 3.4+, aiosqlite, Vanilla JS/CSS, pytest, Playwright

---

## Root Cause Analysis

**WebUI was silently broken:** Root `/` didn't serve UI (fixed 3f651be). Seed servers used deprecated `@anthropic/mcp-server-*` → npm 404 → 0 tools → empty cards. JS gave no error feedback.

**Hardcoded defaults** in `registry.py` block customization. Config file solves this.

**Missing WebUI capabilities:** No enable/disable toggle, no tag management, no config editing, no connection URL copy.

---

## File Structure

```
src/mcp_hub/
├── __init__.py
├── main.py              # Modify: config loading, JSON logging, internal resource, tag middleware
├── config.py            # NEW: Config file loader (hub.config.json, env expansion)
├── admin_router.py      # Modify: PATCH endpoint, enable/disable, metrics, status
├── proxy_manager.py     # Modify: status tracking, connection-time tag filter, disabled exclusion
├── registry.py          # Modify: remove DEFAULT_SERVERS, seed from config
├── state.py             # Modify: metrics counters
├── env_expand.py        # NEW: ${VAR} / ${VAR:-default} expansion utility
├── static/
│   └── index.html       # WebUI SPA — MAJOR: design refresh + enable/disable + tags + config edit + URL copy
hub.config.json          # NEW: Default config file
tests/
├── __init__.py
├── conftest.py
├── test_env_expand.py
├── test_config.py
├── test_admin_api.py
└── test_tag_filter.py
```

---

## Chunk 1: Config File Foundation (BLOCKING)

### Task 1.1: Create hub.config.json

**Files:** Create `hub.config.json`

**What:** Default server definitions as config file, replacing `registry.py`'s `DEFAULT_SERVERS`.

- [ ] **Step 1: Write hub.config.json**

```json
{
  "version": 1,
  "mcpServers": {
    "fetch": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-fetch"],
      "tags": ["web"],
      "disabled": false
    },
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "tags": ["local"],
      "disabled": false
    },
    "sequential-thinking": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
      "tags": ["reasoning"],
      "disabled": false
    },
    "git": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-git", "--repository", "."],
      "tags": ["vcs"],
      "disabled": false
    },
    "puppeteer": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-puppeteer"],
      "tags": ["browser"],
      "disabled": false
    },
    "brave-search": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-brave-search"],
      "env": { "BRAVE_API_KEY": "${BRAVE_API_KEY:-}" },
      "tags": ["search", "web"],
      "disabled": false
    }
  }
}
```

- [ ] **Step 2: Verify JSON**

```bash
cd /home/rausraus/code/MCP-Hub && python3 -m json.tool hub.config.json > /dev/null && echo "valid"
```

- [ ] **Step 3: Commit**

```bash
git add hub.config.json .gitignore
git commit -m "feat: add hub.config.json as default MCP server config file"
```

### Task 1.2: Create env_expand.py (dependency for config.py)

**Files:** Create `src/mcp_hub/env_expand.py`, `tests/test_env_expand.py`

**What:** `${VAR}` and `${VAR:-default}` expansion. Claude Code compatible syntax.

- [ ] **Step 1: Write 13 tests** — See appendix for full test suite (test_env_expand.py)
- [ ] **Step 2: Run → fail**

```bash
python3 -m pytest tests/test_env_expand.py -v
# Expected: FAIL (ModuleNotFoundError)
```

- [ ] **Step 3: Implement env_expand.py** — See appendix for full implementation
- [ ] **Step 4: Run → pass**

```bash
cd /home/rausraus/code/MCP-Hub && source .venv/bin/activate && python3 -m pytest tests/test_env_expand.py -v
# Expected: 13 tests PASS
```

- [ ] **Step 5: Commit**

```bash
git add src/mcp_hub/env_expand.py tests/
git commit -m "feat: add env var expansion utility (${VAR}, ${VAR:-default})"
```

### Task 1.3: Create config.py

**Files:** Create `src/mcp_hub/config.py`, `tests/test_config.py`

**What:** Loads `hub.config.json`, applies env expansion, skips disabled servers. Tags retained for connection-time filtering.

- [ ] **Step 1: Write tests** — See appendix for full test suite (test_config.py, 6 tests)
- [ ] **Step 2: Run → fail**
- [ ] **Step 3: Implement config.py** — See appendix for full implementation
- [ ] **Step 4: Run → pass**
- [ ] **Step 5: Commit**

### Task 1.4: Refactor Registry & Main

**Files:** `registry.py` (remove `DEFAULT_SERVERS`), `main.py` (load config → seed)

**What:** Registry receives servers from config, no more hardcoded list. `MCP_HUB_RESEED=1` wipes DB for migration.

- [ ] **Step 1: Remove DEFAULT_SERVERS, add seed_servers param to init()**
- [ ] **Step 2: Add MCP_HUB_RESEED support in init()**
- [ ] **Step 3: Update main.py lifespan to `load_config()` → `registry.init(seed_servers=config.servers)`**
- [ ] **Step 4: Verify**

```bash
rm -f data/hub.db && MCP_HUB_PORT=26278 timeout 8 python3 -m mcp_hub.main 2>&1 | grep "Seeded"
# Expected: 6 servers seeded from hub.config.json
```

- [ ] **Step 5: Commit**

---

## Chunk 2: API Expansion (PATCH + Enable/Disable)

### Task 2.1: Add PATCH Endpoint for Server Config

**Files:** `admin_router.py`

**What:** `PATCH /admin/api/servers/{name}` updates server config (tags, env, disabled flag, etc.) and triggers proxy rebuild.

- [ ] **Step 1: Add PATCH endpoint**

```python
@router.patch("/servers/{name}")
async def update_server(name: str, config: ServerConfig):
    """サーバー設定を部分的に更新。指定されたフィールドのみ上書き。"""
    registry = _get_registry()
    pm = _get_proxy_manager()

    existing = await registry.get_server(name)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")

    # Merge: keep existing fields, override with new values
    merged = {**existing["config"], **config.model_dump(exclude_unset=True)}
    await registry.add_server(name, merged)
    try:
        await pm.refresh_server(name, merged)
    except Exception as e:
        logger.warning("Failed to refresh proxy for %s: %s", name, e)

    return {"name": name, "config": merged}
```

`ServerConfig` model uses `exclude_unset=True` → only sent fields are updated.

- [ ] **Step 2: Add `refresh_server()` to ProxyManager**

Rebuild proxy for one server (unmount old, create new, mount). If `disabled: true`, unmount without recreating.

- [ ] **Step 3: Test with curl**

```bash
# Update tags
curl -s -X PATCH http://localhost:26278/admin/api/servers/fetch \
  -H "Content-Type: application/json" \
  -d '{"tags":["web","essential"]}' | python3 -m json.tool

# Disable server
curl -s -X PATCH http://localhost:26278/admin/api/servers/brave-search \
  -H "Content-Type: application/json" \
  -d '{"disabled":true}' | python3 -m json.tool
```

- [ ] **Step 4: Commit**

### Task 2.2: Exclude Disabled Servers from /mcp Tool List

**Files:** `proxy_manager.py`

**What:** When `disabled: true`, the server stays registered in DB but its tools are hidden from tool listing via `/mcp` and `/admin/api/servers`.

- [ ] **Step 1: Filter disabled in list_tools()**

```python
async def list_tools(self, tags: list[str] | None = None) -> dict[str, list[dict]]:
    for name, config in self._server_configs.items():
        if config.get("disabled"):
            continue  # skip disabled servers
        # ... tag filtering
```

- [ ] **Step 2: Add test**

```python
def test_disabled_server_not_in_tool_list():
    # Register server with disabled:true → list_tools should not include it
```

- [ ] **Step 3: Commit**

---

## Chunk 3: Connection-Time Tag Filtering (Dual Mode)

### Task 3.1: Support Query Params + HTTP Headers

**Files:** `proxy_manager.py`, `main.py`

**What:** `GET /mcp?tags=web,local` OR `X-MCP-Hub-Tags: web,local` header. Headers take priority over query params. Both use same OR-logic tag matching. No tags → all servers.

- [ ] **Step 1: Store per-connection tags via middleware**

In `main.py`, intercept both query params and headers:

```python
from contextvars import ContextVar

request_tags: ContextVar[list[str] | None] = ContextVar("request_tags", default=None)

@app.middleware("http")
async def tag_middleware(request: Request, call_next):
    if request.url.path.startswith("/mcp"):
        # Headers take priority over query params
        header_tags = request.headers.get("X-MCP-Hub-Tags", "")
        query_tags = request.query_params.get("tags", "")

        tags_raw = header_tags if header_tags else query_tags
        if tags_raw:
            request_tags.set([t.strip() for t in tags_raw.split(",") if t.strip()])
    response = await call_next(request)
    request_tags.set(None)
    return response
```

- [ ] **Step 2: Add list_tools filtering to ProxyManager**

Store `_server_configs` dict (name → config with tags). In `list_tools()`, read `request_tags` context var and apply OR filter.

- [ ] **Step 3: Verify both modes**

```bash
# Query param mode
curl -s -X POST 'http://localhost:26278/mcp?tags=web' \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# Header mode (takes priority)
curl -s -X POST 'http://localhost:26278/mcp?tags=ignore_me' \
  -H "Content-Type: application/json" \
  -H "X-MCP-Hub-Tags: web" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

Both should return same result (web-tagged servers only). Header overrides query param.

- [ ] **Step 4: Commit**

---

## Chunk 4: Observability & Internal Resource

### Task 4.1: Metrics Endpoint

**Files:** `admin_router.py`, `state.py`

`GET /admin/api/metrics` → `{ uptime_seconds, servers_registered, servers_active, total_tools, tool_calls_total, tool_call_errors }`

### Task 4.2: JSON Structured Logging

**Files:** `main.py`

`MCP_HUB_LOG=json` → `{"timestamp":"...", "level":"INFO", "logger":"...", "message":"..."}`

### Task 4.3: `hub://servers` Resource

**Files:** `main.py`

Register FastMCP resource `hub://servers` returning JSON snapshot with tags, status, disabled flag, tool count.

### Task 4.4: Connection URL Display in WebUI → Task 5.4 handles UI portion

**Files:** `admin_router.py` — add `connection_url` to server info

- [ ] Add endpoint that returns formatted connection URL + example config

```python
@router.get("/servers/{name}/connection")
async def connection_info(name: str, request: Request):
    """接続情報（URL + サンプル設定）を返す。"""
    server = await registry.get_server(name)
    tags = server["config"].get("tags", [])
    base_url = f"{request.base_url}mcp"
    url = f"{base_url}?tags={','.join(tags)}" if tags else base_url
    return {"url": url, "tags": tags,
            "example_header": f"X-MCP-Hub-Tags: {','.join(tags)}" if tags else None}
```

### Task 4.5: README Update

Document all new env vars, config file, endpoints, and connection modes.

---

## Chunk 5: WebUI Overhaul (Design + Feature Integration)

**Design direction:** Simple + cute. Replace current glassmorphism. Visual refresh by @designer (#057). Copy reviewed by orchestrator afterward.

### Task 5.1: Design Refresh (@designer lane)

**Files:** `src/mcp_hub/static/index.html` (full rewrite of CSS + layout)

**What:** Replace glassmorphism theme with a simple, cute design. Keep all existing features working.

**Key design parameters for @designer:**
- Simple, clean, cute aesthetic
- Soft colors, rounded corners, playful but professional
- Remove floating orb animations
- Keep all existing HTML structure elements (API, modals, cards)
- Responsive, accessible (WAI-ARIA preserved)
- Light/dark? Let designer choose appropriate palette

- [ ] **Step 1: Dispatch @designer** with design requirements (background task)
- [ ] **Step 2: Orchestrator reviews and fixes copy (after designer completes)**
- [ ] **Step 3: Commit**

### Task 5.2: Server Status Display

**Files:** `proxy_manager.py`, `admin_router.py`, `index.html`

**What:** Show per-server connection status (connected/error) on cards via status badges. Already partially in plan; integrate with new design.

- [ ] **Step 1: Add status tracking to ProxyManager** (mount → "connected", error → "error")
- [ ] **Step 2: Include `status` + `disabled` in API response**
- [ ] **Step 3: @designer adds status badges to server card component in refreshed design**

### Task 5.3: Enable/Disable Toggle + Tag Management

**Files:** `index.html`

**What:** Each server card gets a toggle switch for enable/disable (calls PATCH). Tag chips with add/remove below each server card.

- [ ] **Step 1: @designer adds toggle switch UI component**
- [ ] **Step 2: JS: wire toggle → PATCH `{ disabled: true/false }`**
- [ ] **Step 3: @designer adds tag chip management UI**
- [ ] **Step 4: JS: wire tag add/remove → PATCH `{ tags: [...] }`**

### Task 5.4: Config Editing Modal

**Files:** `index.html`

**What:** "Edit" button per server card → modal showing editable config fields (command, args, env vars, tags, disabled). Save → PATCH.

- [ ] **Step 1: @designer adds edit button + config edit modal**
- [ ] **Step 2: JS: wire edit modal → PATCH endpoint**

### Task 5.5: Connection URL Copy

**Files:** `index.html`

**What:** "Copy Connection URL" button per server card. Shows `/mcp?tags=...` with tags if any. Copies to clipboard. Example config snippet below.

- [ ] **Step 1: @designer adds connection info section to card/expand area**
- [ ] **Step 2: JS: fetch connection info, render URL + copy button**

### Task 5.6: Tag Filter UI

**Files:** `index.html`

**What:** Filter bar above server grid with clickable tag chips (OR logic). Filters displayed cards client-side.

- [ ] **Step 1: @designer adds filter bar component**
- [ ] **Step 2: JS: extract unique tags, render chips, filter card visibility**

---

## Chunk 6: Testing & CI

### Task 6.1: Backend Test Suite

**Files:** `tests/test_admin_api.py`, `tests/test_tag_filter.py`

Test PATCH endpoint, enable/disable exclusion, tag filtering (both modes), mixed disabled + tag scenarios.

### Task 6.2: CI Enhancement

**Files:** `.github/workflows/docker.yml`, `pyproject.toml`

Add `pytest` job. Add `pytest`, `pytest-asyncio`, `httpx` as dev deps.

---

## Execution Order

```
Chunk 1 (Config Foundation) ──┐
                               ├──► Chunk 2 (PATCH + Disable) ──► Chunk 3 (Tag Filtering)
                               │                                          │
                               └──► Chunk 5 (WebUI Overhaul) ◄────────────┘
                                         │
                                    Chunk 4 (Observability)
                                         │
                                    Chunk 6 (Tests/CI)
```

**Parallelization:** Chunks 4 + 5 can start after Chunk 1 (they don't depend on each other). Chunk 6 runs after everything.

---

## Design Handoff Notes

@designer (#057) owns visual quality: layout, hierarchy, spacing, motion, affordances, responsive behavior. After @designer completes, the orchestrator:
1. Reviews copy only (designer copy may be AI-y)
2. Fixes copy without changing visual intent or structure
3. Does NOT simplify/refactor design output (it's intentional)

@fixer (#011) may NOT do design work. Only mechanical follow-up that preserves @designer's output exactly.

---

## Feature Comparison: MCP-Hub vs Hatago

| Feature | Hatago (TS) | MCP-Hub (after plan) |
|---------|-------------|---------------------|
| Config file | ✅ `hatago.config.json` | ✅ `hub.config.json` |
| Env expansion `${VAR:-default}` | ✅ | ✅ |
| Tag filtering | ✅ `--tags` (startup only) | ✅ `/mcp?tags=` + `X-MCP-Hub-Tags` header (per-connection) |
| Enable/disable toggle via API | ✅ (config-level) | ✅ (PATCH endpoint + WebUI toggle) |
| Metrics endpoint | ✅ | ✅ |
| JSON logging | ✅ | ✅ |
| Internal resource | ✅ | ✅ |
| Admin WebUI | ❌ | ✅ |
| REST API | ❌ | ✅ |
| PATCH config editing | ❌ | ✅ (unique) |
| Connection URL copy | ❌ | ✅ (unique) |
| Design | N/A | simple + cute refresh |
| Config inheritance (`extends`) | ✅ | deferred |
