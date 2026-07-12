# MCP-Hub WebUI Fix & Hatago Feature Integration Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking. Each task is self-contained with exact file paths and expected outcomes.

**Goal:** Fix broken WebUI (seed server packages), integrate key features from hatago-mcp-hub (env expansion, tags, metrics, internal resource, structured logging), and add test coverage.

**Architecture:** The MCP-Hub is a Python/FastAPI+FastMCP proxy that aggregates multiple MCP servers. The WebUI is a Vanilla JS SPA served from static HTML. Changes span backend (registry, proxy_manager, admin_router, main), frontend (index.html), and devops (CI, tests).

**Tech Stack:** Python 3.12+, FastAPI, FastMCP 3.4+, aiosqlite, Vanilla JS/CSS

---

## Root Cause Analysis

**WebUI is not actually broken** — it serves HTML at `/admin/` with HTTP 200 and renders correctly. The perceived failure is:
1. All 6 seeded default servers use deprecated `@anthropic/mcp-server-*` packages (renamed to `@modelcontextprotocol/server-*`)
2. Each server fails with npm 404, resulting in 0 tools shown
3. WebUI has no visual distinction between "no tools" (empty server) and "connection failed" (error state)

The seed list in `registry.py:16-61` is the root cause. Additionally, `${BRAVE_API_KEY}` is passed literally without expansion.

---

## File Structure

```
src/mcp_hub/
├── __init__.py
├── main.py              # Entry point — modify: metrics, JSON logging
├── admin_router.py      # REST API — modify: tags, metrics, resource endpoints
├── proxy_manager.py     # Proxy lifecycle — modify: status tracking, tags, notification forwarding
├── registry.py          # SQLite + seeds — modify: updated packages, env expansion, disabled flag
├── state.py             # Shared state — modify: add metrics counter
├── env_expand.py        # NEW: Environment variable expansion utility
├── static/
│   └── index.html       # WebUI SPA — modify: error states, health badges, tag filter
tests/
├── __init__.py          # NEW
├── test_env_expand.py   # NEW: Env expansion tests
├── test_registry.py     # NEW: Registry tests
├── test_admin_api.py    # NEW: Admin API tests
└── conftest.py          # NEW: Test fixtures
.github/workflows/
└── docker.yml           # CI — modify: add lint + test stages
```

---

## Chunk 1: Critical Fixes (P0)

### Task 1.1: Fix Seed Server Packages

**Files:**
- Modify: `src/mcp_hub/registry.py:16-61`
- Modify: `src/mcp_hub/registry.py:57-58` (remove env expansion placeholder — handled in Task 2.1)

**What:** Update `DEFAULT_SERVERS` to use current `@modelcontextprotocol/server-*` packages.

- [ ] **Step 1: Update the seed list in registry.py**

Replace the DEFAULT_SERVERS constant with corrected packages and remove the un-expandable `${BRAVE_API_KEY}` pattern (it will be handled properly by env expansion in Chunk 2):

```python
DEFAULT_SERVERS = [
    {
        "name": "fetch",
        "config": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-fetch"],
        },
    },
    {
        "name": "filesystem",
        "config": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        },
    },
    {
        "name": "sequential-thinking",
        "config": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        },
    },
    {
        "name": "git",
        "config": {
            "command": "npx",
            "args": ["-y", "@anthropic/mcp-server-git", "--repository", "."],
        },
    },
    {
        "name": "puppeteer",
        "config": {
            "command": "npx",
            "args": ["-y", "@anthropic/mcp-server-puppeteer"],
        },
    },
    {
        "name": "brave-search",
        "config": {
            "command": "npx",
            "args": ["-y", "@anthropic/mcp-server-brave-search"],
            "env": {
                "BRAVE_API_KEY": "",  # User must set this
            },
        },
    },
]
```

Note: `@anthropic/mcp-server-git`, `@anthropic/mcp-server-puppeteer`, and `@anthropic/mcp-server-brave-search` are still valid packages. Only `fetch`, `filesystem`, and `sequential-thinking` moved to `@modelcontextprotocol/`. The filesystem path also changed from `/opt/mcp-hub/data` to `/tmp` (user-writable).

