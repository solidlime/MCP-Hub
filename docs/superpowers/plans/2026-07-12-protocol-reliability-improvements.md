# Protocol & Reliability Improvements

> **For agentic workers:** Execute chunks sequentially. Each chunk is self-contained and committable.

**Goal:** Add connection retry with recovery, background health monitoring, resources/prompts API endpoints, and document progress notification limitation.

**Architecture:** Retry and health monitoring form a unified connection lifecycle in `proxy_manager.py`. Lock discipline: IO/retry happens outside `self._lock`, state mutation inside. Resources/prompts endpoints query individual proxies directly.

**Tech Stack:** Python 3.12+, FastMCP 3.4+ (<3.5.0), pytest + pytest-asyncio + httpx

**Lock Discipline (critical):**
- `self._lock` protects: `_proxies`, `_server_configs`, `_status` dict mutation
- `_connect_server()` (retry + IO) runs OUTSIDE lock
- `_rebuild_mounts()` MUST be called inside lock
- Health monitor reads config under lock, does IO outside lock, writes status under lock

---

## Chunk 1: Unified Connection Lifecycle (Retry + Health)

### Design

```
                  ┌──────────────────────────┐
                  │   Health Monitor Loop     │
                  │  (every 60s, background)  │
                  └────────────┬─────────────┘
                               │
              ┌────────────────▼────────────────┐
              │       _health_check()            │
              │  1. lock → read configs/proxies │
              │  2. unlock                      │
              │  3. for each connected server:  │
              │     try list_tools(timeout=10s) │
              │     if fail → status="error"    │
              │     if success + was error →    │
              │       status="recovering"       │
              │  4. for each "recovering":      │
              │     _connect_server(retry!)     │
              │     lock → update proxies +     │
              │     status="connected"          │
              └─────────────────────────────────┘

register_server / load_all:
  1. lock → write config
  2. unlock
  3. _connect_server(retry, outside lock)
  4. lock → write proxy + status
  5. unlock
  6. list_tools (outside lock, fast)
```

### Files:
- Modify: `src/mcp_hub/proxy_manager.py` (major refactor)
- Modify: `src/mcp_hub/main.py` (lifespan: start/stop health monitor)
- Create: `tests/test_retry.py`
- Create: `tests/test_health.py`

### Step 1: Add env helpers and _connect_server

```python
# proxy_manager.py — add to ProxyManager

RETRYABLE_EXCEPTIONS = (ConnectionError, TimeoutError, OSError)

@staticmethod
def _retry_env() -> tuple[int, float]:
    """(max_retries, base_delay_seconds) from env."""
    return (
        int(os.environ.get("MCP_HUB_RETRY_MAX", "3")),
        float(os.environ.get("MCP_HUB_RETRY_DELAY", "1.0")),
    )

async def _connect_server(self, name: str, config: dict) -> "FastMCPProxy | None":
    """Create proxy + mount with retry. Call OUTSIDE asyncio.Lock.
    Returns proxy on success, None on exhaustion."""
    max_retries, base_delay = self._retry_env()
    for attempt in range(max_retries + 1):
        try:
            proxy = self._create_proxy(name, config)
            self.mcp.mount(proxy, namespace=name)
            return proxy
        except RETRYABLE_EXCEPTIONS as e:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "Retry %d/%d for %s in %.1fs: %s",
                    attempt + 1, max_retries, name, delay, e,
                )
                await asyncio.sleep(delay)
            else:
                logger.error("Exhausted %d retries for %s", max_retries, name)
        except Exception:
            # Non-retryable error — don't retry
            logger.exception("Non-retryable error connecting %s", name)
            break
    return None
```

### Step 2: Refactor load_all

```python
async def load_all(self) -> None:
    servers = await self.registry.list_servers()
    if not servers:
        logger.info("No servers to load from DB")
        return

    for srv in servers:
        name = srv["name"]
        config = srv["config"]
        async with self._lock:
            self._server_configs[name] = config
        if config.get("disabled"):
            async with self._lock:
                self._status[name] = "disabled"
            continue

        proxy = await self._connect_server(name, config)
        async with self._lock:
            if proxy is not None:
                self._proxies[name] = proxy
                self._status[name] = "connected"
            else:
                self._status[name] = "error"
```

### Step 3: Refactor register_server

