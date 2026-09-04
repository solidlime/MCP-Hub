# MCP SDK v2 Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate MCP-Hub from fastmcp 3.4.4 + mcp 1.28.1 to fastmcp 4.0.x + mcp 2.1.x with v1+v2 upstream compat.

**Architecture:** Keep proxy flow unchanged; replace private fastmcp/mcp internal uses with 4.x public APIs (`add_provider`, `mount(namespace=)`, `ProxyClient`, `TransportOptions`), isolate the rest behind version-guarded shims, and prove v1 fallback + stateless behavior with contract tests.

**Tech Stack:** Python 3.12 / fastmcp>=4.0,<5.0 / mcp>=2.0,<3.0 / httpx2>=2.5.0 / pydantic>=2.12 / starlette>=1.0.1 / pytest via scripts/run-tests.sh

## Global Constraints

- `pyproject.toml` target: `fastmcp>=4.0,<5.0` (remove `<4.0.0` cap).
- Transitive floors: `mcp>=2.0,<3.0`, `httpx2>=2.5.0` exclusive, `pydantic>=2.12`, `starlette>=1.0.1` (hence `fastapi>=0.133.0`), `opentelemetry-api>=1.28`, `mcp-types` exact-pin accepted.
- Tests MUST run via `scripts/run-tests.sh` (`pytest` direct is forbidden — OOM risk). Single file: `TEST_FILE=tests/test_x.py ./scripts/run-tests.sh`.
- `Client(mode="auto")` modern probe→legacy fallback is the only v1 compat mechanism; no Hub-side protocol branching.
- `Fetch`/`grok` `--with "mcp<2.0.0"` pins in `D:\Desktop\hub.config.json` stay until green, removed only after verification.
- Naming: `{server}_{tool}` mount namespace, `hub://servers`, `data://api/info` URIs unchanged.
- No new extensions (`tasks` etc.); unmount/remove_provider absent → rebuild instead of mutating.

---

### Task 1: Dependency floors + import gate

**Files:**
- Modify: `pyproject.toml:7`
- Modify: `uv.lock` (via `uv lock`)
- Test: `tests/test_health.py`

**Interfaces:**
- Consumes: spec D1 floors.
- Produces: importable `mcp_hub.main` on 4.x for all later tasks.

- [ ] **Step 1: Update pyproject pin**

```toml
# pyproject.toml dependencies, replace line 7
"fastmcp>=4.0,<5.0",
```

- [ ] **Step 2: Re-lock and verify floors**

```bash
uv lock --check
```

Run: `uv lock && uv lock --check`
Expected: PASS, `uv.lock` resolves `fastmcp 4.0.x`, `mcp 2.1.x`, `httpx2`, `mcp-types` exact.

- [ ] **Step 3: Import gate (oracle R1)**

```bash
uv run python -c "import mcp_hub.main; print('import ok')"
```

Expected: PASS with `import ok`. If `ModuleNotFoundError` on `fastmcp.server.http` or similar, stop — fix import before continuing.

- [ ] **Step 4: Run health tests**

Run: `TEST_FILE=tests/test_health.py ./scripts/run-tests.sh`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore(deps): fastmcp 4 mcp2 floors + import gate"
```

### Task 2: proxy_manager public-API migration

**Files:**
- Modify: `src/mcp_hub/proxy_manager.py:21,30-48,134,179,320,837,847,853,856-883,890-900`
- Test: `tests/test_retry.py`

**Interfaces:**
- Consumes: Task 1 importable server.
- Produces: `mount(namespace=)` live proxy + `ProxyProvider(client_factory)` reuse preserved.

- [ ] **Step 1: Fix roots patch attribution**

```python
# src/mcp_hub/proxy_manager.py:21,35,48 — keep, add comment
from fastmcp.server.providers import proxy as _proxy_providers  # 4.x: default_proxy_roots_handler survives (proxy.py:1524)
```

Verify `Client(roots=...)` is frontend path; delete any direct `ctx.list_roots()` call in Hub (4.x removed). Keep `_resilient_default_roots` shim (RuntimeError→[]).

- [ ] **Step 2: Replace providers reset with rebuild**

```python
# BEFORE (private, forbidden):
self.mcp.providers = [self.mcp.local_provider]
# AFTER: reconstruct or add_provider; never assign .providers directly
# If full reset needed: rebuild FastMCP(providers=[local_provider]) or use add_provider()
```

`remove_provider`/`unmount` do not exist — do not call them.

- [ ] **Step 3: Preserve client_factory reuse (do NOT naively create_proxy(URL))**

```python
# Keep: single connected Client returned by client_factory for handshake avoidance
# 4.x正規: ProxyProvider(client_factory, cache_ttl=300), per-request fresh via ProxyClient.new()
from fastmcp.server.providers.proxy import ProxyClient
```

- [ ] **Step 4: Migrate forward headers**

```python
# BEFORE:
transport.forward_incoming_headers = True
# AFTER: rely on PROXY_TRANSPORT_OPTIONS(forward_incoming_headers=True) +
# TransportOptions + _get_forwardable_http_headers(); Cookie needs get_http_headers(include={"cookie"})
```

- [ ] **Step 5: Confirm mount labels (oracle FYI)**

`proxy_manager.py:134,179,320,900` are already `namespace=` — label as "confirm only", real body is `_rebuild_mounts`. `Client(timeout=...)` at `:837` is already float — mark "unchanged (float done)".

- [ ] **Step 6: Run tests**

Run: `TEST_FILE=tests/test_retry.py ./scripts/run-tests.sh`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/mcp_hub/proxy_manager.py
git commit -m "fix(proxy): fastmcp4 public providers/mount/proxyclient"
```

