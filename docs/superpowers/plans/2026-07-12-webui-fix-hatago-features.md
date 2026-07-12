# MCP-Hub: WebUI Fix & Config-Driven Architecture Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax. Each task self-contained with exact file paths.

**Goal:** Fix WebUI, migrate from hardcoded defaults to config-file-driven setup, integrate hatago-mcp-hub features (env expansion, tags, metrics, structured logging, internal resource).

**Architecture:** Python 3.12+ / FastAPI / FastMCP 3.4+ / aiosqlite / Vanilla JS SPA. Config file `hub.config.json` replaces hardcoded `DEFAULT_SERVERS`.

**Tech Stack:** Python 3.12+, FastAPI, FastMCP 3.4+, aiosqlite, Vanilla JS/CSS, pytest

---

## Root Cause Analysis

**WebUI issue**: Two-part problem.
1. Root `/` was not serving the admin UI (fixed in 3f651be).
2. Seed servers used deprecated `@anthropic/mcp-server-*` packages; all failed with npm 404, resulting in 6 empty server cards with 0 tools. The JS had no error feedback — cards rendered silently with no tools.

**Hardcoded defaults**: `registry.py` contains a `DEFAULT_SERVERS` Python list. This should be a config file so users can customize without editing source code.

---

## File Structure

```
src/mcp_hub/
├── __init__.py
├── main.py              # Entry point — modify: load config file, JSON logging, internal resource
├── config.py            # NEW: Config file loader (reads hub.config.json, env expansion)
├── admin_router.py      # REST API — modify: metrics endpoint, status field
├── proxy_manager.py     # Proxy lifecycle — modify: status tracking, connection-time tag filtering
├── registry.py          # SQLite — modify: remove DEFAULT_SERVERS, seed from config instead
├── state.py             # Shared state — modify: metrics counters
├── env_expand.py        # NEW: ${VAR} / ${VAR:-default} expansion utility
├── static/
│   └── index.html       # WebUI SPA — modify: error states, status badges, tag filter UI
hub.config.json          # NEW: Default config file (seed servers, tags, etc.)
tests/
├── __init__.py
├── conftest.py
├── test_env_expand.py
├── test_config.py
└── test_admin_api.py
```

---

## Chunk 1: Config File System (foundation)

### Task 1.1: Create hub.config.json

**Files:**
- Create: `hub.config.json`

**What:** Default config file with MCP server definitions, replacing `registry.py`'s `DEFAULT_SERVERS`.

- [ ] **Step 1: Create hub.config.json**

```json
{
  "version": 1,
  "mcpServers": {
    "fetch": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-fetch"],
      "tags": ["web"]
    },
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "tags": ["local"]
    },
    "sequential-thinking": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
      "tags": ["reasoning"]
    },
    "git": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-git", "--repository", "."],
      "tags": ["vcs"]
    },
    "puppeteer": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-puppeteer"],
      "tags": ["browser"]
    },
    "brave-search": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-brave-search"],
      "env": {
        "BRAVE_API_KEY": "${BRAVE_API_KEY:-}"
      },
      "tags": ["search", "web"]
    }
  }
}
```

Matches hatago's config structure: `mcpServers` key, `command`/`args`/`env`/`tags`/`url` fields.

- [ ] **Step 2: Verify the file is valid JSON**

```bash
cd /home/rausraus/code/MCP-Hub && python3 -m json.tool hub.config.json > /dev/null && echo "valid"
```

- [ ] **Step 3: Commit**

```bash
git add hub.config.json .gitignore
git commit -m "feat: add hub.config.json as default MCP server config file"
```

### Task 1.2: Create config.py — Config Loader

**Files:**
- Create: `src/mcp_hub/config.py`
- Create: `tests/test_config.py`

**What:** Module that loads and validates `hub.config.json`. Supports:
- File loading with path resolution (`~`, relative, absolute)
- Environment variable expansion via `env_expand.py`
- Falls back gracefully if no config file
- **Note:** Tag filtering is connection-time (see Chunk 2.3), NOT at config load time.

- [ ] **Step 1: Write tests first**

Create `tests/test_config.py`:

```python
import json
import os
import tempfile
import pytest
from mcp_hub.config import load_config, HubConfig


class TestLoadConfig:
    def test_loads_valid_config(self, tmp_path):
        config_file = tmp_path / "hub.config.json"
        config_file.write_text(json.dumps({
            "version": 1,
            "mcpServers": {
                "test": {"command": "echo", "args": ["hello"]}
            }
        }))
        config = load_config(str(config_file))
        assert "test" in config.servers

    def test_expands_env_vars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_KEY", "secret123")
        config_file = tmp_path / "hub.config.json"
        config_file.write_text(json.dumps({
            "version": 1,
            "mcpServers": {
                "api": {
                    "url": "https://${MY_KEY}.example.com"
                }
            }
        }))
        config = load_config(str(config_file))
        assert config.servers["api"]["url"] == "https://secret123.example.com"

    def test_missing_file_returns_empty(self):
        config = load_config("/nonexistent/path.json")
        assert config.servers == {}

    def test_invalid_json_raises(self, tmp_path):
        config_file = tmp_path / "bad.json"
        config_file.write_text("{invalid}")
        with pytest.raises(ValueError, match="Invalid config"):
            load_config(str(config_file))

    def test_disabled_servers_are_skipped(self, tmp_path):
        config_file = tmp_path / "hub.config.json"
        config_file.write_text(json.dumps({
            "version": 1,
            "mcpServers": {
                "active": {"command": "echo", "args": ["hello"]},
                "off": {"command": "echo", "args": ["nope"], "disabled": True},
            }
        }))
        config = load_config(str(config_file))
        assert "active" in config.servers
        assert "off" not in config.servers

    def test_servers_retain_tags_field(self, tmp_path):
        """Tags are preserved in config — filtering happens at connection time."""
        config_file = tmp_path / "hub.config.json"
        config_file.write_text(json.dumps({
            "version": 1,
            "mcpServers": {
                "fetch": {"command": "npx", "args": ["-y", "server-fetch"], "tags": ["web"]},
            }
        }))
        config = load_config(str(config_file))
        assert config.servers["fetch"]["tags"] == ["web"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_config.py -v
```

Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement config.py**

```python
"""
Configuration file loader for MCP Hub.
Reads hub.config.json, applies env expansion and tag filtering.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .env_expand import expand_env_vars

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATHS = [
    "hub.config.json",
    "~/.config/mcp-hub/config.json",
]


@dataclass
class HubConfig:
    servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    version: int = 1
    log_level: str = "info"


def _resolve_path(path: str) -> Path:
    """Resolve ~ and relative paths."""
    return Path(path).expanduser().resolve()


def load_config(config_path: str | None = None) -> HubConfig:
    """Load and parse hub.config.json.

    Priority:
    1. Explicit config_path argument
    2. MCP_HUB_CONFIG env var
    3. Default paths (hub.config.json, ~/.config/mcp-hub/config.json)
    """
    # Determine file path
    path = config_path or os.environ.get("MCP_HUB_CONFIG")
    if path:
        resolved = _resolve_path(path)
        if not resolved.exists():
            raise FileNotFoundError(f"Config file not found: {resolved}")
        return _parse_config(resolved)

    # Try default paths
    for default_path in DEFAULT_CONFIG_PATHS:
        resolved = _resolve_path(default_path)
        if resolved.exists():
            logger.info("Using config: %s", resolved)
            return _parse_config(resolved)

    # No config found — return empty (user can add servers via API)
    logger.info("No config file found. Starting with empty server list.")
    return HubConfig()


def _parse_config(filepath: Path) -> HubConfig:
    """Parse and validate a config file."""
    try:
        raw = json.loads(filepath.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid config JSON in {filepath}: {e}") from e

    version = raw.get("version", 1)
    if not isinstance(version, int) or version < 1:
        raise ValueError(f"Unsupported config version: {version}")

    log_level = raw.get("log_level", "info")
    raw_servers = raw.get("mcpServers", raw.get("servers", {}))

    if not isinstance(raw_servers, dict):
        raise ValueError(f"mcpServers must be a dict, got {type(raw_servers)}")

    # Apply env expansion to each server config
    servers: dict[str, dict] = {}
    for name, cfg in raw_servers.items():
        if not isinstance(cfg, dict):
            continue
        # Skip disabled servers
        if cfg.get("disabled"):
            logger.info("Skipping disabled server '%s'", name)
            continue
        try:
            servers[name] = expand_env_vars(cfg)
        except ValueError as e:
            logger.warning("Skipping server '%s': %s", name, e)

    return HubConfig(
        servers=servers,
        version=version,
        log_level=log_level,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /home/rausraus/code/MCP-Hub && source .venv/bin/activate && python3 -m pytest tests/test_config.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/mcp_hub/config.py tests/test_config.py
git commit -m "feat: add hub.config.json loader with env expansion and tag filtering"
```