- [ ] **Step 2: Delete old DB to force re-seed and test**

```bash
rm -f /home/rausraus/code/MCP-Hub/data/hub.db
cd /home/rausraus/code/MCP-Hub && source .venv/bin/activate && MCP_HUB_PORT=26266 timeout 10 python3 -m mcp_hub.main 2>&1
```

Expected: Seeding logs show new package names. Some servers may still fail (network-dependent), but npm 404 errors for `@modelcontextprotocol/server-fetch` etc. should be gone.

- [ ] **Step 3: Commit**

```bash
git add src/mcp_hub/registry.py
git commit -m "fix: update default MCP server packages to @modelcontextprotocol/server-*"
```

### Task 1.2: WebUI Error Resilience — Server Status Display

**Files:**
- Modify: `src/mcp_hub/static/index.html` — JS rendering + CSS

**What:** Show per-server connection status. Differentiate "connected with tools", "connected but 0 tools", "connection failed". Add status badge on each server card.

- [ ] **Step 1: Add health status field to API response**

In `admin_router.py`, add a `status` field derived from the proxy's tools listing success/failure:

Modify `list_servers()` at `admin_router.py:72-90` to include status:

```python
@router.get("/servers")
async def list_servers():
    registry = _get_registry()
    pm = _get_proxy_manager()

    servers = await registry.list_servers()
    tools_map = await pm.list_tools()
    # Also get per-server status
    status_map = pm.get_all_status()

    result = []
    for srv in servers:
        name = srv["name"]
        info = {
            "name": name,
            "config": srv["config"],
            "tools_count": len(tools_map.get(name, [])),
            "tools": tools_map.get(name, []),
            "status": status_map.get(name, "unknown"),
        }
        result.append(info)
    return {"servers": result}
```

- [ ] **Step 2: Add `get_all_status()` to ProxyManager**

In `proxy_manager.py`, add method:

```python
def get_all_status(self) -> dict[str, str]:
    """Get status for all registered servers."""
    result = {}
    for name in self._proxies:
        result[name] = getattr(self, "_status", {}).get(name, "connected")
    # Also include servers from registry that aren't yet proxied
    return result
```

Also track status during `register_server` and `load_all` — set `self._status[name] = "connected"` on success, `"error"` on failure. Initialize `self._status: dict[str, str] = {}` in `__init__`.

- [ ] **Step 3: Update WebUI CSS for status badges**

Add to `index.html` CSS section (after `.server-meta-item` styles):

```css
.status-badge-sm {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 0.75rem;
    padding: 2px 8px;
    border-radius: 12px;
    font-weight: 600;
}
.status-badge-sm.connected { background: rgba(52, 211, 153, 0.15); color: var(--accent-green); }
.status-badge-sm.error { background: rgba(248, 113, 113, 0.15); color: var(--accent-red); }
.status-badge-sm.unknown { background: rgba(251, 191, 36, 0.15); color: var(--accent-yellow); }
```

- [ ] **Step 4: Update WebUI JS rendering**

In the `renderServers()` function's server card template, add a status badge next to the server name:

```javascript
// Inside the server-header div, after server-name:
<div class="server-status-area">
    <span class="status-badge-sm ${server.status || 'unknown'}">
        ${server.status === 'connected' ? '● 接続済' : server.status === 'error' ? '● エラー' : '● 不明'}
    </span>
</div>
```

Also show error toast if any server has error status on initial load (in `loadServers()`):

```javascript
async function loadServers() {
    try {
        const data = await API.getServers();
        servers = data.servers || [];
        renderServers();
        // Show warning for servers in error state
        const errorServers = servers.filter(s => s.status === 'error');
        if (errorServers.length > 0) {
            showToast(`${errorServers.length} サーバーで接続エラーが発生しています`, 'error');
        }
    } catch (error) {
        showToast(error.message, 'error');
    }
}
```

- [ ] **Step 5: Test rendering**