```python
async def register_server(self, name: str, config: dict) -> list[str]:
    await self.registry.add_server(name, config)
    async with self._lock:
        self._server_configs[name] = config

    if config.get("disabled"):
        async with self._lock:
            self._status[name] = "disabled"
        return []

    proxy = await self._connect_server(name, config)
    if proxy is None:
        async with self._lock:
            self._status[name] = "error"
        return []

    async with self._lock:
        self._proxies[name] = proxy
        self._status[name] = "connected"

    # list_tools outside lock (fast network call)
    try:
        tools = await proxy.list_tools()
        return [t.name for t in tools]
    except Exception:
        logger.warning("Could not list tools for %s", name)
        async with self._lock:
            self._status[name] = "error"
        return []
```

### Step 4: Add health monitor with recovery

```python
# __init__ addition:
self._health_task: asyncio.Task | None = None

async def _health_check(self) -> None:
    """Check all connected servers, recover failed ones."""
    # Snapshots under lock (prevents dict mutation during iteration)
    async with self._lock:
        proxies_snapshot = dict(self._proxies)
        configs_snapshot = dict(self._server_configs)
        status_snapshot = dict(self._status)

    timeout = int(os.environ.get("MCP_HUB_HEALTH_TIMEOUT", "10"))
    to_recover: list[str] = []

    for name, proxy in proxies_snapshot.items():
        config = configs_snapshot.get(name, {})
        if config.get("disabled"):
            continue
        try:
            await asyncio.wait_for(proxy.list_tools(), timeout=timeout)
            # Was in error → mark recovering
            if status_snapshot.get(name) == "error":
                logger.info("Server %s appears reachable — attempting recovery", name)
                async with self._lock:
                    self._status[name] = "recovering"
                to_recover.append(name)
        except asyncio.TimeoutError:
            logger.warning("Health check timeout for %s", name)
            async with self._lock:
                self._status[name] = "error"
        except asyncio.CancelledError:
            raise
        except Exception:
            if status_snapshot.get(name) == "connected":
                logger.warning("Server %s health check failed", name)
            async with self._lock:
                self._status[name] = "error"

    # Recovery: reconnect failed servers that HAVE a proxy (outside lock for IO)
    for name in to_recover:
        config = configs_snapshot.get(name, {})
        if not config:
            continue
        proxy = await self._connect_server(name, config)
        async with self._lock:
            if proxy is not None:
                self._proxies[name] = proxy
                self._status[name] = "connected"
                logger.info("Server %s recovered", name)
            else:
                self._status[name] = "error"

    # Recovery: servers that failed initial connection (status="error", no proxy in _proxies)
    for name, config in configs_snapshot.items():
        if config.get("disabled"):
            continue
        if name in proxies_snapshot:
            continue  # already handled above
        if status_snapshot.get(name) != "error":
            continue
        # Attempt initial recovery
        logger.info("Attempting recovery for %s (never connected)", name)
        proxy = await self._connect_server(name, config)
        async with self._lock:
            if proxy is not None:
                self._proxies[name] = proxy
                self._status[name] = "connected"
                logger.info("Server %s recovered (initial)", name)
            # else: stays "error", will retry next interval

async def _health_monitor_loop(self, interval: int) -> None:
    """Background loop. Never dies — exceptions are caught and logged."""
    while True:
        await asyncio.sleep(interval)
        try:
            await self._health_check()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Health monitor iteration failed — will retry")

def start_health_monitor(self, interval: int | None = None) -> None:
    """Start background health check. Cancels any existing task first."""
    if interval is None:
        interval = int(os.environ.get("MCP_HUB_HEALTH_INTERVAL", "60"))
    if interval <= 0:
        return
    # Cancel existing task to prevent zombie
    if self._health_task and not self._health_task.done():
        self._health_task.cancel()
    self._health_task = asyncio.create_task(self._health_monitor_loop(interval))
    logger.info("Health monitor started (interval=%ds)", interval)

async def stop_health_monitor(self) -> None:
    """Cancel background health task. Safe to call multiple times."""
    if self._health_task and not self._health_task.done():
        self._health_task.cancel()
        try:
            await self._health_task
        except asyncio.CancelledError:
            pass
    self._health_task = None
```

### Step 5: Integrate with main.py lifespan

```python
# main.py — lifespan()
async with asynccontextmanager
async def lifespan(app: FastAPI):
    # ... existing init ...
    await proxy_manager.load_all()

    # Start health monitor AFTER all servers loaded
    proxy_manager.start_health_monitor()

    # Mount MCP sub-app
    # ... existing mount code ...

    yield  # <-- app runs here

    # Shutdown: stop health monitor BEFORE cleaning up proxies
    await proxy_manager.stop_health_monitor()
    logger.info("MCP Hub shutting down")
```

### Step 6: Tests