### Task 1.3: Create env_expand.py

**Files:**
- Create: `src/mcp_hub/env_expand.py`
- Create: `tests/test_env_expand.py`

**What:** `${VAR}` and `${VAR:-default}` expansion utility. Claude Code / hatago compatible syntax.

- [ ] **Step 1: Write tests**

Create `tests/__init__.py` (empty) and `tests/conftest.py`:

```python
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture
def clean_env(monkeypatch):
    for key in ("TEST_VAR", "API_KEY", "PORT", "BRAVE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
```

Create `tests/test_env_expand.py`:

```python
import os
import pytest
from mcp_hub.env_expand import expand_env_vars


class TestExpandString:
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

    def test_non_string_passthrough(self):
        assert expand_env_vars(42) == 42
        assert expand_env_vars(None) is None
        assert expand_env_vars(True) is True


class TestExpandDict:
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


class TestExpandList:
    def test_expand_in_list(self, monkeypatch):
        monkeypatch.setenv("PKG", "server-fetch")
        args = ["-y", "@modelcontextprotocol/${PKG}"]
        result = expand_env_vars(args)
        assert result[1] == "@modelcontextprotocol/server-fetch"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_env_expand.py -v
```

Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement env_expand.py**

```python
"""
Environment variable expansion utility.
Supports Claude Code / hatago compatible syntax:
  - ${VAR}          : Required variable (raises if undefined)
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
        default = match.group(2)
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
    """Recursively expand env var placeholders in any structure.

    Strings: expand ${VAR} placeholders.
    Dicts: recursively expand values.
    Lists: recursively expand items.
    Other types: returned as-is.
    """
    if isinstance(obj, str):
        return _expand_string(obj)
    if isinstance(obj, dict):
        return {k: expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env_vars(item) for item in obj]
    return obj
```

- [ ] **Step 4: Run tests to verify all pass**

```bash
cd /home/rausraus/code/MCP-Hub && source .venv/bin/activate && python3 -m pytest tests/test_env_expand.py -v
```

