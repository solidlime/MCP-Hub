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
├── config.py            # NEW: Config file loader (reads hub.config.json, env expansion, tag filter)
├── admin_router.py      # REST API — modify: metrics endpoint, status field
├── proxy_manager.py     # Proxy lifecycle — modify: status tracking, tag filtering
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
  "$schema": "https://raw.githubusercontent.com/rausraus/mcp-hub/main/schemas/config.schema.json",
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
- Tag filtering
- Falls back gracefully if no config file

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

    def test_tag_filtering(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_HUB_TAGS", "web")
        config_file = tmp_path / "hub.config.json"
        config_file.write_text(json.dumps({
            "version": 1,
            "mcpServers": {
                "fetch": {"command": "npx", "args": ["-y", "server-fetch"], "tags": ["web"]},
                "filesystem": {"command": "npx", "args": ["-y", "server-fs"], "tags": ["local"]},
            }
        }))
        config = load_config(str(config_file))
        assert "fetch" in config.servers
        assert "filesystem" not in config.servers
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

    log_level = raw.get("logLevel", raw.get("log_level", "info"))
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
            continue
        try:
            servers[name] = expand_env_vars(cfg)
        except ValueError as e:
            logger.warning("Skipping server '%s': %s", name, e)

    # Tag filtering
    filter_tags = os.environ.get("MCP_HUB_TAGS", "").split(",")
    filter_tags = [t.strip() for t in filter_tags if t.strip()]
    if filter_tags:
        filtered = {}
        for name, cfg in servers.items():
            server_tags = cfg.get("tags", [])
            if any(t in server_tags for t in filter_tags):
                filtered[name] = cfg
            else:
                logger.info("Skipping '%s': tags %s don't match filter %s", name, server_tags, filter_tags)
        servers = filtered

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

Same implementation as before. Reuse from previous plan's Task 2.1.

- [ ] **Step 1: Write tests** (see previous plan's test_env_expand.py)
- [ ] **Step 2: Implement** (see previous plan's env_expand.py)
- [ ] **Step 3: Verify tests pass**
- [ ] **Step 4: Commit**

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

- [ ] **Step 3: Verify with clean DB**

```bash
cd /home/rausraus/code/MCP-Hub && source .venv/bin/activate && rm -f data/hub.db && MCP_HUB_PORT=26277 timeout 8 python3 -m mcp_hub.main 2>&1 | grep -E "(Seeded|seeding|Skipping|Loaded)"
```

Expected: Seeding from hub.config.json. New package names used.

- [ ] **Step 4: Commit**

```bash
git add src/mcp_hub/registry.py src/mcp_hub/main.py src/mcp_hub/config.py
git commit -m "refactor: replace hardcoded DEFAULT_SERVERS with hub.config.json loading"
```

---

## Chunk 2: WebUI Enhancements

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

- Add CSS for `.status-badge-sm`
- In `renderServers()`, show status badge per server card
- In `loadServers()`, toast if any servers have error status

- [ ] **Step 4: Test with Playwright**

```python
# Verify status badges visible, error toasts appear for failed servers
```

- [ ] **Step 5: Commit**

---

## Chunk 3: Observability (independent)

### Task 3.1: Metrics Endpoint

**Files:** `admin_router.py`, `state.py`

Add `GET /admin/api/metrics`: uptime, servers_registered, servers_active, total_tools, tool_calls_total, tool_call_errors.

### Task 3.2: JSON Structured Logging

**Files:** `main.py`

Support `MCP_HUB_LOG=json` for JSON-formatted log output.

---

## Chunk 4: Internal Resource & Misc

### Task 4.1: `hub://servers` Resource

**Files:** `main.py`

Register FastMCP resource returning JSON snapshot of connected servers.

### Task 4.2: README Update

**Files:** `README.md`

Document new config file, env vars (`MCP_HUB_CONFIG`, `MCP_HUB_TAGS`, `MCP_HUB_LOG`), tags, metrics, etc.

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
Chunk 1 (Config System) ──► Chunk 2 (WebUI) ──► Chunk 3 (Observability)
     │                                              │
     └──► Chunk 5 (Tests/CI) ◄── Chunk 4 (Resource + Docs)
```

Chunk 1 is the blocking foundation. Chunks 3 and 4 are independent and can run in parallel after Chunk 1.

---

## Feature Comparison: MCP-Hub vs Hatago

| Feature | Hatago (TS) | MCP-Hub (after plan) |
|---------|-------------|---------------------|
| Config file (hub.config.json) | ✅ (`hatago.config.json`) | ✅ |
| Env var expansion `${VAR}` / `${VAR:-default}` | ✅ | ✅ |
| Tag-based filtering | ✅ | ✅ |
| Metrics endpoint | ✅ `/metrics` | ✅ `/admin/api/metrics` |
| JSON logging | ✅ `HATAGO_LOG=json` | ✅ `MCP_HUB_LOG=json` |
| Internal resource | ✅ `hatago://servers` | ✅ `hub://servers` |
| Admin WebUI | ❌ | ✅ (MCP-Hub unique) |
| REST API | ❌ | ✅ (MCP-Hub unique) |
| Config inheritance (`extends`) | ✅ | deferred |
| Progress notification forwarding | ✅ (automatic) | ⚠️ (FastMCP built-in) |