Create `tests/test_retry.py`:
- `test_retry_env_defaults` — returns (3, 1.0)
- `test_retry_env_custom` — env vars override
- `test_connect_server_retries_on_transient_error` — retries ConnectionError
- `test_connect_server_exhausts_retries` — returns None after max
- `test_connect_server_no_retry_on_valueerror` — ValueError not retried
- `test_load_all_connects_after_retry` — load_all with retry
- `test_load_all_marks_error_on_exhaustion` — load_all exhausted
- `test_register_server_handles_none_proxy` — no NoneType error
- `test_register_server_retry_succeeds` — register with retry

Create `tests/test_health.py`:
- `test_health_check_detects_failure` — error status on list_tools fail
- `test_health_check_detects_recovery` — status transitions error→recovering→connected
- `test_health_check_skips_disabled` — disabled servers untouched
- `test_health_check_timeout` — asyncio.wait_for wrapper works
- `test_health_monitor_loop_survives_exceptions` — continue after _health_check raises
- `test_health_monitor_no_zombie_on_restart` — old task cancelled
- `test_start_stop_health_monitor` — lifecycle

### Step 7: Commit

```bash
git add src/mcp_hub/proxy_manager.py src/mcp_hub/main.py tests/test_retry.py tests/test_health.py
git commit -m "feat: unified connection lifecycle with retry and health monitoring

- _connect_server(name, config) — retry with exponential backoff (outside lock)
- Health monitor: background task detects failures and auto-recovers
- Recovery path: error → _connect_server → connected
- Lock discipline: IO/retry outside self._lock, state mutation inside
- Config: MCP_HUB_RETRY_MAX(3), MCP_HUB_RETRY_DELAY(1.0), MCP_HUB_HEALTH_INTERVAL(60), MCP_HUB_HEALTH_TIMEOUT(10)"
```

---

## Chunk 2: Resources & Prompts API

### Design

Resources/prompts endpoints query individual proxy directly (not all servers):

```python
@router.get("/servers/{name}/resources")
async def list_server_resources(name: str):
    pm = _get_proxy_manager()
    proxy = pm.get_proxy(name)
    if proxy is None:
        raise HTTPException(404, f"Server {name!r} not found or not connected")
    try:
        resources = await proxy.list_resources()
        return {"resources": [
            {"uri": str(r.uri), "name": r.name, "description": r.description or ""}
            for r in resources
        ]}
    except Exception:
        raise HTTPException(502, f"Failed to list resources from {name}")
```

### Files:
- Modify: `src/mcp_hub/admin_router.py` (new endpoints, use `pm.get_proxy(name)` directly)
- Modify: `src/mcp_hub/static/index.html` (show counts, optional)

### Steps:

- [ ] Add `GET /servers/{name}/resources` — uses `pm.get_proxy(name).list_resources()` (1 RPC call per request)
- [ ] Add `GET /servers/{name}/prompts` — uses `pm.get_proxy(name).list_prompts()`
- [ ] Add `GET /servers/{name}/resource-templates` — uses `pm.get_proxy(name).list_resource_templates()`
- [ ] Add tests: connected server returns resources, disconnected returns 404/502, nonexistent returns 404
- [ ] Update WebUI: show resource/prompt/template counts in server cards

---

## Chunk 3: Progress Notification Forwarding

### Research result: Not feasible with current FastMCP

`FastMCPProxy.call_tool(name, arguments)` does not accept `_meta`/`progressToken`.
`ProxyProvider` has zero notification forwarding code.

**Reference: hatago-mcp-hub** supports this via `onprogress` callback in `client.callTool()`:
- Extracts `progressToken` from `tools/call._meta`
- Passes `onprogress` callback to child server
- Forwards `notifications/progress` via STDIO (`onNotification`), StreamableHTTP (`sendProgressNotification`), and SSE (`SSEManager.sendProgress`)

### Future implementation path (not in this PR):
1. FastMCP PR: expose `onprogress` callback in `FastMCPProxy.call_tool()`
2. Custom proxy layer: subclass ProxyProvider or replace with hand-rolled MCP client proxy
3. Workaround: tool call wrapper in the hub's MCP server that intercepts `tools/call` → extracts `progressToken` → injects callback → calls child

### Actions (this PR):
- Create `docs/progress-forwarding.md` — limitation doc with hatago reference
- Update README known limitations section

---

## Verification

```bash
pytest tests/ -v          # all pass (39 + ~15 new = ~54)
docker compose up --build # health monitor visible in logs
```

## Dependency Order

```
Chunk 1 (Retry + Health) ──→ Chunk 2 (Resources/Prompts)
                               Chunk 3 (Progress doc — parallel, no deps)
```