### Task 3: main.py guards + transport + lifespan

**Files:**
- Modify: `src/mcp_hub/main.py:92-96,142-157,259,264-282,286-318`
- Test: `tests/test_lenient_session_manager.py`

**Interfaces:**
- Consumes: Task 2 proxy.
- Produces: bootable HTTP app on 4.x with LenientSM swap intact.

- [ ] **Step 1: Flip version guard comments**

```python
# main.py:142-145: "Tested against <5.0.0"
# main.py:150-157: warn only if >=5.0.0 (was >=4.0.0)
# main.py:264-267: delete "pinned to <3.5.0" stale comment
```

- [ ] **Step 2: Fix http_app arg order**

```python
# 4.x: FastMCP.http_app(path, middleware, json_response, stateless_http, transport, event_store,...)
mcp_server.http_app(path="/", transport="streamable-http")
```

Apply at `:259` and `:286`.

- [ ] **Step 3: Harden routes scan for modern era**

```python
# main.py:271-274 — match StreamableHTTPASGIApp endpoint; ignore server/discover routes
# Guard: RequireAuthMiddleware/auth_routes and HostOriginGuard must not false-positive
from fastmcp.server.http import StreamableHTTPASGIApp
```

- [ ] **Step 4: Replace _cleanup_stale with idle timeout**

```python
# main.py:92-96 hasattr(_cleanup_stale) → session_idle_timeout=... on SM construction
# _server_instances is shutdown-terminate only — do not reuse for idle掃除
```

- [ ] **Step 5: Run tests**

Run: `TEST_FILE=tests/test_lenient_session_manager.py ./scripts/run-tests.sh`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mcp_hub/main.py
git commit -m "fix(main): fastmcp4 guards/http_app/routes/lifespan"
```

### Task 4: LenientSM split (server side only)

**Files:**
- Modify: `src/mcp_hub/lenient_session_manager.py:1-33`
- Test: `tests/test_lenient_session_manager.py`

**Interfaces:**
- Consumes: Task 3 SM construction.
- Produces: POST-only lenient SM on surviving base class.

- [ ] **Step 1: Confirm base and trim to server-only touchpoints**

```python
from fastmcp.server.http import FastMCPStreamableHTTPSessionManager  # survives (http.py:38)
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER

class LenientSessionManager(FastMCPStreamableHTTPSessionManager):
    async def handle_request(self, *a, **kw):  # POST+unknown-session → stateless, else super()
        ...
```

Only `handle_request`, `_is_unknown_session`, `_server_instances` (read-only check). `_handle_stateless_request` absent in 4.x → use `stateless_http=True` path.

- [ ] **Step 2: Run tests**

Run: `TEST_FILE=tests/test_lenient_session_manager.py ./scripts/run-tests.sh`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add src/mcp_hub/lenient_session_manager.py src/mcp_hub/main.py
git commit -m "fix(sm): lenient POST-only on fastmcp4 base"
```

### Task 5: streamable_http_patch httpx2 + RootModel removal

**Files:**
- Modify: `src/mcp_hub/streamable_http_patch.py:27,51-57,98,127,139,152,171-179`
- Test: `tests/test_streamable_http_async.py`

**Interfaces:**
- Consumes: Task 1 httpx2.
- Produces: working 202-poll patch without `message.root`.

- [ ] **Step 1: httpx→httpx2 (incl. type annotation :139)**

```python
import httpx2
from mcp.client.streamable_http import RequestContext  # (client: httpx2.AsyncClient,...)
```

`ctx.client.stream/post` types become httpx2.

- [ ] **Step 2: Eliminate message.root**

```python
# BEFORE (2 hits :127,:152):
if isinstance(message.root, JSONRPCRequest):
# AFTER: direct union member check via TypeAdapter (no .root)
```

- [ ] **Step 3: Fix names**

`_handle_json_response`/`_handle_sse_response` are full names (`_handle_json` short is wrong). `_send_session_terminated_error` does not exist — keep 404 Session-terminated behavior inline. `_handle_unexpected` function does not exist — keep else-branch. `CONTENT_TYPE_JSON/SSE` survive. `model_dump(by_alias=True)` for wire camelCase.

- [ ] **Step 4: Run tests**

Run: `TEST_FILE=tests/test_streamable_http_async.py ./scripts/run-tests.sh`
Expected: PASS (202-poll real flow via async_mcp_server).