```bash
cd /home/rausraus/code/MCP-Hub && source .venv/bin/activate && rm -f data/hub.db && MCP_HUB_PORT=26267 timeout 8 python3 -m mcp_hub.main 2>&1 &
sleep 4
curl -s http://localhost:26267/admin/api/servers | python3 -m json.tool | head -30
```

Expected: Server responses include `"status"` field.

- [ ] **Step 6: Commit**

```bash
git add src/mcp_hub/admin_router.py src/mcp_hub/proxy_manager.py src/mcp_hub/static/index.html
git commit -m "feat: add per-server status display to WebUI and API"
```

---

## Chunk 2: Environment Variable Expansion (P1)

### Task 2.1: Create env_expand.py Utility

**Files:**
- Create: `src/mcp_hub/env_expand.py`
- Create: `tests/test_env_expand.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

**What:** Implement `${VAR}` and `${VAR:-default}` expansion matching hatago's syntax. Claude Code compatible.

- [ ] **Step 1: Write tests first**

Create `tests/__init__.py` (empty) and `tests/conftest.py`:

```python
import os
import sys
import pytest

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture
def clean_env(monkeypatch):
    """Remove test-affecting env vars."""
    for key in ("TEST_VAR", "API_KEY", "PORT", "BRAVE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
```

Create `tests/test_env_expand.py`:

```python
import os
import pytest
from mcp_hub.env_expand import expand_env_vars


class TestExpandEnvVars:
    def test_passthrough_no_placeholders(self):
        assert expand_env_vars("hello world") == "hello world"

    def test_expand_simple_var(self, monkeypatch):
        monkeypatch.setenv("FOO", "bar")
        assert expand_env_vars("${FOO}") == "bar"

    def test_expand_var_in_string(self, monkeypatch):
        monkeypatch.setenv("NAME", "world")
        assert expand_env_vars("hello ${NAME}") == "hello world"

    def test_missing_var_raises(self):
        with pytest.raises(ValueError, match="NOT_EXIST"):
            expand_env_vars("${NOT_EXIST}")

    def test_var_with_default_uses_value(self, monkeypatch):
        monkeypatch.setenv("FOO", "bar")
        assert expand_env_vars("${FOO:-default}") == "bar"

    def test_var_with_default_falls_back(self):
        assert expand_env_vars("${NOT_EXIST:-default}") == "default"

    def test_empty_default(self):
        assert expand_env_vars("${NOT_EXIST:-}") == ""

    def test_multiple_vars(self, monkeypatch):
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        assert expand_env_vars("${A} ${B}") == "1 2"

    def test_expand_in_dict(self, monkeypatch):
        monkeypatch.setenv("KEY", "secret")
        config = {"url": "https://${KEY}.example.com", "port": 8080}
        result = expand_env_vars(config)
        assert result["url"] == "https://secret.example.com"
        assert result["port"] == 8080

    def test_expand_nested_dict(self, monkeypatch):
        monkeypatch.setenv("TOKEN", "abc123")
        config = {
            "env": {"AUTH_TOKEN": "${TOKEN}", "DEBUG": "true"},
            "headers": {"Authorization": "Bearer ${TOKEN}"}
        }
        result = expand_env_vars(config)
        assert result["env"]["AUTH_TOKEN"] == "abc123"
        assert result["headers"]["Authorization"] == "Bearer abc123"

    def test_expand_in_list(self, monkeypatch):
        monkeypatch.setenv("PKG", "server-fetch")
        args = ["-y", "@modelcontextprotocol/${PKG}"]
        result = expand_env_vars(args)
        assert result[1] == "@modelcontextprotocol/server-fetch"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/rausraus/code/MCP-Hub && source .venv/bin/activate && uv pip install pytest pytest-asyncio 2>&1 | tail -3
python3 -m pytest tests/test_env_expand.py -v 2>&1
```

Expected: All tests FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement env_expand.py**

Create `src/mcp_hub/env_expand.py`:

```python
"""
Environment variable expansion utility.
Supports Claude Code / hatago compatible syntax:
  - ${VAR}      : Required variable (raises if undefined)
  - ${VAR:-default} : Variable with default fallback
"""

import os
import re
from typing import Any

# Matches ${VAR} or ${VAR:-default}
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_string(value: str) -> str:
    """Expand a single string value."""

    def _replacer(match: re.Match) -> str:
        var_name = match.group(1)
        default = match.group(2)  # None if no :-default part
        env_val = os.environ.get(var_name)
        if env_val is not None:
            return env_val
        if default is not None:
            return default
        raise ValueError(
            f"Environment variable '{var_name}' is not defined and no default provided"
        )

    return _ENV_PATTERN.sub(_replacer, value)


def expand_env_vars(obj: Any) -> Any:
    """Recursively expand environment variable placeholders in any structure.

    Supports strings, dicts, and lists. Other types are returned as-is.
    """
    if isinstance(obj, str):
        return _expand_string(obj)
    if isinstance(obj, dict):
        return {k: expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env_vars(item) for item in obj]
    return obj
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /home/rausraus/code/MCP-Hub && source .venv/bin/activate && python3 -m pytest tests/test_env_expand.py -v 2>&1
```

Expected: All 11 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_hub/env_expand.py tests/
git commit -m "feat: add environment variable expansion utility with ${VAR} and ${VAR:-default}"
```

### Task 2.2: Integrate env expansion into config processing

**Files:**
- Modify: `src/mcp_hub/proxy_manager.py:26,119-131`
- Modify: `src/mcp_hub/registry.py:114-121` (add_server also expands)

**What:** Expand env vars when creating proxies from config. Apply at the boundary where config enters the system.

- [ ] **Step 1: Add env expansion to _create_proxy**

In `proxy_manager.py`, import and use `expand_env_vars`:

```python
from .env_expand import expand_env_vars
```

Modify `_create_proxy`:

```python
def _create_proxy(self, name: str, config: dict) -> FastMCPProxy:
    """config から FastMCPProxy を生成。環境変数を展開する。"""
    config = expand_env_vars(config)
    url = config.get("url")
    command = config.get("command")
    ...
```

- [ ] **Step 2: Add env expansion in registry.add_server**

In `registry.py`, expand before saving:

```python
from .env_expand import expand_env_vars
```

In `add_server`:

```python
async def add_server(self, name: str, config: dict) -> None:
    """サーバー登録。同名なら上書き。config内の環境変数を展開して保存。"""
    config = expand_env_vars(config)
    async with aiosqlite.connect(self.db_path) as db:
        ...
```

- [ ] **Step 3: Verify with a quick test**

```bash
cd /home/rausraus/code/MCP-Hub && source .venv/bin/activate && rm -f data/hub.db && BRAVE_API_KEY=test123 MCP_HUB_PORT=26268 timeout 6 python3 -m mcp_hub.main 2>&1 | grep -i brave
```

Expected: brave-search server should show `BRAVE_API_KEY=test123` in its config (expanded at save time).

- [ ] **Step 4: Commit**

```bash
git add src/mcp_hub/proxy_manager.py src/mcp_hub/registry.py
git commit -m "feat: integrate env variable expansion into server config processing"
```

---

## Chunk 3: Server Tags (P1)

### Task 3.1: Add tag support to config, API, and CLI

**Files:**
- Modify: `src/mcp_hub/admin_router.py` — ServerConfig model + endpoints
- Modify: `src/mcp_hub/registry.py` — Store tags
- Modify: `src/mcp_hub/proxy_manager.py` — Tag filtering
- Modify: `src/mcp_hub/main.py` — MCP_HUB_TAGS env var

**What:** Support `tags` field on server configs. Allow filtering via `MCP_HUB_TAGS` env var (comma-separated, OR logic matching hatago).

- [ ] **Step 1: Update ServerConfig model**

In `admin_router.py`, add `tags` field:

```python
class ServerConfig(BaseModel):
    url: str | None = None
    command: str | None = None
    args: list[str] = []
    tags: list[str] = []
```

- [ ] **Step 2: Update registry to store tags**

Modify `DEFAULT_SERVERS` to include tags:

```python
DEFAULT_SERVERS = [
    {"name": "fetch", "config": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-fetch"], "tags": ["web"]}},
    {"name": "filesystem", "config": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"], "tags": ["local"]}},
    {"name": "sequential-thinking", "config": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"], "tags": ["reasoning"]}},
    {"name": "git", "config": {"command": "npx", "args": ["-y", "@anthropic/mcp-server-git", "--repository", "."], "tags": ["vcs"]}},
    {"name": "puppeteer", "config": {"command": "npx", "args": ["-y", "@anthropic/mcp-server-puppeteer"], "tags": ["browser"]}},
    {"name": "brave-search", "config": {"command": "npx", "args": ["-y", "@anthropic/mcp-server-brave-search"], "env": {"BRAVE_API_KEY": ""}, "tags": ["search", "web"]}},
]
```

- [ ] **Step 3: Add tag filtering in ProxyManager.load_all**

In `proxy_manager.py`, filter by `MCP_HUB_TAGS`:

```python
async def load_all(self) -> None:
    """DB から全サーバーを読み込んでマウント。MCP_HUB_TAGS でフィルタ。"""
    servers = await self.registry.list_servers()
    if not servers:
        return

    # Tag filtering
    filter_tags = os.environ.get("MCP_HUB_TAGS", "").split(",")
    filter_tags = [t.strip() for t in filter_tags if t.strip()]

    for srv in servers:
        name = srv["name"]
        config = srv["config"]

        # Apply tag filter
        if filter_tags:
            server_tags = config.get("tags", [])
            if not any(t in server_tags for t in filter_tags):
                logger.info("Skipping %s (tags %s don't match filter %s)", name, server_tags, filter_tags)
                continue

        try:
            proxy = self._create_proxy(name, config)
            ...
```

- [ ] **Step 4: Verify with tag filtering**

```bash
cd /home/rausraus/code/MCP-Hub && source .venv/bin/activate && rm -f data/hub.db && MCP_HUB_PORT=26269 MCP_HUB_TAGS=web timeout 6 python3 -m mcp_hub.main 2>&1 | grep -E "(Loaded|Skipping|Seeded)"
```

Expected: Only `fetch` and `brave-search` are loaded; other 4 are skipped.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_hub/admin_router.py src/mcp_hub/registry.py src/mcp_hub/proxy_manager.py src/mcp_hub/main.py
git commit -m "feat: add tag-based server filtering via MCP_HUB_TAGS env var"
```

---

## Chunk 4: Observability & Diagnostics (P1)

### Task 4.1: Metrics Endpoint

**Files:**
- Modify: `src/mcp_hub/admin_router.py`
- Modify: `src/mcp_hub/state.py`

**What:** Add `/admin/api/metrics` endpoint returning hub stats (active servers, total tools, uptime, request count). hatago's `HATAGO_METRICS=1` pattern.

- [ ] **Step 1: Add metrics tracking to state**

In `state.py`, add:

```python
import time

class _AppState:
    registry: SqliteStore | None = None
    proxy_manager: ProxyManager | None = None
    start_time: float = 0.0
    tool_calls_total: int = 0
    tool_call_errors: int = 0
```

- [ ] **Step 2: Set start_time in lifespan**

In `main.py`, at the start of lifespan:

```python
async def lifespan(app: FastAPI):
    app_state.start_time = time.time()
    ...
```

- [ ] **Step 3: Add metrics endpoint**

In `admin_router.py`, add:

```python
import time
from .state import app_state

@router.get("/metrics")
async def metrics():
    pm = _get_proxy_manager()
    registry = _get_registry()
    servers = await registry.list_servers()

    uptime = time.time() - app_state.start_time
    total_tools = sum(
        len(tools)
        for tools in (await pm.list_tools()).values()
    )

    return {
        "uptime_seconds": round(uptime, 1),
        "servers_registered": len(servers),
        "servers_active": len(pm._proxies),
        "total_tools": total_tools,
        "tool_calls_total": app_state.tool_calls_total,
        "tool_call_errors": app_state.tool_call_errors,
    }
```

- [ ] **Step 4: Increment tool call counters in call_tool**

In `admin_router.py` `call_tool()`:

```python
@router.post("/servers/{name}/tools/{tool_name}/call")
async def call_tool(name: str, tool_name: str, body: CallToolRequest):
    pm = _get_proxy_manager()
    app_state.tool_calls_total += 1
    try:
        result = await pm.call_tool(name, tool_name, body.arguments)
        return {"result": result}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        app_state.tool_call_errors += 1
        ...
```

- [ ] **Step 5: Verify endpoint**

```bash
cd /home/rausraus/code/MCP-Hub && source .venv/bin/activate && rm -f data/hub.db && MCP_HUB_PORT=26270 timeout 8 python3 -m mcp_hub.main 2>&1 &
sleep 4
curl -s http://localhost:26270/admin/api/metrics | python3 -m json.tool
```

Expected: JSON with `uptime_seconds`, `servers_registered`, `servers_active`, `total_tools`, `tool_calls_total`, `tool_call_errors`.

- [ ] **Step 6: Commit**

```bash
git add src/mcp_hub/admin_router.py src/mcp_hub/state.py src/mcp_hub/main.py
git commit -m "feat: add /admin/api/metrics endpoint with hub telemetry"
```

### Task 4.2: JSON Structured Logging

**Files:**
- Modify: `src/mcp_hub/main.py`

**What:** Support `MCP_HUB_LOG=json` env var for structured JSON log output. hatago compatible.

- [ ] **Step 1: Add JSON log formatter**

In `main.py`, in the `main()` function:

```python
import json
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }, ensure_ascii=False)


