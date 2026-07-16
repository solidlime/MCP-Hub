"""
FastMCP の create_proxy + mount を管理。
動的なサーバー追加/削除に対応。
"""

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.client.transports.stdio import StdioTransport
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import FastMCPProxy

from .env_expand import expand_env_vars
from .store import JsonStore

logger = logging.getLogger(__name__)


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

    async def _connect_and_mount(self, name: str, config: dict) -> None:
        """Single-shot connect + mount (no retry). Used by load_all() for
        non-blocking startup.  Failed connections are left as 'error' for the
        health monitor to recover."""
        try:
            proxy = self._create_proxy(name, config)
            async with self._lock:
                self.mcp.mount(proxy, namespace=name)
                self._proxies[name] = proxy
                self._status[name] = "connected"
            logger.info("Server %s connected (background)", name)
        except Exception:
            logger.warning(
                "Server %s failed initial connection — health monitor will retry",
                name, exc_info=True,
            )
            async with self._lock:
                self._status[name] = "error"

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

    async def register_server(self, name: str, config: dict) -> list[str]:
        """サーバー登録 + DB保存 + マウント。

        Returns:
            プロキシ経由で利用可能なツール名のリスト。
        """
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
            async with self._lock:
                self._tool_counts[name] = len(tools)
            result = [t.name for t in tools]
        except Exception:
            logger.warning("Could not list tools for %s", name)
            async with self._lock:
                self._status[name] = "error"
            result = []

        for cb in self._on_change_callbacks:
            await cb()
        return result

    async def unregister_server(self, name: str) -> bool:
        """サーバー削除 + アンマウント。"""
        existed = await self.registry.remove_server(name)
        if not existed:
            return False

        async with self._lock:
            self._proxies.pop(name, None)
            self._server_configs.pop(name, None)
            self._status.pop(name, None)
            self._tool_counts.pop(name, None)
            await self._rebuild_mounts()

        for cb in self._on_change_callbacks:
            await cb()
        return True

    async def refresh_server(self, name: str, config: dict) -> None:
        """プロキシの再生成 + 設定更新。disable 時はアンマウントのみ。"""
        async with self._lock:
            self._refreshing.add(name)
        try:
            async with self._lock:
                self._server_configs[name] = config
                self._status[name] = "disabled" if config.get("disabled") else "connected"

                # 古い proxy をアンマウント
                old_proxy = self._proxies.pop(name, None)
                if old_proxy:
                    await self._rebuild_mounts()

                # disabled なら再生成しない
                if config.get("disabled"):
                    logger.info("Server %s is disabled, not mounting", name)
                else:
                    try:
                        proxy = self._create_proxy(name, config)
                        self._proxies[name] = proxy
                        self.mcp.mount(proxy, namespace=name)
                        self._status[name] = "connected"
                        logger.info("Refreshed server %s", name)
                    except Exception:
                        logger.exception("Failed to refresh server %s", name)
                        self._status[name] = "error"

            # Callbacks outside lock — they may perform IO (rebuild_index calls list_tools)
            for cb in self._on_change_callbacks:
                await cb()
        finally:
            async with self._lock:
                self._refreshing.discard(name)

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
        from .state import request_tags  # no circular dep needed; state is shared

        if tags is None:
            tags = request_tags.get(None)

        logger.debug("list_tools called with tags=%s", tags)

        # Snapshot under lock to prevent dict-mutation-during-iteration races
        async with self._lock:
            proxies_snapshot = dict(self._proxies)
            configs_snapshot = dict(self._server_configs)

        result: dict[str, list[dict]] = {}
        for srv_name, proxy in proxies_snapshot.items():
            # Tag filter (OR logic)
            if tags:
                config = configs_snapshot.get(srv_name, {})
                server_tags = config.get("tags", [])
                if not any(t in server_tags for t in tags):
                    continue

            try:
                tools = await proxy.list_tools()
                self._tool_counts[srv_name] = len(tools)
                result[srv_name] = [{"name": t.name, "description": t.description or ""} for t in tools]
            except Exception:
                logger.warning("Failed to list tools for %s", srv_name)
                result[srv_name] = []

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
        """Register a callback invoked after server add/remove/refresh."""
        self._on_change_callbacks.append(callback)

    def get_proxy(self, name: str) -> FastMCPProxy | None:
        """プロキシインスタンスを取得。"""
        return self._proxies.get(name)

    def get_connected_servers(self) -> dict[str, Any]:
        """Return snapshot of connected proxy instances.

        Returns a dict mapping server_name → proxy. This exposes only
        connected servers (not disabled or errored ones).
        Use this instead of accessing _proxies directly.
        """
        return dict(self._proxies)

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
                tools = await asyncio.wait_for(proxy.list_tools(), timeout=timeout)
                async with self._lock:
                    self._tool_counts[name] = len(tools)
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
            async with self._lock:
                if name in self._refreshing:
                    continue  # skip — refresh_server is handling it
                current_config = self._server_configs.get(name)
            if not current_config:
                continue
            new_proxy = await self._connect_server(name, current_config)
            async with self._lock:
                if name in self._refreshing:
                    # refresh_server took over during our IO — discard
                    logger.debug("Server %s being refreshed concurrently, discarding recovery", name)
                    continue
                if new_proxy is not None:
                    self._proxies[name] = new_proxy
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
            async with self._lock:
                if name in self._refreshing:
                    continue  # skip — refresh_server is handling it
            # Attempt initial recovery
            logger.info("Attempting recovery for %s (never connected)", name)
            new_proxy = await self._connect_server(name, config)
            async with self._lock:
                if name in self._refreshing:
                    # refresh_server took over during our IO — discard
                    logger.debug("Server %s being refreshed concurrently, discarding init recovery", name)
                    continue
                if new_proxy is not None:
                    self._proxies[name] = new_proxy
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

    def _create_proxy(self, name: str, config: dict) -> FastMCPProxy:
        """config から FastMCPProxy を生成。env変数はここで展開する。"""
        config = expand_env_vars(config)
        url = config.get("url")
        command = config.get("command")
        if url:
            proxy = create_proxy(url, name=name)
        elif command:
            args = config.get("args", [])
            env = config.get("env")
            transport = StdioTransport(command=command, args=args, env=env)
            proxy = create_proxy(transport, name=name)
        else:
            raise ValueError(f"Invalid config for {name}: need 'url' or 'command'")
        return proxy

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
