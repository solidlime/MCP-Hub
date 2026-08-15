"""
FastMCP の create_proxy + mount を管理。
動的なサーバー追加/削除に対応。
"""

import asyncio
import logging
import os
import re
import traceback
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.client.transports.http import StreamableHttpTransport
from fastmcp.client.transports.sse import SSETransport
from fastmcp.client.transports.stdio import StdioTransport
from fastmcp.server.providers import proxy as _proxy_providers
from fastmcp.server.providers.proxy import FastMCPProxy

from .env_expand import expand_env_vars
from .store import JsonStore
from .validators import bearer_headers_from_env

logger = logging.getLogger(__name__)

# === Resilient roots forwarding ===
# When upstream servers request roots during initialization (before a client
# connects to MCP-Hub), the default handler calls ctx.list_roots() which fails
# with "session is not available".  Replace with a resilient version that
# returns empty roots instead of raising RuntimeError.
_orig_default_roots = _proxy_providers.default_proxy_roots_handler


async def _resilient_default_roots(*args: Any, **kwargs: Any) -> list[Any]:
    try:
        return await _orig_default_roots(*args, **kwargs)
    except RuntimeError:
        logger.debug(
            "Roots forwarding skipped — no client session available yet"
        )
        return []


_proxy_providers.default_proxy_roots_handler = _resilient_default_roots


RETRYABLE_EXCEPTIONS = (ConnectionError, TimeoutError, OSError)