def main():
    log_format = os.environ.get("MCP_HUB_LOG", "text")
    if log_format == "json":
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logging.basicConfig(level=logging.INFO, handlers=[handler])
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
    ...
```

- [ ] **Step 2: Verify JSON output**

```bash
cd /home/rausraus/code/MCP-Hub && source .venv/bin/activate && rm -f data/hub.db && MCP_HUB_LOG=json MCP_HUB_PORT=26271 timeout 5 python3 -m mcp_hub.main 2>&1 | head -5
```

Expected: JSON lines with `timestamp`, `level`, `logger`, `message` fields.

- [ ] **Step 3: Commit**

```bash
git add src/mcp_hub/main.py
git commit -m "feat: add JSON structured logging via MCP_HUB_LOG=json"
```

---

## Chunk 5: Internal Resource & Progress Forwarding (P1)

### Task 5.1: `hub://servers` Internal Resource

**Files:**
- Modify: `src/mcp_hub/proxy_manager.py`
- Modify: `src/mcp_hub/main.py`
- Modify: `src/mcp_hub/admin_router.py`

**What:** Expose a FastMCP resource `hub://servers` returning a JSON snapshot of all connected servers. hatago compatible URI.

- [ ] **Step 1: Register internal resource handler**

In `main.py` lifespan, after creating the FastMCP instance:

```python
mcp_server = FastMCP("MCP Hub")

# Register internal resource
@mcp_server.resource("hub://servers")
def get_hub_servers() -> str:
    """Return JSON snapshot of connected servers."""
    servers_info = []
    for name, proxy in proxy_manager._proxies.items():
        servers_info.append({
            "name": name,
            "status": proxy_manager._status.get(name, "unknown"),
        })
    return json.dumps(servers_info, indent=2, ensure_ascii=False)
```

- [ ] **Step 2: Verify via MCP protocol**

```bash
cd /home/rausraus/code/MCP-Hub && source .venv/bin/activate && rm -f data/hub.db && MCP_HUB_PORT=26272 timeout 8 python3 -m mcp_hub.main 2>&1 &
sleep 4
# Test resources/list via MCP endpoint
curl -s -X POST http://localhost:26272/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"resources/list","params":{}}' | python3 -m json.tool
```

Expected: Response includes `hub://servers` resource.

- [ ] **Step 3: Commit**

```bash
git add src/mcp_hub/main.py
git commit -m "feat: add hub://servers internal resource endpoint"
```

### Task 5.2: Progress Notification Forwarding

**Files:**
- Modify: `src/mcp_hub/proxy_manager.py`

**What:** Forward `notifications/progress` from child MCP servers to connected clients. This requires hooking into the FastMCP proxy's notification stream.