- [ ] **Step 5: Commit**

```bash
git add src/mcp_hub/streamable_http_patch.py
git commit -m "fix(patch): httpx2 + root removal"
```

### Task 6: tag_filter backend-name swap

**Files:**
- Modify: `src/mcp_hub/tag_filter.py:116`
- Test: `tests/test_tool_index.py` (or nearest tag test; fallback `tests/test_dogfood.py`)

**Interfaces:**
- Consumes: Task 2 mount namespaces.
- Produces: correct server attribution without `_server`.

- [ ] **Step 1: Swap private attr**

```python
# BEFORE:
getattr(item, "_server", None)
# AFTER:
getattr(item, "_backend_name", None)  # ProxyTool._backend_name (proxy.py:340); Prompt→_backend_name, Resource→_backend_uri
```

Keep `proxy_to_name(id(server))` fallback until proven; mark shim-isolated.

- [ ] **Step 2: Run tests**

Run: `TEST_FILE=tests/test_dogfood.py ./scripts/run-tests.sh`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add src/mcp_hub/tag_filter.py
git commit -m "fix(filter): backend_name attribution"
```

### Task 7: meta_provider + full_info inputSchema decision

**Files:**
- Modify: `src/mcp_hub/meta_provider.py:86,198,230,284,434,697`, `src/mcp_hub/full_info.py:26,63-67,85,90`
- Test: `tests/test_full_info.py`

**Interfaces:**
- Consumes: Tasks 2,6.
- Produces: consistent Tool↔dict conversion on 4.x fields.

- [ ] **Step 1: Lock internal key decision (keep inputSchema internally)**

`meta_provider.py:198 doc.get("inputSchema")`, `:434 get_schema`, `:697 getattr(t,"parameters")→"inputSchema"`, docstrings `:86,:230,:284`, `full_info.py:66 schema.get("inputSchema")` stay as internal dict keys. `Tool(name,description,parameters)` and `ToolResult(is_error)` are already snake_case — do not touch. Verify `TextContent(type="text")` survives.

- [ ] **Step 2: Run tests**

Run: `TEST_FILE=tests/test_full_info.py ./scripts/run-tests.sh`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add src/mcp_hub/meta_provider.py src/mcp_hub/full_info.py
git commit -m "fix(meta): inputSchema internal key locked"
```

### Task 8: echo servers + AnyUrl + contract tests

**Files:**
- Modify: `tests/test_servers/stdio_echo_server.py:6`, `tests/test_servers/sse_echo_server.py:29`, `tests/test_admin_api.py:306`
- Test: `tests/test_admin_api.py`

**Interfaces:**
- Consumes: Tasks 1-7.
- Produces: v1+v2 provable fixtures.

- [ ] **Step 1: Update echo servers**

`@mcp.tool` plain decorator stays; drop `serializer=` if present. Note SSE is legacy-only on 4.x (handshake fixed even in auto).

- [ ] **Step 2: Fix AnyUrl**

```python
# tests/test_admin_api.py:306 — Resource.uri is str on v2, AnyUrl(...) construction must go
```

- [ ] **Step 3: Add contract checks (extend existing, no new framework)**

v1 upstream mixed auto-fallback proof, `{server}_{tool}` naming, `data://api/info` URI, modern-era LenientSM stateless, httpx2 202-poll via `async_mcp_server.py`.

- [ ] **Step 4: Run tests**

Run: `TEST_FILE=tests/test_admin_api.py ./scripts/run-tests.sh`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_servers/stdio_echo_server.py tests/test_servers/sse_echo_server.py tests/test_admin_api.py
git commit -m "test: v2 fixtures + AnyUrl + contracts"
```

### Task 9: docs + dependency gates + real-machine matrix

**Files:**
- Modify: `docs/architecture.md:295-308`, `docs/development.md:26`, `docs/security.md:144-161`
- Test: full suite + real Hub on `D:\Desktop\hub.config.json`

**Interfaces:**
- Consumes: Tasks 1-8.
- Produces: shippable migration with evidence.

- [ ] **Step 1: Update pin docs**

`<4.0.0` → `<5.0.0`, `sys.exit` contradiction removed, old `>=1.24.0,<3.5.0` remnant removed.

- [ ] **Step 2: Full suite**

Run: `./scripts/run-tests.sh`
Expected: 0 failures.

- [ ] **Step 3: Real-machine matrix**

Hub with `D:\Desktop\hub.config.json`: tool counts, SSE/StreamableHTTP, Fetch/grok pin on/off, meta on/off, tag, full_info. Then decide Fetch `--with` removal. Record results in commit message.

- [ ] **Step 4: Dependency gate**

```bash
uv lock --check
```

pydantic/starlette/FastAPI floors + mcp-types exact-pin verified.

- [ ] **Step 5: Commit**

```bash
git add docs/architecture.md docs/development.md docs/security.md
git commit -m "docs: v2 migration pins + verification matrix"
```