class ProxyManager:
    """プロキシサーバーのライフサイクル管理。

    複数の MCP サーバーへのプロキシを保持し、
    メインの FastMCP インスタンスに mount/unmount する。
    """

    def __init__(self, mcp: FastMCP, registry: "JsonStore"):
        self.mcp = mcp
        self.registry = registry
        self._proxies: dict[str, FastMCPProxy] = {}
        self._clients: dict[str, Client] = {}  # 接続済み upstream Client（session 再利用のため保持）
        self._server_configs: dict[str, dict] = {}
        self._status: dict[str, str] = {}
        self._tool_counts: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._rebuilding: bool = False  # Protected by self._lock
        self._refreshing: set[str] = set()  # Protected by self._lock
        self._health_task: asyncio.Task | None = None
        self._on_change_callbacks: list[Callable] = []
        self._rebuild_complete = asyncio.Event()
        self._rebuild_complete.set()  # initially not rebuilding
        # Concurrency cap for tool calls (prevents DoS via unlimited process/connection spawn)
        _max_calls = int(os.environ.get("MCP_HUB_MAX_CONCURRENT_CALLS", "50"))
        self._call_semaphore = asyncio.Semaphore(_max_calls)
        self._tool_cache: dict[str, tuple[float, list[Any]]] = {}  # server_name -> (timestamp, tools)
        self._health_failures: dict[str, int] = {}  # server_name -> consecutive health-check failures

    @staticmethod
    def _retry_env() -> tuple[int, float]:
        """(max_retries, base_delay_seconds) from env."""
        return (
            int(os.environ.get("MCP_HUB_RETRY_MAX", "3")),
            float(os.environ.get("MCP_HUB_RETRY_DELAY", "1.0")),
        )

    def _client_timeout(self) -> float | None:
        """Read timeout for upstream requests (seconds).
        Priority: DB setting → MCP_HUB_CLIENT_TIMEOUT env → 30.0 (SDK httpx default)."""
        data = getattr(self.registry, "_data", None)
        db = data.get("client_timeout") if isinstance(data, dict) else None
        if db is not None:
            return float(db)
        raw = os.environ.get("MCP_HUB_CLIENT_TIMEOUT")
        return float(raw) if raw else 30.0

    def _connect_timeout(self) -> float:
        """Initial connectivity check timeout (seconds).
        Priority: DB setting → MCP_HUB_CONNECT_TIMEOUT env → 30.0."""
        data = getattr(self.registry, "_data", None)
        db = data.get("connect_timeout") if isinstance(data, dict) else None
        if db is not None:
            return float(db)
        return float(os.environ.get("MCP_HUB_CONNECT_TIMEOUT", "30.0"))

    async def _connect_server(self, name: str, config: dict) -> "FastMCPProxy | None":
        """Create proxy + mount with retry. Call OUTSIDE asyncio.Lock.
        Returns proxy on success, None on exhaustion."""
        max_retries, base_delay = self._retry_env()
        prev_client: Client | None = None
        try:
            for attempt in range(max_retries + 1):
                # 前回試行の client を破棄（mount 失敗 = proxy 未使用なので安全。
                # _clients[name] に登録されているのが自分が作った client と同一の
                # 場合のみ pop してから close する）
                if prev_client is not None:
                    async with self._lock:
                        if self._clients.get(name) is prev_client:
                            self._clients.pop(name, None)
                    await prev_client.close()
                    prev_client = None
                try:
                    proxy, client = await self._create_proxy(name, config)
                    prev_client = client
                    self.mcp.mount(proxy, namespace=name)
                    prev_client = None  # 成功: client は保持対象から外す（finally が close しないように）
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
        finally:
            # ループ終了時（成功以外）に最後の client が open のまま残らないようにする
            if prev_client is not None:
                async with self._lock:
                    if self._clients.get(name) is prev_client:
                        self._clients.pop(name, None)
                await prev_client.close()

    async def _connect_and_mount(self, name: str, config: dict) -> None:
        """Single-shot connect + mount (no retry). Used by load_all() for
        non-blocking startup.  Verifies connectivity via list_tools() before
        registering the proxy — avoids mounting broken proxies that would
        trigger cascading rebuild_index failures or SSE reconnection loops."""
        client: Client | None = None
        try:
            proxy, client = await self._create_proxy(name, config)
            # Verify the proxy actually works before registering.
            # A broken server (e.g. URL endpoint returning 405) fails here
            # and is left for the health monitor to recover at its own pace.
            import time
            tools = list(await asyncio.wait_for(proxy.list_tools(), timeout=self._connect_timeout()))
            self._tool_cache[name] = (time.monotonic(), tools)
            self._tool_counts[name] = len(tools)
            async with self._lock:
                if name not in self._server_configs:
                    return  # リネーム/削除済み — ゾンビ復活を防ぐ
                self.mcp.mount(proxy, namespace=name)
                self._proxies[name] = proxy
                self._status[name] = "connected"
            logger.info("Server %s connected (background)", name)
            # Notify listeners so meta index can rebuild
            await self._notify_change(name, "connected", {"tool_count": len(tools)})
        except asyncio.TimeoutError:
            logger.warning("Server %s connection timed out — health monitor will retry", name)
            if client is not None:
                async with self._lock:
                    if self._clients.get(name) is client:
                        self._clients.pop(name, None)
                        await client.close()  # 自分が登録した client のみ close
            async with self._lock:
                self._status[name] = "error"
            await self._notify_change(name, "spawn_failed", {"error": "Connection timed out"})
        except Exception:
            logger.warning(
                "Server %s failed initial connection — health monitor will retry",
                name, exc_info=True,
            )
            if client is not None:
                async with self._lock:
                    if self._clients.get(name) is client:
                        self._clients.pop(name, None)
                        await client.close()  # 自分が登録した client のみ close
            async with self._lock:
                self._status[name] = "error"
            await self._notify_change(name, "spawn_failed", {
                "error": "Connection failed",
                "detail": traceback.format_exc()[:500],
            })

    async def load_all(self) -> None:
        """DB から全サーバーをバックグラウンドで読み込んでマウント。

        起動時のブロッキングを避けるため、各サーバー接続は
        asyncio.create_task で起動し即座に return する。
        失敗した接続はヘルスモニターがリカバリする。
        """
        servers = await self.registry.list_servers()
        if not servers:
            logger.info("No servers to load from DB")
            return

        launched = 0
        for srv in servers:
            name = srv["name"]
            config = srv["config"]
            async with self._lock:
                self._server_configs[name] = config
            if config.get("disabled"):
                async with self._lock:
                    self._status[name] = "disabled"
                continue

            async with self._lock:
                self._status[name] = "connecting"
            asyncio.create_task(self._connect_and_mount(name, config))
            launched += 1

        logger.info("Launched %d server connections in background", launched)

    async def register_server(self, name: str, config: dict) -> dict:
        """サーバー登録 + DB保存。接続はバックグラウンドで行う。

        Returns:
            {"name": str, "status": str, "config": dict}
        """
        await self.registry.add_server(name, config)
        async with self._lock:
            self._server_configs[name] = config

        if config.get("disabled"):
            async with self._lock:
                self._status[name] = "disabled"
            return {"name": name, "status": "disabled", "config": config}

        async with self._lock:
            self._status[name] = "connecting"

        # Start background connection (like load_all does)
        asyncio.create_task(self._connect_and_mount(name, config))

        return {"name": name, "status": "connecting", "config": config}

    async def unregister_server(self, name: str) -> bool:
        """サーバー削除 + アンマウント。"""
        existed = await self.registry.remove_server(name)
        if not existed:
            return False

        async with self._lock:
            self._proxies.pop(name, None)
            client = self._clients.pop(name, None)
            self._server_configs.pop(name, None)
            self._status.pop(name, None)
            self._tool_counts.pop(name, None)
            self._tool_cache.pop(name, None)
            await self._rebuild_mounts()

        if client is not None:
            await client.close()  # ゾンビ接続防止

        await self._notify_change(name, "removed", None)
        return True

    async def refresh_server(self, name: str, config: dict) -> None:
        """プロキシの再生成 + 設定更新。disable 時はアンマウントのみ。"""
        # --- Phase 1: 状態更新（ロック保護）---
        needs_remount = False
        refreshed = False
        is_disabled = config.get("disabled", False)
        old_client = None
        async with self._lock:
            self._refreshing.add(name)
            self._server_configs[name] = config
            self._status[name] = "disabled" if is_disabled else "connected"
            old_proxy = self._proxies.pop(name, None)
            old_client = self._clients.pop(name, None)
            self._tool_cache.pop(name, None)
            if old_proxy:
                needs_remount = True

        # --- Phase 2: 副作用（ロック外）---
        try:
            if needs_remount:
                async with self._lock:
                    await self._rebuild_mounts()

            if old_client is not None:
                await old_client.close()  # ゾンビ接続防止

            if not is_disabled:
                # プロキシ生成（サブプロセス起動を含む可能性があるためロック外）
                try:
                    proxy, _client = await self._create_proxy(name, config)
                    async with self._lock:
                        if name not in self._server_configs:
                            return  # リネーム/削除済み — ゾンビ復活を防ぐ
                        self._proxies[name] = proxy
                        self.mcp.mount(proxy, namespace=name)
                        self._status[name] = "connected"
                    logger.info("Refreshed server %s", name)
                    refreshed = True
                except Exception:
                    logger.exception("Failed to refresh server %s", name)
                    async with self._lock:
                        self._status[name] = "error"

            # Callbacks outside lock — they may perform IO (rebuild_index calls list_tools)
            # "updated" fires only when the proxy was actually regenerated (refreshed).
            # A tags-only PATCH or a pure disable remount does NOT regenerate the
            # proxy, so no misleading "updated" event is emitted for it.
            if refreshed:
                await self._notify_change(name, "updated", {"disabled": bool(is_disabled)})
        finally:
            async with self._lock:
                self._refreshing.discard(name)

    async def update_config_only(self, name: str, config: dict) -> None:
        """プロキシに影響しない設定（tags 等）のみの更新。

        プロキシ再生成・サブプロセス再起動を伴わない。tags は
        server_tags() 経由で動的に参照されるため、ここでの更新で
        タグフィルタ・meta index に即時反映される。
        """
        async with self._lock:
            self._server_configs[name] = config

    async def rename_server(self, old_name: str, new_name: str, config: dict) -> None:
        """サーバー名変更。プロキシインスタンスは再利用（接続維持）。"""
        async with self._lock:
            self._refreshing.add(old_name)
            self._refreshing.add(new_name)
            if new_name in self._proxies or new_name in self._server_configs:
                self._refreshing.discard(old_name)
                self._refreshing.discard(new_name)
                raise ValueError(f"Server '{new_name}' already exists")
            proxy = self._proxies.pop(old_name, None)
            client = self._clients.pop(old_name, None)
            status = self._status.pop(old_name, "unknown")
            tc = self._tool_counts.pop(old_name, None)
            cache = self._tool_cache.pop(old_name, None)
            self._server_configs.pop(old_name, None)
            if proxy is not None:
                self._proxies[new_name] = proxy  # 接続維持
                await self._rebuild_mounts()
            if client is not None:
                self._clients[new_name] = client  # 接続維持
            self._server_configs[new_name] = config
            self._status[new_name] = status
            if tc is not None:
                self._tool_counts[new_name] = tc
            if cache is not None:
                self._tool_cache[new_name] = cache  # キャッシュ移動で再列挙回避
        await self._notify_change(new_name, "renamed", {"old_name": old_name})
        async with self._lock:
            self._refreshing.discard(old_name)
            self._refreshing.discard(new_name)

    def get_all_status(self) -> dict[str, str]:
        """全サーバーのステータス一覧。"""
        return dict(self._status)

    def get_servers_info(self) -> list[dict]:
        """Return consistent snapshot of all server metadata.

        Safe to call from sync contexts (no await points). In asyncio, sync
        functions run atomically — no event-loop preemption between dict reads.
        All four dicts are read within one event-loop tick.
        """
        servers_info = []
        for name, config in self._server_configs.items():
            servers_info.append({
                "name": name,
                "disabled": config.get("disabled", False),
                "tags": config.get("tags", []),
                "status": self._status.get(name, "unknown"),
                "tool_count": self._tool_counts.get(name, 0),
            })
        return servers_info

    async def list_tools(self, tags: list[str] | None = None) -> dict[str, list[dict]]:
        """全サーバーのツール一覧。オプションの tags フィルター。"""
        from .state import request_tags, tags_match  # no circular dep needed; state is shared

        if tags is None:
            tags = request_tags.get(None)

        logger.debug("list_tools called with tags=%s", tags)

        import time
        timeout = float(os.environ.get("MCP_HUB_LIST_TOOLS_TIMEOUT", "10.0"))
        max_failures = int(os.environ.get("MCP_HUB_HEALTH_MAX_FAILURES", "3"))

        # Snapshot under lock to prevent dict-mutation-during-iteration races
        async with self._lock:
            proxies_snapshot = dict(self._proxies)
            configs_snapshot = dict(self._server_configs)
            status_snapshot = dict(self._status)

        result: dict[str, list[dict]] = {}
        pending: list[tuple[str, Any]] = []  # (name, proxy) — gather 対象
        for srv_name, proxy in proxies_snapshot.items():
            # Tag filter (OR logic)
            if tags:
                config = configs_snapshot.get(srv_name, {})
                server_tags = config.get("tags", [])
                if not tags_match(tags, server_tags):
                    continue

            # Error-state servers: skip proxy.list_tools() — an unresponsive
            # upstream would block this call indefinitely. Serve cached tools
            # if still fresh, otherwise [].
            if status_snapshot.get(srv_name) == "error":
                cached = self._tool_cache.get(srv_name)
                if cached and time.monotonic() - cached[0] < 60.0:
                    result[srv_name] = [
                        {"name": t.name, "description": t.description or ""} for t in cached[1]
                    ]
                else:
                    result[srv_name] = []
                continue

            pending.append((srv_name, proxy))

        async def _fetch_one(name: str, proxy: Any) -> tuple[str, list[dict]]:
            try:
                tools = await asyncio.wait_for(
                    self.list_tools_for_server(name, proxy), timeout=timeout
                )
                self._health_failures.pop(name, None)  # success resets counter
                return name, [{"name": t.name, "description": t.description or ""} for t in tools]
            except asyncio.TimeoutError:
                logger.warning("list_tools timed out for %s after %.1fs", name, timeout)
                if self._record_health_failure(name, "timeout", max_failures):
                    await self._mark_error(name, f"list_tools timeout after {timeout}s")
                return name, []
            except Exception:
                logger.warning("Failed to list tools for %s", name)
                if self._record_health_failure(name, "exception", max_failures):
                    await self._mark_error(name, "list_tools failed")
                return name, []

        # 並列実行（gather は入力順を保持。name もタスクに紐づけて二重に保証）
        if pending:
            for name, tools in await asyncio.gather(
                *(_fetch_one(n, p) for n, p in pending)
            ):
                result[name] = tools

        return result

    async def call_tool(self, server_name: str, tool_name: str,
                        arguments: dict) -> Any:
        """ツール実行。asyncio.Event で rebuild 完了を待ち、Semaphore で同時実行数を制限。"""
        # Wait for any ongoing rebuild to complete (with timeout)
        timeout = int(os.environ.get("MCP_HUB_CALL_TOOL_TIMEOUT", "30"))
        try:
            await asyncio.wait_for(self._rebuild_complete.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("Server mounts rebuild timed out — retry later")

        async with self._lock:
            proxy = self._proxies.get(server_name)
        if proxy is None:
            raise ValueError(f"Server {server_name!r} not found")

        async with self._call_semaphore:
            return await proxy.call_tool(tool_name, arguments)

    def on_change(self, callback: Callable) -> None:
        """Register a callback invoked after server lifecycle events.

        New signature: callback(name: str, event: str, detail: dict | None)
        where event is one of:
          "connected" | "disconnected" | "spawn_failed" | "recovered"
          | "removed" | "updated"
        """
        self._on_change_callbacks.append(callback)

    async def _notify_change(self, name: str, event: str, detail: dict | None = None) -> None:
        """Fire all on_change callbacks with the new signature, protected."""
        for cb in self._on_change_callbacks:
            try:
                await cb(name, event, detail)
            except Exception:
                logger.warning(
                    "on_change callback failed for %s (%s)", name, event, exc_info=True
                )

    def get_proxy(self, name: str) -> FastMCPProxy | None:
        """プロキシインスタンスを取得。"""
        return self._proxies.get(name)

    def proxy_to_name(self, proxy_id: int) -> str | None:
        """id(proxy) → server_name の逆引き。TagFilterMiddleware 用。"""
        for name, proxy in self._proxies.items():
            if id(proxy) == proxy_id:
                return name
        return None

    def server_tags(self, name: str) -> list[str]:
        """サーバーの設定タグ一覧。TagFilterMiddleware 用。"""
        return self._server_configs.get(name, {}).get("tags", [])

    def get_connected_servers(self) -> dict[str, Any]:
        """Return snapshot of connected proxy instances.

        Returns a dict mapping server_name → proxy. This exposes only
        connected servers (not disabled or errored ones).
        Use this instead of accessing _proxies directly.
        """
        return dict(self._proxies)

    async def _list_tools_with_retry(self, proxy: FastMCPProxy, name: str,
                                     max_retries: int = 2) -> list[Any]:
        """Call proxy.list_tools() with retry on transient connection errors."""
        retry_delay = float(os.environ.get("MCP_HUB_LIST_TOOLS_RETRY_DELAY", "0.3"))
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                return list(await proxy.list_tools())
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    logger.debug(
                        "list_tools for %s attempt %d/%d failed: %s — retrying in %.1fs",
                        name, attempt + 1, max_retries + 1, e, retry_delay,
                    )
                    await asyncio.sleep(retry_delay)
        raise last_error  # type: ignore[misc]

    async def list_tools_for_server(self, name: str, proxy: FastMCPProxy, cache_ttl: float = 60.0) -> list[Any]:
        """List tools with caching. Returns cached result if within TTL."""
        import time
        now = time.monotonic()
        if name in self._tool_cache:
            ts, tools = self._tool_cache[name]
            if now - ts < cache_ttl:
                return tools
        tools = await self._list_tools_with_retry(proxy, name)
        self._tool_cache[name] = (now, tools)
        self._tool_counts[name] = len(tools)
        return tools

    def _record_health_failure(self, name: str, reason: str, max_failures: int) -> bool:
        """Count a health-check failure; return True when the server should be marked error.

        Transient failures (slow external APIs like Mapbox) must not flip a
        server to error on the first timeout. After max_failures consecutive
        failures the server is marked error, and the counter resets so it takes
        max_failures failures again before re-marking (prevents event spam).
        """
        failures = self._health_failures.get(name, 0) + 1
        self._health_failures[name] = failures
        if failures < max_failures:
            logger.warning(
                "Health check failure for %s (%d/%d): %s — keeping connected",
                name, failures, max_failures, reason,
            )
            return False
        logger.warning("Health check failure for %s (%d consecutive): %s — marking error", name, failures, reason)
        self._health_failures[name] = 0
        return True

    async def _mark_error(self, name: str, error_msg: str) -> None:
        """Mark a server as error and notify disconnect."""
        async with self._lock:
            self._status[name] = "error"
        await self._notify_change(name, "disconnected", {"error": error_msg})

    async def _health_check(self) -> None:
        """Check all connected servers, recover failed ones."""
        # Snapshots under lock (prevents dict mutation during iteration)
        async with self._lock:
            proxies_snapshot = dict(self._proxies)
            configs_snapshot = dict(self._server_configs)
            status_snapshot = dict(self._status)

        timeout = int(os.environ.get("MCP_HUB_HEALTH_TIMEOUT", "10"))
        max_failures = int(os.environ.get("MCP_HUB_HEALTH_MAX_FAILURES", "3"))
        to_recover: list[str] = []

        for name, proxy in proxies_snapshot.items():
            config = configs_snapshot.get(name, {})
            if config.get("disabled"):
                continue
            try:
                tools = await asyncio.wait_for(
                    self.list_tools_for_server(name, proxy), timeout=timeout
                )
                async with self._lock:
                    self._tool_counts[name] = len(tools)
                self._health_failures.pop(name, None)  # success resets counter
                # Was in error → mark recovering
                if status_snapshot.get(name) == "error":
                    logger.info("Server %s appears reachable — attempting recovery", name)
                    async with self._lock:
                        self._status[name] = "recovering"
                    to_recover.append(name)
            except asyncio.TimeoutError:
                if self._record_health_failure(name, "timeout", max_failures):
                    await self._mark_error(name, "Health check timeout")
            except asyncio.CancelledError:
                raise
            except Exception:
                if status_snapshot.get(name) == "connected":
                    logger.warning("Server %s health check failed", name)
                if self._record_health_failure(name, "exception", max_failures):
                    await self._mark_error(name, "Health check failed")

        # Recovery: reconnect failed servers that HAVE a proxy (outside lock for IO)
        for name in to_recover:
            config = configs_snapshot.get(name, {})
            if not config:
                continue
            async with self._lock:
                if name in self._refreshing:
                    continue  # skip — refresh_server is handling it
                current_config = self._server_configs.get(name)
                old_client = self._clients.get(name)
            if not current_config:
                continue
            new_proxy = await self._connect_server(name, current_config)
            recovered = False
            async with self._lock:
                if name in self._refreshing:
                    # refresh_server took over during our IO — discard
                    logger.debug("Server %s being refreshed concurrently, discarding recovery", name)
                    continue
                if new_proxy is not None:
                    self._proxies[name] = new_proxy
                    self._status[name] = "connected"
                    logger.info("Server %s recovered", name)
                    recovered = True
                else:
                    self._status[name] = "error"
                    await self._notify_change(name, "spawn_failed", {"error": "Recovery failed"})
            if recovered:
                if old_client is not None:
                    await old_client.close()  # 旧接続を破棄（new_proxy に置換済みなので安全）
                self._health_failures.pop(name, None)
                await self._notify_change(name, "recovered", None)

        # Recovery: servers that failed initial connection (status="error", no proxy in _proxies)
        for name, config in configs_snapshot.items():
            if config.get("disabled"):
                continue
            if name in proxies_snapshot:
                continue  # already handled above
            if status_snapshot.get(name) != "error":
                continue
            async with self._lock:
                if name in self._refreshing:
                    continue  # skip — refresh_server is handling it
            # Attempt initial recovery
            logger.info("Attempting recovery for %s (never connected)", name)
            new_proxy = await self._connect_server(name, config)
            recovered = False
            async with self._lock:
                if name in self._refreshing:
                    # refresh_server took over during our IO — discard
                    logger.debug("Server %s being refreshed concurrently, discarding init recovery", name)
                    continue
                if new_proxy is not None:
                    self._proxies[name] = new_proxy
                    self._status[name] = "connected"
                    logger.info("Server %s recovered (initial)", name)
                    recovered = True
                else:
                    # stays "error", will retry next interval
                    await self._notify_change(name, "spawn_failed", {"error": "Recovery failed"})
            if recovered:
                self._health_failures.pop(name, None)
                await self._notify_change(name, "recovered", None)

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

    async def close_all(self) -> None:
        """全 upstream client を閉じる（アプリ shutdown 時）。"""
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            try:
                await client.close()
            except Exception:
                logger.warning("Failed to close upstream client", exc_info=True)

    async def _create_proxy(self, name: str, config: dict) -> tuple[FastMCPProxy, Client]:
        """config から FastMCPProxy を生成。env変数はここで展開する。

        素の fastmcp Client を接続確立（__aenter__）してから FastMCPProxy に
        client_factory 経由で渡す。fastmcp は接続済み Client を渡すと同一
        セッションを再利用するため、list_tools ごとの initialize ハンド
        シェイクを回避できる。client_factory は error 状態なら即時 raise
        する（死んだサーバーがリモート read_timeout でブロックしない）。

        (proxy, client) を返す。client の所有権は呼び出し元が持ち、
        close は呼び出し元（または破棄経路）が行う。
        """
        config = expand_env_vars(config)
        url = config.get("url")
        command = config.get("command")
        if url:
            headers = config.get("headers")
            env = config.get("env")
            if not headers and env:
                derived = bearer_headers_from_env(env)
                if derived:
                    headers = derived
                    logger.info("Derived 'Authorization: Bearer' header for %s from env", name)
                else:
                    logger.warning(
                        "env for URL server '%s' ignored: no unique TOKEN/API_KEY/SECRET "
                        "variable found; use 'headers' for custom authentication", name)
            path = urlparse(url).path
            if re.search(r"/sse(/|\?|&|$)", path):
                transport: Any = SSETransport(url=url, headers=headers)
            else:
                transport = StreamableHttpTransport(url=url, headers=headers)
            client = Client(transport=transport, timeout=self._client_timeout())
        elif command:
            args = config.get("args", [])
            env = config.get("env")
            transport = StdioTransport(command=command, args=args, env=env)  # type: ignore[assignment]
            client = Client(transport=transport)
        else:
            raise ValueError(f"Invalid config for {name}: need 'url' or 'command'")
        # 接続確立（Client は reentrant: __aenter__ で接続、close で切断）
        await client.__aenter__()
        try:
            # fastmcp の _create_client_factory が行う header forwarding の移植
            # （接続済み Client の transport に incoming header 伝播を設定）
            transport = getattr(client, "transport", None)
            if isinstance(transport, (StreamableHttpTransport, SSETransport)):
                transport.forward_incoming_headers = True
            proxy = FastMCPProxy(
                client_factory=self._make_client_factory(name, client),
                name=name,
            )
        except Exception:
            await client.close()  # FastMCPProxy 生成失敗時のリーク防止
            raise
        self._clients[name] = client  # 登録は維持
        return proxy, client

    def _make_client_factory(self, name: str, client: Any) -> Callable[[], Any]:
        """Return a client factory that raises immediately when the server is in error state.

        Called by FastMCPProxy._get_client() on every request (list_tools etc.);
        raising here makes the aggregate provider skip the dead server immediately
        instead of blocking on a remote read timeout.
        """
        def client_factory() -> Any:
            if self._status.get(name) == "error":
                raise RuntimeError(f"Server '{name}' is in error state; skipping request")
            return client
        return client_factory

    async def _rebuild_mounts(self) -> None:
        """全プロキシを再マウント（追加/削除後の整合性確保）。

        NOTE: Callers must hold self._lock when calling this method.
        """
        # NOTE: self.mcp.providers and self.mcp.local_provider are FastMCP
        # internal/private APIs. These may break across FastMCP minor
        # version updates. FastMCP is pinned to <3.5.0 in pyproject.toml.
        self._rebuilding = True
        self._rebuild_complete.clear()
        try:
            self.mcp.providers = [self.mcp.local_provider]

            # 全 proxy を再マウント
            for srv_name, proxy in self._proxies.items():
                self.mcp.mount(proxy, namespace=srv_name)
        finally:
            self._rebuilding = False
            self._rebuild_complete.set()