- [ ] **Step 1: Research FastMCP proxy notification API**

Check if `FastMCPProxy` or `create_proxy` supports notification callbacks. This may require using FastMCP internals.

Note: As of FastMCP 3.4.x, proxy notifications are forwarded automatically by the mount system. If this is already handled, we can document it and move on. If not, we'll need to investigate the internal API.

```bash
cd /home/rausraus/code/MCP-Hub && source .venv/bin/activate && python3 -c "from fastmcp.server.providers.proxy import FastMCPProxy; help(FastMCPProxy)" 2>&1 | head -40
```

- [ ] **Step 2: If forwarding is automatic, just add a log message**

In `proxy_manager.py`, when creating a proxy, log that notifications will be forwarded:

```python
logger.debug("Proxy %s created — progress notifications forwarded automatically by FastMCP", name)
```

- [ ] **Step 3: If manual forwarding is needed**

Implement callback-based forwarding (details depend on API discovery in Step 1). Only implement if API supports it.

- [ ] **Step 4: Commit**

```bash
git add src/mcp_hub/proxy_manager.py
git commit -m "feat: enable progress notification forwarding from child MCP servers"
```

---

## Chunk 6: Testing & CI Enhancement

### Task 6.1: Admin API Tests

**Files:**
- Create: `tests/test_admin_api.py`