Expected: All 13 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_hub/env_expand.py tests/
git commit -m "feat: add env var expansion utility with ${VAR} and ${VAR:-default}"
```

### Task 1.4: Integrate Config into Registry & Main

**Files:**
- Modify: `src/mcp_hub/registry.py` — remove `DEFAULT_SERVERS`, accept config-fed seed
- Modify: `src/mcp_hub/main.py` — load config file, pass to registry

**What:** Registry no longer has hardcoded defaults. Instead, `main.py` loads config and seeds via `registry.seed_from_config()`.

- [ ] **Step 1: Refactor registry.py**

Remove `DEFAULT_SERVERS` constant. Change `init()` to accept optional config servers:

```python
class SqliteStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.environ.get("MCP_HUB_DB_PATH", "data/hub.db")

    async def init(self, seed_servers: dict[str, dict] | None = None) -> None:
        """テーブル作成 + 初回起動時に config からシード。"""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS servers (
                    name TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            await db.commit()

        # Allow forced re-seed via env var
        if os.environ.get("MCP_HUB_RESEED") == "1":
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DELETE FROM servers")
                await db.commit()
            logger.info("MCP_HUB_RESEED=1: wiped existing servers for re-seed")

        existing = await self.list_servers()
        if not existing and seed_servers:
            logger.info("Seeding %d servers from config...", len(seed_servers))
            for name, config in seed_servers.items():
                try:
                    await self.add_server(name, config)
                    logger.info("  Seeded: %s", name)
                except Exception:
                    logger.warning("  Failed to seed %s", name, exc_info=True)
        elif existing:
            logger.info("Found %d existing server(s). Skipping seed.", len(existing))
        else:
            logger.info("No servers in config or DB. Starting empty.")
```

- [ ] **Step 2: Update main.py**

Load config file, pass to registry:

```python
from .config import load_config

async def lifespan(app: FastAPI):
    import time
    app_state.start_time = time.time()

    # Load config
    config = load_config()

    # Apply log level from config
    if config.log_level:
        logging.getLogger().setLevel(config.log_level.upper())

    registry = SqliteStore()
    await registry.init(seed_servers=config.servers)
    ...
```

The config file path can be set via `MCP_HUB_CONFIG` env var.

**Migration note for existing users:** Upgrading from hardcoded-defaults version: existing servers in DB are preserved as-is. To force re-seed from the new config file, delete `data/hub.db` and restart. For automation, set `MCP_HUB_RESEED=1` to wipe DB on startup and re-seed from config (add this as an option in `init()` — if env var is set, skip the "existing servers" check).

- [ ] **Step 3: Add MCP_HUB_RESEED support**

In `init()`, before the existing-servers check:

```python
if os.environ.get("MCP_HUB_RESEED") == "1":
    async with aiosqlite.connect(self.db_path) as db:
        await db.execute("DELETE FROM servers")
        await db.commit()
    logger.info("MCP_HUB_RESEED=1: wiped existing servers")
    # Force re-seed
    existing = []
else:
    existing = await self.list_servers()
```

- [ ] **Step 4: Verify with clean DB**

```bash
cd /home/rausraus/code/MCP-Hub && source .venv/bin/activate && rm -f data/hub.db && MCP_HUB_PORT=26277 timeout 8 python3 -m mcp_hub.main 2>&1 | grep -E "(Seeded|seeding|Skipping|Loaded)"
```

Expected: Seeding from hub.config.json. New package names used.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_hub/registry.py src/mcp_hub/main.py src/mcp_hub/config.py
git commit -m "refactor: replace hardcoded DEFAULT_SERVERS with hub.config.json loading"
```

---

## Chunk 2: WebUI Enhancements

**Known limitation (status tracking):** `ProxyManager._status` captures only mount-time outcome. If a subprocess crashes later, status remains "connected" until hub restart. This is acceptable for v1; a periodic health-check loop can be added later.

### Task 2.1: Add Server Status to API & WebUI

**Files:**
- Modify: `src/mcp_hub/proxy_manager.py` — track connection status per server
- Modify: `src/mcp_hub/admin_router.py` — include `status` field in list response
- Modify: `src/mcp_hub/static/index.html` — render status badges

**What:** Distinguish "connected with tools" / "connected but 0 tools" / "connection failed" visually.

- [ ] **Step 1: Add status tracking in ProxyManager**

```python
def __init__(self, mcp: FastMCP, registry: SqliteStore):
    self._status: dict[str, str] = {}  # name -> "connected" | "error"

def get_all_status(self) -> dict[str, str]:
    return dict(self._status)
```

Set `self._status[name] = "connected"` on successful mount, `"error"` on failure.

- [ ] **Step 2: Include status in API response**

In `admin_router.py` `list_servers()`, add `"status": status_map.get(name, "unknown")`.

- [ ] **Step 3: Update WebUI JS**

- Add CSS for `.status-badge-sm` (green/red/yellow variants)
- In `renderServers()`, show status badge per server card
- In `loadServers()`, toast if any servers have error status

- [ ] **Step 4: Test with Playwright** (actual test, not placeholder)

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("http://localhost:26277/")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    # Assert status badges exist on server cards
    badges = page.locator(".status-badge-sm")
    assert badges.count() >= 1, "Expected at least one status badge"
    # Each badge should contain connection status text
    for i in range(badges.count()):
        text = badges.nth(i).text_content()
        assert any(status in text for status in ["接続済", "エラー", "不明"]), \
            f"Badge {i} has unexpected text: {text}"

    browser.close()
```

- [ ] **Step 5: Commit**

### Task 2.2: Add Tag Filter UI

**Files:**
- Modify: `src/mcp_hub/static/index.html` — tag filter chips + API query param

**What:** Let WebUI users filter displayed servers by tags without restarting. Adds a filter bar above the server grid with clickable tag chips (OR logic, matching backend behavior).

- [ ] **Step 1: Add tag filter UI HTML/CSS**

Above the server grid (`#serverGrid`), add a filter bar:

```html
<div id="tagFilter" class="tag-filter">
  <span class="tag-filter-label">フィルター:</span>
  <div id="tagChips"></div>
</div>
```

CSS:

```css
.tag-filter { display: flex; align-items: center; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
.tag-filter-label { font-size: 0.8rem; color: var(--text-muted); }
.tag-chip { display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 0.75rem; cursor: pointer; border: 1px solid var(--border); transition: all 0.2s; }
.tag-chip.active { background: var(--accent-purple); color: white; border-color: var(--accent-purple); }
.tag-chip:not(.active):hover { border-color: var(--accent-purple); }
```

- [ ] **Step 2: Add tag filter JavaScript logic**

In `renderServers()`, extract all unique tags from loaded servers and render chips:

```javascript
function renderTagFilters() {
    const allTags = new Set();
    servers.forEach(s => (s.config?.tags || []).forEach(t => allTags.add(t)));
    if (allTags.size === 0) {
        document.getElementById('tagFilter').style.display = 'none';
        return;
    }
    document.getElementById('tagFilter').style.display = 'flex';
    const chips = document.getElementById('tagChips');
    chips.innerHTML = '';
    allTags.forEach(tag => {
        const chip = document.createElement('span');
        chip.className = 'tag-chip' + (activeTags.has(tag) ? ' active' : '');
        chip.textContent = tag;
        chip.onclick = () => {
            if (activeTags.has(tag)) activeTags.delete(tag);
            else activeTags.add(tag);
            renderServers();
        };
        chips.appendChild(chip);
    });
}

let activeTags = new Set();

function getFilteredServers() {
    if (activeTags.size === 0) return servers;
    return servers.filter(s => 
        (s.config?.tags || []).some(t => activeTags.has(t))
    );
}
```

Call `renderTagFilters()` within `renderServers()`. Filter server cards using `getFilteredServers()`.

- [ ] **Step 3: Verify with Playwright**

```python
# Verify tag chips appear and filter works
page.locator(".tag-chip").first.click()
page.wait_for_timeout(500)
cards = page.locator(".server-card")
assert cards.count() < total_without_filter  # Filter reduced visible cards
```

- [ ] **Step 4: Commit**

---

## Chunk 3: Connection-time Tag Filtering

### Task 3.1: Per-connection Tag Filter on /mcp Endpoint

**Files:**
- Modify: `src/mcp_hub/proxy_manager.py` — `list_tools()` accepts optional `tags` filter
- Modify: `src/mcp_hub/main.py` — intercept `/mcp` query params, store tags in request context

**What:** Clients connect to `/mcp?tags=web,local` and only see tools from matching servers. No tags → all tools (backward compatible). All servers run continuously; filtering happens at tool listing time.

**Architecture:**

```
Client A → /mcp?tags=web      → list_tools(tags=["web"])     → fetch + brave-search tools
Client B → /mcp?tags=local,vcs → list_tools(tags=["local","vcs"]) → filesystem + git tools  
Client C → /mcp                → list_tools()                → all 6 servers' tools
```

- [ ] **Step 1: Add tag-filtered list_tools to ProxyManager**

```python
async def list_tools(self, tags: list[str] | None = None) -> dict[str, list[dict]]:
    """全プロキシのツール一覧を返す。tags 指定時はそのタグにマッチするサーバーのみ。"""
    result: dict[str, list[dict]] = {}

    for name, config in self._server_configs.items():
        # Tag filter (OR logic)
        if tags:
            server_tags = config.get("tags", [])
            if not any(t in server_tags for t in tags):
                continue

        try:
            tools = await self._proxies[name].list_tools()
            result[name] = [{"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in tools]
        except Exception as e:
            logger.warning("Failed to list tools for %s: %s", name, e)
            self._status[name] = "error"

    return result
```

`_server_configs` is a new dict mapping server name → config (populated during `register`/`load_all`).

- [ ] **Step 2: Store server configs in ProxyManager**

Modify `register_server()` / `load_all()`:

```python
self._server_configs: dict[str, dict] = {}

async def register_server(self, name: str, config: dict):
    self._server_configs[name] = config
    ...

async def load_all(self):
    for srv in servers:
        self._server_configs[srv["name"]] = srv["config"]
        ...
```

- [ ] **Step 3: Extract tags from /mcp query params**

In `main.py` lifespan, add middleware or wrap the FastMCP app:

```python
from contextvars import ContextVar

request_tags: ContextVar[list[str] | None] = ContextVar("request_tags", default=None)

# Middleware to extract tags from query params
@app.middleware("http")
async def extract_tags_middleware(request: Request, call_next):
    if request.url.path.startswith("/mcp"):
        tags_param = request.query_params.get("tags", "")
        if tags_param:
            request_tags.set([t.strip() for t in tags_param.split(",") if t.strip()])
    response = await call_next(request)
    request_tags.set(None)
    return response
```

- [ ] **Step 4: Wire context into ProxyManager.list_tools**

In `proxy_manager.py`, read `request_tags` context var:

```python
from mcp_hub.main import request_tags

async def list_tools(self, tags: list[str] | None = None) -> dict[str, list[dict]]:
    if tags is None:
        tags = request_tags.get(None)
    ...
```

- [ ] **Step 5: Verify with curl**

```bash
cd /home/rausraus/code/MCP-Hub && source .venv/bin/activate && rm -f data/hub.db && MCP_HUB_PORT=26280 timeout 10 python3 -m mcp_hub.main 2>&1 &
sleep 5

# No tags → all tools
echo "=== No filter ==="
curl -s -X POST http://localhost:26280/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | python3 -c "
import sys,json
d=json.load(sys.stdin)
tools=[t['name'] for t in d.get('result',{}).get('tools',[])]
print(f'{len(tools)} tools: {tools}')
"

# With tag filter
echo "=== tags=web ==="
curl -s -X POST 'http://localhost:26280/mcp?tags=web' \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | python3 -c "
import sys,json
d=json.load(sys.stdin)
tools=[t['name'] for t in d.get('result',{}).get('tools',[])]
print(f'{len(tools)} tools: {tools}')
"
```

Expected: `tags=web` returns fewer tools than unfiltered (only fetch + brave-search tagged servers).

- [ ] **Step 6: Commit**

```bash
git add src/mcp_hub/proxy_manager.py src/mcp_hub/main.py
git commit -m "feat: add per-connection tag filtering on /mcp?tags= endpoint"
```

---

## Chunk 4: Observability & Internal Resource

### Task 4.1: Metrics Endpoint

**Files:** `admin_router.py`, `state.py`

Add `GET /admin/api/metrics`: uptime, servers_registered, servers_active, total_tools, tool_calls_total, tool_call_errors.

### Task 4.2: JSON Structured Logging

**Files:** `main.py`

Support `MCP_HUB_LOG=json` for JSON-formatted log output.

### Task 4.3: `hub://servers` Resource

**Files:** `main.py`

Register FastMCP resource returning JSON snapshot of connected servers (with tags, status, tool count).

### Task 4.4: README Update

**Files:** `README.md`

Document: config file (`MCP_HUB_CONFIG`), connection-time tags (`/mcp?tags=`), env vars (`MCP_HUB_LOG`, `MCP_HUB_RESEED`), metrics endpoint, `hub://servers` resource.

---

## Chunk 5: Testing & CI

### Task 5.1: Admin API Integration Tests

**Files:** `tests/test_admin_api.py`

Test CRUD endpoints with FastAPI TestClient.

### Task 5.2: CI Test Stage

**Files:** `.github/workflows/docker.yml`, `pyproject.toml`

Add `pytest` job before Docker build. Add `pytest`, `pytest-asyncio` as dev deps.

---

## Execution Order

```
Chunk 1 (Config System) → Chunk 2 (WebUI) → Chunk 3 (Connection-time Tags)
                                                   │
                              Chunk 5 (Tests/CI) ← Chunk 4 (Observability)
```

Chunk 1 is the blocking foundation. Chunks 3 and 4 are independent of each other but both follow Chunk 1-2.

---

## Feature Comparison: MCP-Hub vs Hatago

| Feature | Hatago (TS) | MCP-Hub (after plan) |
|---------|-------------|---------------------|
| Config file (hub.config.json) | ✅ (`hatago.config.json`) | ✅ |
| Env var expansion `${VAR}` / `${VAR:-default}` | ✅ | ✅ |
| Tag-based filtering | ✅ `--tags` (startup only) | ✅ `/mcp?tags=` (per-connection, MCP-Hub unique) |
| Metrics endpoint | ✅ `/metrics` | ✅ `/admin/api/metrics` |
| JSON logging | ✅ `HATAGO_LOG=json` | ✅ `MCP_HUB_LOG=json` |
| Internal resource | ✅ `hatago://servers` | ✅ `hub://servers` |
| Admin WebUI | ❌ | ✅ (MCP-Hub unique) |
| REST API | ❌ | ✅ (MCP-Hub unique) |
| Config inheritance (`extends`) | ✅ | deferred |
| Progress notification forwarding | ✅ (automatic) | ⚠️ (FastMCP built-in) |