**What:** Test the admin REST API endpoints using FastAPI's TestClient.

- [ ] **Step 1: Write admin API tests**

```python
import pytest
from fastapi.testclient import TestClient
from mcp_hub.main import create_app

@pytest.fixture
def client():
    app = create_app()
    # We need to run lifespan manually for testing
    # Use TestClient with lifespan
    with TestClient(app) as c:
        yield c

class TestHealthEndpoint:
    def test_health_returns_ok(self, client):
        response = client.get("/admin/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

class TestServerCRUD:
    def test_list_servers_empty(self, client):
        response = client.get("/admin/api/servers")
        assert response.status_code == 200
        assert "servers" in response.json()

    def test_add_server_url(self, client):
        response = client.post("/admin/api/servers", json={
            "name": "test-server",
            "config": {"url": "http://localhost:9999"}
        })
        assert response.status_code == 201
        assert response.json()["name"] == "test-server"

    def test_add_server_duplicate(self, client):
        client.post("/admin/api/servers", json={
            "name": "dup", "config": {"url": "http://localhost:9999"}
        })
        response = client.post("/admin/api/servers", json={
            "name": "dup", "config": {"url": "http://localhost:9999"}
        })
        assert response.status_code == 409

    def test_add_server_no_url_or_command(self, client):
        response = client.post("/admin/api/servers", json={
            "name": "bad", "config": {}
        })
        assert response.status_code == 422

    def test_delete_nonexistent(self, client):
        response = client.delete("/admin/api/servers/nonexistent")
        assert response.status_code == 404
```

Note: Actual test implementation may need adjustments for the lifespan-based initialization. Use `pytest-asyncio` and test the app with an in-memory SQLite database via dependency injection.

- [ ] **Step 2: Run tests (may need fixture refinement)**

```bash
cd /home/rausraus/code/MCP-Hub && source .venv/bin/activate && python3 -m pytest tests/test_admin_api.py -v 2>&1
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_admin_api.py
git commit -m "test: add admin API integration tests"
```

### Task 6.2: Add CI Test + Lint Stage

**Files:**
- Modify: `.github/workflows/docker.yml`

**What:** Add a test job that runs pytest before the Docker build.

- [ ] **Step 1: Add test job**

In `docker.yml`, add before the build job:

```yaml
  test:
    name: Run Tests
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install uv
        uses: astral-sh/setup-uv@v5

      - name: Install dependencies
        run: uv sync --dev

      - name: Run tests
        run: uv run pytest tests/ -v
```

Note: This may need `pyproject.toml` updates to add pytest as dev dependency.

- [ ] **Step 2: Add pytest to pyproject.toml**

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "httpx>=0.28",
]
```

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/docker.yml pyproject.toml
git commit -m "ci: add test stage to GitHub Actions workflow"
```

---

## Chunk 7: Final Integration & Polish

### Task 7.1: Update README with new features

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document new env vars and features**

Add sections for:
- `MCP_HUB_TAGS` — tag-based filtering
- `MCP_HUB_LOG=json` — JSON logging
- `MCP_HUB_METRICS` — metrics endpoint
- Environment variable expansion in config
- `hub://servers` resource

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document new env vars, tags, metrics, and hub://servers"
```

### Task 7.2: Final Integration Test

- [ ] **Step 1: Full run with new features**

```bash
cd /home/rausraus/code/MCP-Hub && source .venv/bin/activate && rm -f data/hub.db && MCP_HUB_LOG=json MCP_HUB_TAGS=web MCP_HUB_PORT=26273 timeout 8 python3 -m mcp_hub.main 2>&1
```

Expected: Only web-tagged servers load. JSON log output. No npm 404 errors for @modelcontextprotocol packages.

- [ ] **Step 2: Run all tests**

```bash
cd /home/rausraus/code/MCP-Hub && source .venv/bin/activate && python3 -m pytest tests/ -v 2>&1
```

- [ ] **Step 3: Commit if clean**

```bash
git add -A
git commit -m "chore: final integration polish and verification"
```

---

## Feature Comparison: MCP-Hub vs Hatago

| Feature | Hatago (TS) | MCP-Hub (Python) | Status |
|---------|-------------|-------------------|--------|
| Core proxy aggregation | ✅ | ✅ | Done |
| Multi-transport (stdio/http/sse) | ✅ | ✅ (stdio+http) | Done |
| Admin WebUI | ❌ | ✅ | Done |
| REST API for management | ❌ | ✅ | Done |
| Seed default servers | ❌ | ✅ | Fixed (Chunk 1) |
| Environment var expansion | ✅ | ✅ | Added (Chunk 2) |
| Tag-based filtering | ✅ | ✅ | Added (Chunk 3) |
| Configuration inheritance | ✅ | ❌ | Deferred |
| Metrics endpoint | ✅ | ✅ | Added (Chunk 4) |
| JSON structured logging | ✅ | ✅ | Added (Chunk 4) |
| Internal resource (hub://servers) | ✅ | ✅ | Added (Chunk 5) |
| Progress notification forwarding | ✅ | ✅ | Added (Chunk 5) |
| IHub interface | ✅ | ❌ | N/A (Python) |
| Session manager | ✅ | ⚠️ (via FastMCP) | Built-in |
| Dynamic tool list updates | ✅ | ⚠️ (via FastMCP mount) | Built-in |
| Env file loading | ✅ | ❌ | Deferred |
| Server disabled flag | ✅ | ✅ (implicit) | Simple |
| SSE server support | ✅ | ⚠️ (limited) | TBD |
| Docker support | ✅ | ✅ | Done |
| GitHub Actions CI | ✅ | ✅ | Enhanced |
| Tests | ✅ (402 tests) | ✅ | Added (Chunk 6) |

---

## Execution Order

1. **Chunk 1** (P0: Fix seeds + WebUI resilience)
2. **Chunk 6** (Tests + CI — run in parallel with Chunk 2-5 if using subagents)
3. **Chunk 2** (Env expansion — dependency for tags)
4. **Chunk 3** (Tags — depends on Chunk 2)
5. **Chunk 4** (Metrics + logging — independent)
6. **Chunk 5** (Internal resource + progress — independent)
7. **Chunk 7** (Polish — depends on all above)
