"""
FastMCP の create_proxy + mount を管理。
動的なサーバー追加/削除に対応。
"""

import asyncio
import contextlib
import logging
import os
import re
import time
import traceback
from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.client.transports.base import TransportOptions
from fastmcp.client.transports.http import StreamableHttpTransport
from fastmcp.client.transports.sse import SSETransport
from fastmcp.client.transports.stdio import StdioTransport
from fastmcp.server.providers import proxy as _proxy_providers
from fastmcp.server.providers.proxy import FastMCPProxy

from .env_expand import expand_env_vars
from .schema_diff import (
    SCHEMA_CHANGED_EVENT,
    SchemaDiffEntry,
    diff_items,
    item_fingerprints,
    tool_fingerprints,
)
from .store import JsonStore
from .state import app_state
from .validators import ValidationError, bearer_headers_from_env, validate_limits

logger = logging.getLogger(__name__)

# === Resilient roots forwarding ===
# When upstream servers request roots during initialization (before a client
# connects to MCP-Hub), the default handler calls ctx.list_roots() which fails
# with "session is not available".  Replace with a resilient version that
# returns empty roots instead of raising RuntimeError.
# 4.x note: default_proxy_roots_handler survives as the canonical proxy
# forwarding path (fastmcp.server.providers.proxy); the frontend path is
# Client(roots=...). Hub itself makes no direct ctx.list_roots() call.
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

# ツール結果キャッシュ（tool_cache）を無効化するライフサイクルイベント
_CACHE_INVALIDATING_EVENTS = frozenset(
    {"connected", "recovered", "updated", "removed", "renamed"}
)


class StaleConnectError(RuntimeError):
    """接続中に世代が変わった（削除/再登録/改名）ことを示す。_create_proxy が送出する。"""


class RateLimitExceeded(RuntimeError):
    """サーバー単位レート制限に引っかかった（待機が rate_window を超える）。"""


class CallTimeout(RuntimeError):
    """サーバー単位の call_timeout を超えた。"""


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
        self._generations: dict[str, int] = {}
        self._tool_counts: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._refreshing: set[str] = set()  # Protected by self._lock
        self._health_task: asyncio.Task | None = None
        self._on_change_callbacks: list[Callable] = []
        self._rebuild_complete = asyncio.Event()
        self._rebuild_complete.set()  # initially not rebuilding
        # Concurrency cap for tool calls (prevents DoS via unlimited process/connection spawn)
        _max_calls = int(os.environ.get("MCP_HUB_MAX_CONCURRENT_CALLS", "50"))
        if _max_calls < 1:
            # 0/負は Semaphore を枯渇させるため 1 に正規化
            logger.warning("MCP_HUB_MAX_CONCURRENT_CALLS=%d は不正のため 1 に正規化", _max_calls)
            _max_calls = 1
        self._call_semaphore = asyncio.Semaphore(_max_calls)
        self._tool_cache: dict[str, tuple[float, list[Any]]] = {}  # server_name -> (timestamp, tools)
        # server_name -> {kind: {name: fingerprint}}（差分検知のベースライン）
        self._schema_snapshots: dict[str, dict[str, dict[str, str]]] = {}
        self._health_failures: dict[str, int] = {}  # server_name -> consecutive health-check failures
        self._last_recovery_fail: dict[str, float] = {}  # server_name -> last recovery attempt time
        # サーバー単位の同時実行 / レート制限状態（limits 設定時のみ使う）
        self._server_sems: dict[str, tuple[int, asyncio.Semaphore]] = {}
        self._rate_state: dict[str, tuple[float, float]] = {}  # name -> (tokens, last_ts)
        self._rate_lock = asyncio.Lock()
        # 接続状態（API/UI 用）: 連続失敗数・最終成功時刻・直近エラー・カタログ件数
        self._consecutive_failures: dict[str, int] = {}
        self._last_success: dict[str, float] = {}
        self._last_error: dict[str, dict] = {}
        self._resource_counts: dict[str, int] = {}
        self._prompt_counts: dict[str, int] = {}
        # バックグラウンド接続タスク（ホットリロード/終了処理が待機できるよう保持）
        self._bg_tasks: set[asyncio.Task] = set()
        # グレースフルシャットダウン中フラグ（True の間は新規ツール呼び出しを拒否）
        self._draining = False

    @staticmethod
    def _retry_env() -> tuple[int, float]:
        """(max_retries, base_delay_seconds) from env."""
        return (
            int(os.environ.get("MCP_HUB_RETRY_MAX", "3")),
            float(os.environ.get("MCP_HUB_RETRY_DELAY", "1.0")),
        )

    @staticmethod
    def _recovery_cooldown() -> float:
        """Minimum seconds between recovery attempts for a failed server."""
        return float(os.environ.get("MCP_HUB_RECOVERY_COOLDOWN", "300.0"))

    def _client_timeout(self) -> float | None:
        """Read timeout for upstream requests (seconds).
        Priority: DB setting → MCP_HUB_CLIENT_TIMEOUT env → 180.0."""
        data = getattr(self.registry, "_data", None)
        db = data.get("client_timeout") if isinstance(data, dict) else None
        if db is not None:
            return float(db)
        raw = os.environ.get("MCP_HUB_CLIENT_TIMEOUT")
        return float(raw) if raw else 180.0

    def _connect_timeout(self) -> float:
        """Initial connectivity check timeout (seconds).
        Priority: DB setting → MCP_HUB_CONNECT_TIMEOUT env → 30.0."""
        data = getattr(self.registry, "_data", None)
        db = data.get("connect_timeout") if isinstance(data, dict) else None
        if db is not None:
            return float(db)
        return float(os.environ.get("MCP_HUB_CONNECT_TIMEOUT", "30.0"))

    # --- per-server 呼び出し制限（timeout / 同時実行 / レート） ---

    def server_limits(self, name: str) -> dict:
        """サーバー設定 config["limits"] を正規化して返す。未知/壊れた値は無視。"""
        raw = self._server_configs.get(name, {}).get("limits")
        if not isinstance(raw, dict):
            raw = {}
        try:
            validated = validate_limits(raw)
        except ValidationError as e:
            logger.warning("Server %s の limits が不正のため既定値使用: %s", name, e)
            validated = {}
        return {
            "call_timeout": validated.get("call_timeout"),
            "max_concurrent": validated.get("max_concurrent"),
            "rate_limit": validated.get("rate_limit"),
            "rate_burst": validated.get("rate_burst") or validated.get("rate_limit"),
            "rate_window": validated.get("rate_window") or 1.0,
        }

    def known_servers(self) -> dict[str, Any]:
        """limits 解決用のサーバー名集合（設定済み + 接続済み）。

        制限は設定値なので未接続サーバーにも効く必要がある。接続済みを後に
        重ねるのは resolve_server が最長一致で名前だけ見るため実害はない。
        """
        return {**self._server_configs, **self._proxies}

    def server_call_timeout(self, name: str) -> float | None:
        """呼び出しタイムアウト（秒）。サーバー上書き → MCP_HUB_CALL_TOOL_TIMEOUT → 30。

        0 以下は無効（無制限）として None を返す。
        """
        override = self.server_limits(name)["call_timeout"]
        if override is not None:
            return override
        try:
            env = float(os.environ.get("MCP_HUB_CALL_TOOL_TIMEOUT", "30"))
        except ValueError:
            env = 30.0
        return env if env > 0 else None

    @contextlib.asynccontextmanager
    async def call_slot(self, server_name: str, tool_name: str = "-"):
        """per-server のレート制限と同時実行スロットを取得する。

        レート制限はトークンバケット。待機が rate_window を超える場合は
        即座に RateLimitExceeded（無限定な待機でクライアントを固めない）。
        """
        limits = self.server_limits(server_name)
        if limits["rate_limit"]:
            await self._acquire_rate(server_name, limits)
        max_concurrent = limits["max_concurrent"]
        if not max_concurrent:
            yield limits
            return
        sem = self._server_semaphore(server_name, max_concurrent)
        async with sem:
            yield limits

    def _server_semaphore(self, name: str, max_concurrent: int) -> asyncio.Semaphore:
        """max_concurrent ごとに生成・キャッシュ。値が変われば作り直す。"""
        entry = self._server_sems.get(name)
        if entry is not None and entry[0] == max_concurrent:
            return entry[1]
        # shortcut: 上限変更時に旧 Semaphore の待機者は旧上限のまま解放される。
        # 1 回の上限変更で数呼び出し分の遅延しか出ないため許容。
        sem = asyncio.Semaphore(max_concurrent)
        self._server_sems[name] = (max_concurrent, sem)
        return sem

    async def _acquire_rate(self, name: str, limits: dict) -> None:
        """トークンバケットで 1 トークン消費する（待機はロック内＝保守的直列化）。"""
        rate = limits["rate_limit"] / limits["rate_window"]  # tokens/sec
        capacity = float(max(limits["rate_burst"] or 1, 1))
        async with self._rate_lock:
            now = time.monotonic()
            tokens, ts = self._rate_state.get(name, (capacity, now))
            tokens = min(capacity, tokens + max(0.0, now - ts) * rate)
            if tokens < 1.0:
                wait = (1.0 - tokens) / rate
                # 待機が窓のほぼ全体（99%）になる＝この窓内では払えない。無限定な
                # 待機でクライアントを固めず即座に拒否する。境界を内側に取るのは、
                # 直前の呼び出しから数µs 分のトークンが既に補充されていて厳密な
                # 等号比較が成立しないため。
                if wait >= limits["rate_window"] * 0.99:
                    raise RateLimitExceeded(
                        f"Rate limit exceeded for server {name!r}: "
                        f"{limits['rate_limit']:g} calls / {limits['rate_window']:g}s "
                        f"(burst {capacity:g}) — retry later"
                    )
                wait = (1.0 - tokens) / rate
                await asyncio.sleep(wait)
                now = time.monotonic()
                tokens = min(capacity, tokens + wait * rate)
            self._rate_state[name] = (tokens - 1.0, now)

    def reset_limits_state(self, name: str) -> None:
        """設定変更時に Semaphore / レートの状態を破棄する。"""
        self._server_sems.pop(name, None)
        self._rate_state.pop(name, None)

    # --- 接続状態の記録（ホットリロード/ヘルスチェック共通） ---

    def _reset_connection_state(self, name: str) -> None:
        """再接続時に一時的な失敗状態を捨てる（設定変更で直る可能性があるため）。"""
        self._consecutive_failures.pop(name, None)
        self._last_error.pop(name, None)
        self._health_failures.pop(name, None)
        self.reset_limits_state(name)

    def _record_success(self, name: str) -> None:
        """接続/呼び出し成功: 連続失敗をリセットし最終成功時刻を更新する。"""
        self._consecutive_failures.pop(name, None)
        self._health_failures.pop(name, None)
        self._last_recovery_fail.pop(name, None)
        self._last_success[name] = time.time()

    def _record_failure(self, name: str, reason: str, message: str) -> None:
        """接続/呼び出し失敗: 連続失敗数を進め、理由を残す（履歴は有界）。"""
        self._consecutive_failures[name] = self._consecutive_failures.get(name, 0) + 1
        short = str(message)[:200]
        self._last_error[name] = {"reason": reason, "message": short, "at": time.time()}
        app_state.record_health(name, "failure", f"{reason}: {short}")

    def record_catalog_counts(self, name: str, resources: int | None = None,
                              prompts: int | None = None) -> None:
        """resources/prompts の件数を記録（取得できた種別のみ更新）。"""
        if resources is not None:
            self._resource_counts[name] = resources
        if prompts is not None:
            self._prompt_counts[name] = prompts

    _EFFECTIVE_STATES = {
        "connected": "healthy",
        "connecting": "initializing",
        "recovering": "recovering",
        "error": "error",
    }

    def get_connection_state(self, name: str) -> dict:
        """サーバー 1 台の接続状態を一貫した形で返す（admin API / WebUI 共通）。

        effective_state は disabled > 生 status の優先順で決める。
        retry_* は実際に適用される復旧クールダウンから算出する（error のみ）。
        """
        config = self._server_configs.get(name, {})
        status = self._status.get(name, "unknown")
        disabled = bool(config.get("disabled", False))
        if status == "error":  # 直近の失敗は disabled より優先して見せる
            effective = "error"
        elif disabled:
            effective = "disabled"
        else:
            effective = self._EFFECTIVE_STATES.get(status, "unknown")
        cooldown = self._recovery_cooldown()
        retry_in: float | None = None
        if status == "error":
            last_fail = self._last_recovery_fail.get(name)
            elapsed = time.monotonic() - last_fail if last_fail is not None else cooldown
            retry_in = max(0.0, cooldown - elapsed)
        return {
            "name": name,
            "status": status,
            "effective_state": effective,
            "disabled": disabled,
            "consecutive_failures": self._consecutive_failures.get(name, 0),
            "last_success_at": self._last_success.get(name),
            "last_error": self._last_error.get(name),
            "retry_backoff_seconds": cooldown,
            "retry_in_seconds": round(retry_in, 1) if retry_in is not None else None,
            "next_retry_at": time.time() + retry_in if retry_in is not None else None,
            "tool_count": self._tool_counts.get(name, 0),
            "resource_count": self._resource_counts.get(name, 0),
            "prompt_count": self._prompt_counts.get(name, 0),
            "health_history": app_state.health_history(name),
        }

    def _spawn_bg(self, coro: Any) -> asyncio.Task:
        """バックグラウンド接続タスクを起動して保持する（完了時に自動で外れる）。"""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    async def call_tool_limited(self, server_name: str, tool_name: str,
                                arguments: dict) -> Any:
        """per-server 制限を適用した call_tool（admin REST などの直接呼び出し用）。"""
        timeout = self.server_call_timeout(server_name)
        async with self.call_slot(server_name, tool_name):
            call = self.call_tool(server_name, tool_name, arguments)
            if timeout is None:
                return await call
            try:
                return await asyncio.wait_for(call, timeout=timeout)
            except asyncio.TimeoutError:
                raise CallTimeout(
                    f"Call to server {server_name!r} tool {tool_name!r} timed out "
                    f"after {timeout:g}s (limits.call_timeout)"
                ) from None

    async def _connect_server(
        self, name: str, config: dict, expected_gen: int | None = None
    ) -> "FastMCPProxy | None":
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
                    proxy, client = await self._create_proxy(
                        name, config, expected_gen=expected_gen
                    )
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
                except StaleConnectError:
                    # 世代不一致 — リトライせず即伝播（呼び出し元が破棄判断する）
                    raise
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
        async with self._lock:
            gen = self._generations.get(name)
        try:
            proxy, client = await self._create_proxy(name, config, expected_gen=gen)
            # Verify the proxy actually works before registering.
            # A broken server (e.g. URL endpoint returning 405) fails here
            # and is left for the health monitor to recover at its own pace.
            import time
            tools = list(await asyncio.wait_for(proxy.list_tools(), timeout=self._connect_timeout()))
            if tools:
                self._tool_cache[name] = (time.monotonic(), tools)
            self._tool_counts[name] = len(tools)
            zombie = False
            leaked = None
            async with self._lock:
                if name not in self._server_configs or self._generations.get(name) != gen:
                    # リネーム/削除済み — ゾンビ復活を防ぐ。
                    # 自分が登録した client のみ回収する（他タスクの client を盗まない）
                    zombie = True
                    self._tool_cache.pop(name, None)
                    self._tool_counts.pop(name, None)
                    if self._clients.get(name) is client:
                        leaked = self._clients.pop(name)
                else:
                    self.mcp.mount(proxy, namespace=name)
                    self._proxies[name] = proxy
                    self._status[name] = "connected"
            if leaked is not None:
                await leaked.close()  # ロック外で close（デッドロック回避）
            if zombie:
                return
            self._record_success(name)
            app_state.record_health(name, "connected", f"{len(tools)} tools")
            logger.info("Server %s connected (background)", name)
            # ベースライン確保 / 再接続時の差分検知（変化がなければ無通知）
            await self._check_schema_diff(name, proxy, "connect", tools)
            # Notify listeners so meta index can rebuild
            await self._notify_change(name, "connected", {"tool_count": len(tools)})
        except StaleConnectError:
            # 世代不一致 — client は _create_proxy 内で close・未登録済み。静かに破棄
            return
        except asyncio.TimeoutError:
            logger.warning("Server %s connection timed out — health monitor will retry", name)
            leaked = None
            if client is not None:
                async with self._lock:
                    if self._clients.get(name) is client:
                        leaked = self._clients.pop(name, None)
            if leaked is not None:
                await leaked.close()  # 自分が登録した client のみ close（ロック外）
            # 世代が変わっていたらステータス上書きは行わない — 新しいライフサイクルが所有している
            if (leaked is not None or client is None) and name in self._server_configs and self._generations.get(name) == gen:
                async with self._lock:
                    self._status[name] = "error"
                self._record_failure(name, "connect_timeout", "Connection timed out")
            # 世代が変わっていれば幽霊の spawn_failed 通知は出さない
            if name in self._server_configs and self._generations.get(name) == gen:
                await self._notify_change(name, "spawn_failed", {"error": "Connection timed out"})
        except Exception:
            logger.warning(
                "Server %s failed initial connection — health monitor will retry",
                name, exc_info=True,
            )
            leaked = None
            if client is not None:
                async with self._lock:
                    if self._clients.get(name) is client:
                        leaked = self._clients.pop(name, None)
            if leaked is not None:
                await leaked.close()  # 自分が登録した client のみ close（ロック外）
            # 世代が変わっていたらステータス上書きは行わない — 新しいライフサイクルが所有している
            if (leaked is not None or client is None) and name in self._server_configs and self._generations.get(name) == gen:
                async with self._lock:
                    self._status[name] = "error"
                self._record_failure(name, "connect_failed", "Connection failed")
            # 世代が変わっていれば幽霊の spawn_failed 通知は出さない
            if name in self._server_configs and self._generations.get(name) == gen:
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
                # 起動時の接続を最初の refresh/unregister で無効化できるよう世代を進める
                self._bump_generation(name)
                self._reset_connection_state(name)
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
            self._bump_generation(name)
            self._reset_connection_state(name)

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
            self._bump_generation(name)
            self._tool_counts.pop(name, None)
            self._tool_cache.pop(name, None)
            self._schema_snapshots.pop(name, None)
            self._consecutive_failures.pop(name, None)
            self._last_success.pop(name, None)
            self._last_error.pop(name, None)
            self._resource_counts.pop(name, None)
            self._prompt_counts.pop(name, None)
            app_state.clear_health_history(name)
            await self._rebuild_mounts()

        if client is not None:
            await client.close()  # ゾンビ接続防止

        await self._notify_change(name, "removed", None)
        return True

    async def apply_server_configs(self, desired: dict[str, dict]) -> dict[str, list[str]]:
        """設定ファイル由来のサーバー集合にランタイムを合わせる（ホットリロード）。

        store へは書き戻さない（呼び出し元が読んだファイルが真実源のため）。
        追加は register_server と同じくバックグラウンド接続、削除はアンマウント +
        upstream client 切断、設定変更は refresh_server（プロキシ再生成）。

        Returns:
            {"added": [...], "removed": [...], "updated": [...], "unchanged": [...]}
        """
        added: list[str] = []
        updated: list[str] = []
        unchanged: list[str] = []

        for name, config in desired.items():
            current = self._server_configs.get(name)
            if current is None:
                async with self._lock:
                    self._server_configs[name] = config
                    self._bump_generation(name)
                    self._status[name] = (
                        "disabled" if config.get("disabled") else "connecting"
                    )
                if not config.get("disabled"):
                    self._spawn_bg(self._connect_and_mount(name, config))
                added.append(name)
            elif current != config:
                updated.append(name)
            else:
                unchanged.append(name)

        removed: list[str] = []
        closing: list[Client] = []
        for name in list(self._server_configs):
            if name in desired:
                continue
            async with self._lock:
                proxy = self._proxies.pop(name, None)
                client = self._clients.pop(name, None)
                self._server_configs.pop(name, None)
                self._status.pop(name, None)
                self._tool_counts.pop(name, None)
                self._tool_cache.pop(name, None)
                self._bump_generation(name)
                if proxy is not None:
                    await self._rebuild_mounts()
            if client is not None:
                closing.append(client)
            removed.append(name)

        for client in closing:
            await client.close()  # ロック外で close（ゾンビ接続防止）

        for name in updated:
            await self.refresh_server(name, desired[name])
        for name in removed:
            await self._notify_change(name, "removed", None)

        return {
            "added": sorted(added),
            "removed": sorted(removed),
            "updated": sorted(updated),
            "unchanged": sorted(unchanged),
        }

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
            self._reset_connection_state(name)
            self._bump_generation(name)  # 進行中の接続を無効化（この refresh の世代はその次）
            gen = self._generations.get(name)
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
                    proxy, client = await self._create_proxy(name, config, expected_gen=gen)
                    zombie = False
                    leaked = None
                    async with self._lock:
                        if name not in self._server_configs or self._generations.get(name) != gen:
                            # リネーム/削除済み — ゾンビ復活を防ぐ。
                            # 自分が登録した client のみ回収する（他タスクの client を盗まない）
                            zombie = True
                            if self._clients.get(name) is client:
                                leaked = self._clients.pop(name)
                        else:
                            self._proxies[name] = proxy
                            self.mcp.mount(proxy, namespace=name)
                            self._status[name] = "connected"
                    if leaked is not None:
                        await leaked.close()  # ロック外で close（デッドロック回避）
                    if zombie:
                        return
                    self._record_success(name)
                    logger.info("Refreshed server %s", name)
                    await self._check_schema_diff(name, proxy, "refresh")
                    refreshed = True
                except StaleConnectError:
                    logger.debug("Refresh for %s discarded (generation changed)", name)
                except Exception:
                    logger.exception("Failed to refresh server %s", name)
                    if name in self._server_configs and self._generations.get(name) == gen:
                        async with self._lock:
                            self._status[name] = "error"
                        self._record_failure(name, "refresh", "Refresh failed")

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

        tags_updated は検索インデックス再構築（tags が doc に焼込まれているため）
        をトリガーする専用の変更通知。プロキシ再生成は伴わない。
        """
        async with self._lock:
            self._server_configs[name] = config
        self.reset_limits_state(name)
        await self._notify_change(name, "tags_updated", None)

    async def update_limits(self, name: str, config: dict) -> None:
        """limits 変更を反映する（プロキシ再生成なし・制限状態を破棄）。

        制限は tools/call 毎に config から読むため、反映は config 差し替えと
        旧設定由来の Semaphore / レート状態の破棄だけで足りる。
        """
        async with self._lock:
            self._server_configs[name] = config
        self.reset_limits_state(name)

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
            self._bump_generation(old_name)
            self._bump_generation(new_name)
            status = self._status.pop(old_name, "unknown")
            tc = self._tool_counts.pop(old_name, None)
            cache = self._tool_cache.pop(old_name, None)
            self._server_configs.pop(old_name, None)
            if proxy is not None:
                self._proxies[new_name] = proxy  # 接続維持
                await self._rebuild_mounts()
            stale_client = self._clients.pop(new_name, None)  # 進行中 connect の残骸なら回収
            if client is not None:
                self._clients[new_name] = client  # 接続維持
            self._server_configs[new_name] = config
            self._status[new_name] = status
            if tc is not None:
                self._tool_counts[new_name] = tc
            if cache is not None:
                self._tool_cache[new_name] = cache  # キャッシュ移動で再列挙回避
            snapshot = self._schema_snapshots.pop(old_name, None)
            if snapshot:
                self._schema_snapshots[new_name] = snapshot
        if stale_client is not None and stale_client is not client:
            try:
                await stale_client.close()
            except Exception:
                logger.debug("Renamed-over stale client close raised", exc_info=True)
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
            info = {
                "name": name,
                "tags": config.get("tags", []),
                "description": config.get("description", ""),
            }
            info.update(self.get_connection_state(name))
            servers_info.append(info)
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
                    if self._status.get(name) != "recovering":
                        await self._mark_error(name, f"list_tools timeout after {timeout}s")
                return name, []
            except Exception:
                logger.warning("Failed to list tools for %s", name)
                if self._record_health_failure(name, "exception", max_failures):
                    if self._status.get(name) != "recovering":
                        await self._mark_error(name, "list_tools failed")
                return name, []

        # 並列実行（gather は入力順を保持。name もタスクに紐づけて二重に保証）
        if pending:
            for name, tools in await asyncio.gather(
                *(_fetch_one(n, p) for n, p in pending)
            ):
                result[name] = tools

        return result

    def begin_drain(self) -> None:
        """新規ツール呼び出しを拒否するモードに入る（既存呼び出しは継続）。"""
        self._draining = True

    async def call_tool(self, server_name: str, tool_name: str,
                        arguments: dict) -> Any:
        """ツール実行。asyncio.Event で rebuild 完了を待ち、Semaphore で同時実行数を制限。"""
        if self._draining:
            raise RuntimeError(
                f"Server is shutting down — tool call {server_name!r}.{tool_name!r} rejected"
            )
        # Wait for any ongoing rebuild to complete (with timeout)
        timeout = self.server_call_timeout(server_name) or 30.0
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
          | "removed" | "updated" | "tags_updated"
        """
        self._on_change_callbacks.append(callback)

    async def _notify_change(self, name: str, event: str, detail: dict | None = None) -> None:
        """Fire all on_change callbacks with the new signature, protected."""
        if event in _CACHE_INVALIDATING_EVENTS:
            # 再接続/スキーマ・設定変更/削除で結果キャッシュを無効化する（古い結果を返さない）
            from .state import app_state
            app_state.tool_cache.invalidate_server(name)
            old_name = (detail or {}).get("old_name")
            if isinstance(old_name, str) and old_name:
                app_state.tool_cache.invalidate_server(old_name)
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

    async def test_connect(self, name: str) -> tuple["FastMCPProxy | None", str]:
        """Test endpoint helper: return a usable proxy, fresh-connecting once
        if needed so the WebUI test button also works as a reconnect trigger.

        Returns (proxy, reason) where reason is one of:
          "connected"       — existing proxy (no connect attempted) or fresh
                              connect succeeded
          "not_found"       — server not registered
          "disabled"        — server is disabled
          "in_progress"     — a background connect/recovery is already running
          "failed:<status>" — fresh connect attempted but proxy is still absent
        """
        proxy = self._proxies.get(name)
        if proxy is not None:
            return proxy, "connected"
        config = self._server_configs.get(name)
        if config is None:
            return None, "not_found"
        if config.get("disabled"):
            return None, "disabled"
        status = self._status.get(name, "unknown")
        if status in ("connecting", "recovering"):
            return None, "in_progress"
        # 未接続 — fresh connect を1回試みる。_connect_and_mount は single-shot
        # （リトライなし・例外内包・世代管理込み）なので endpoint から await 可能。
        await self._connect_and_mount(name, config)
        proxy = self._proxies.get(name)
        if proxy is None:
            return None, f"failed:{self._status.get(name, 'unknown')}"
        return proxy, "connected"

    def proxy_to_name(self, proxy_id: int) -> str | None:
        """id(proxy) → server_name の逆引き。TagFilterMiddleware 用。"""
        for name, proxy in self._proxies.items():
            if id(proxy) == proxy_id:
                return name
        return None

    def server_tags(self, name: str) -> list[str]:
        """サーバーの設定タグ一覧。TagFilterMiddleware 用。"""
        return self._server_configs.get(name, {}).get("tags", [])

    def server_description(self, name: str) -> str:
        """サーバーの一行説明。無ければ空文字。meta カタログ用。"""
        return self._server_configs.get(name, {}).get("description", "")

    def server_tool_search_desc(self, name: str) -> dict[str, str]:
        """ツール単位の日本語1文（config.tool_search_desc）。無ければ空 dict。

        meta index が索引テキストに前置する。英語 docstring の語彙の壁を越えるための
        検索ヒントで、表示用ではない。
        """
        value = self._server_configs.get(name, {}).get("tool_search_desc", {})
        return value if isinstance(value, dict) else {}

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
        assert last_error is not None  # 例外時のみループを抜ける
        raise last_error

    async def list_tools_for_server(self, name: str, proxy: FastMCPProxy, cache_ttl: float = 60.0) -> list[Any]:
        """List tools with caching. Returns cached result if within TTL."""
        now = time.monotonic()
        if name in self._tool_cache:
            ts, tools = self._tool_cache[name]
            if now - ts < cache_ttl:
                return tools
        try:
            tools = await self._list_tools_with_retry(proxy, name)
        except Exception as e:
            self._record_failure(name, "list_tools", str(e) or e.__class__.__name__)
            raise
        self._record_success(name)
        app_state.record_health(name, "ok", f"{len(tools)} tools")
        if tools:
            # 空リストでは上書きしない（probe が空を返しても最後の既知ツールを保持）
            self._tool_cache[name] = (now, tools)
            self._tool_counts[name] = len(tools)
            # 定期 refresh 経由の差分検知（ベースラインがある時だけ）
            if name in self._schema_snapshots:
                changes = self._record_schema_diff(name, {"tools": tool_fingerprints(tools)}, "periodic")
                if changes:
                    await self._notify_change(name, SCHEMA_CHANGED_EVENT, {"trigger": "periodic", "changes": changes})
        return tools

    async def _schema_snapshot(self, proxy: Any, tools: list[Any] | None = None) -> dict[str, dict[str, str]]:
        """ツール/リソース/プロンプトの指紋。未対応の種別は省く。"""
        snapshot: dict[str, dict[str, str]] = {"tools": tool_fingerprints(tools or [])}
        for kind, method, key_attr in (
            ("resources", "list_resources", "uri"),
            ("prompts", "list_prompts", "name"),
        ):
            fn = getattr(proxy, method, None)
            if not callable(fn):
                continue
            try:
                snapshot[kind] = item_fingerprints(await cast(Any, fn()), key_attr)
            except Exception:
                logger.debug("%s not available for snapshot", method, exc_info=True)
        return snapshot

    def _record_schema_diff(self, name: str, partial: dict[str, dict[str, str]], trigger: str) -> dict | None:
        """指紋を更新し、変化があれば履歴に記録して差分を返す（無変化は None）。

        初回はベースラインとして黙って覚える（新規接続を「追加」と騒がない）。
        空の応答は取得失敗か未対応とみなし、「全削除」として記録しない。
        """
        baseline = name not in self._schema_snapshots
        old = self._schema_snapshots.get(name, {})
        merged = dict(old)
        changes: dict[str, dict[str, list[str]]] = {}
        for kind, items in partial.items():
            if not baseline and not items:
                continue
            if not baseline:
                diff = diff_items(old.get(kind, {}), items)
                if diff["added"] or diff["removed"] or diff["changed"]:
                    changes[kind] = diff
            merged[kind] = items
        self._schema_snapshots[name] = merged
        if not changes:
            return None
        entry = SchemaDiffEntry(ts=time.time(), server=name, trigger=trigger, changes=changes)
        app_state.append_diff(entry)
        logger.info("Schema changed for %s (%s): %s", name, trigger, entry.to_dict()["summary"])
        return changes

    async def _check_schema_diff(self, name: str, proxy: Any, trigger: str, tools: list[Any] | None = None) -> dict | None:
        """スナップショット比較 → 記録 → schema_changed 通知。空応答は「全削除」とみなさない。"""
        try:
            if tools is None:
                tools = await self._list_tools_with_retry(proxy, name)
            changes = self._record_schema_diff(name, await self._schema_snapshot(proxy, tools), trigger)
        except Exception:
            logger.warning("Schema snapshot for %s failed", name, exc_info=True)
            return None
        if changes:
            await self._notify_change(name, SCHEMA_CHANGED_EVENT, {"trigger": trigger, "changes": changes})
        return changes

    def _record_health_failure(self, name: str, reason: str, max_failures: int) -> bool:
        """Count a health-check failure; return True when the server should be marked error.

        Transient failures (slow external APIs like Mapbox) must not flip a
        server to error on the first timeout. After max_failures consecutive
        failures the server is marked error, and the counter resets so it takes
        max_failures failures again before re-marking (prevents event spam).
        """
        failures = self._health_failures.get(name, 0) + 1
        self._health_failures[name] = failures
        app_state.metrics.record_health_failure(name, reason)
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
        """Mark a server as error and drop its broken proxy/client.

        Dropping the proxy from _proxies (and thus from the FastMCP mount list via
        _rebuild_mounts) routes the server into the health monitor's initial
        recovery path (status="error" with no proxy → _connect_server with a fresh
        client). A stale client with leaked nesting_counter can never recover in
        place, so it must be closed and recreated.
        """
        async with self._lock:
            self._status[name] = "error"
            old_proxy = self._proxies.pop(name, None)
            old_client = self._clients.pop(name, None)
        if old_client is not None:
            try:
                await old_client.close()
            except Exception:
                logger.warning("Failed to close upstream client for %s", name, exc_info=True)
        await self._notify_change(name, "disconnected", {"error": error_msg})
        if old_proxy is not None:
            async with self._lock:
                await self._rebuild_mounts()

    async def _health_check(self) -> None:
        """Check all connected servers, recover failed ones."""
        # Snapshots under lock (prevents dict mutation during iteration)
        async with self._lock:
            proxies_snapshot = dict(self._proxies)
            configs_snapshot = dict(self._server_configs)
            status_snapshot = dict(self._status)
            # probe 成功経路は _record_success で _last_recovery_fail を消すため、
            # クールダウン判定には probe 前の値を退避して使う（probe 成功≠復旧）。
            recovery_fail_snapshot = dict(self._last_recovery_fail)

        # Must exceed the 202-polling budget in streamable_http_patch.py
        # (MAX_POLL_ATTEMPTS * POLL_DELAY_SECONDS = 20s), or 202-style servers
        # (EDINET etc.) time out on every health check and get disconnected.
        timeout = int(os.environ.get("MCP_HUB_HEALTH_TIMEOUT", "25"))
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
                gen = self._generations.get(name)
            if not current_config:
                continue
            last_fail = recovery_fail_snapshot.get(name)
            if last_fail is not None and (time.monotonic() - last_fail) < self._recovery_cooldown():
                logger.debug("Recovery for %s skipped (cooldown)", name)
                async with self._lock:
                    self._status[name] = "error"  # 次の probe で再評価できるように戻す
                continue
            try:
                new_proxy = await asyncio.wait_for(
                    self._connect_server(name, current_config, expected_gen=gen),
                    timeout=self._connect_timeout(),
                )
            except StaleConnectError:
                logger.debug("Recovery connect for %s aborted (generation changed)", name)
                continue  # 次のサーバーへ（ステータス・通知とも触らない）
            except asyncio.TimeoutError:
                logger.warning(
                    "Recovery connect for %s timed out after %.1fs",
                    name, self._connect_timeout(),
                )
                new_proxy = None
            if new_proxy is None:
                self._last_recovery_fail[name] = time.monotonic()
            recovered = False
            async with self._lock:
                if name in self._refreshing:
                    # refresh_server took over during our IO — discard
                    logger.debug("Server %s being refreshed concurrently, discarding recovery", name)
                    # _connect_server が先行 mount した死体プロキシを providers から除去
                    await self._rebuild_mounts()
                    continue
                if self._generations.get(name) != gen:
                    logger.debug("Server %s recovery discarded (generation changed)", name)
                    # _connect_server が先行 mount した死体プロキシを providers から除去
                    await self._rebuild_mounts()
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
                self._last_recovery_fail.pop(name, None)
                self._health_failures.pop(name, None)
                app_state.metrics.record_reconnect()
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
                gen = self._generations.get(name)
            last_fail = self._last_recovery_fail.get(name)
            if last_fail is not None and (time.monotonic() - last_fail) < self._recovery_cooldown():
                logger.debug("Recovery for %s skipped (cooldown)", name)
                continue
            # Attempt initial recovery
            logger.info("Attempting recovery for %s (never connected)", name)
            try:
                new_proxy = await asyncio.wait_for(
                    self._connect_server(name, config, expected_gen=gen),
                    timeout=self._connect_timeout(),
                )
            except StaleConnectError:
                logger.debug("Recovery connect for %s aborted (generation changed)", name)
                continue  # 次のサーバーへ（ステータス・通知とも触らない）
            except asyncio.TimeoutError:
                logger.warning(
                    "Recovery connect for %s timed out after %.1fs",
                    name, self._connect_timeout(),
                )
                new_proxy = None
            if new_proxy is None:
                self._last_recovery_fail[name] = time.monotonic()
            recovered = False
            async with self._lock:
                if name in self._refreshing:
                    # refresh_server took over during our IO — discard
                    logger.debug("Server %s being refreshed concurrently, discarding init recovery", name)
                    # _connect_server が先行 mount した死体プロキシを providers から除去
                    await self._rebuild_mounts()
                    continue
                if self._generations.get(name) != gen:
                    logger.debug("Server %s recovery discarded (generation changed)", name)
                    # _connect_server が先行 mount した死体プロキシを providers から除去
                    await self._rebuild_mounts()
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
                self._last_recovery_fail.pop(name, None)
                self._health_failures.pop(name, None)
                app_state.metrics.record_reconnect()
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

    async def cancel_background_tasks(self) -> None:
        """バックグラウンドの接続/再接続タスクを止める（新規受付も停止）。

        進行中の connect を放置すると、子プロセスが親の終了後も残る。
        非同期タスクは cancel して待つ（例外は無視して残りを続行）。
        """
        self._draining = True
        tasks = list(self._bg_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._bg_tasks.clear()
        if tasks:
            logger.info("Cancelled %d background connection task(s)", len(tasks))

    async def close_all(self) -> None:
        """新規受付を止め、バックグラウンドタスクと upstream client を閉じる。

        アプリ shutdown 時に呼ぶ。個々の失敗は警告に留めて残りを続行する
        （1 台の失敗で他サーバーの接続が残る＝ゾンビ化を避ける）。
        """
        await self.cancel_background_tasks()
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            try:
                await client.close()
            except Exception:
                logger.warning("Failed to close upstream client", exc_info=True)
        logger.info("Closed %d upstream client(s)", len(clients))

    def _bump_generation(self, name: str) -> None:
        """世代を進める（呼び出し側は self._lock を保持していること）。

        _generations は pop しない（bump のみ）。unregister で消すと
        再登録時に 1 に戻り、進行中タスクの capture と衝突して無効化する。
        """
        self._generations[name] = self._generations.get(name, 0) + 1

    async def _create_proxy(
        self, name: str, config: dict, expected_gen: int | None = None
    ) -> tuple[FastMCPProxy, Client]:
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
            # timeout は float 秒のまま (4.x でも単位不変のため変更なし)
            client = Client(transport=transport, timeout=self._client_timeout())
        elif command:
            args = config.get("args", [])
            env = config.get("env")
            transport = StdioTransport(command=command, args=args, env=env)  # type: ignore[assignment]
            client = Client(transport=transport)
        else:
            raise ValueError(f"Invalid config for {name}: need 'url' or 'command'")
        # 4.x header forwarding: TransportOptions.forward_incoming_headers.
        # Transports read it at connect time via _get_forwardable_http_headers()
        # (authorization + custom proxy headers; mcp-*/last-event-id stripped,
        # Cookie excluded — escape hatch is get_http_headers(include={"cookie"})).
        # Must be set BEFORE __aenter__; the old transport.forward_incoming_headers
        # attribute no longer exists in 4.x. Honored by HTTP/SSE, ignored by stdio.
        # Fresh clients default _transport_options to None (4.x Client has
        # no transport_options ctor param); never read it back here —
        # autospec mocks lack the instance attr. The write below is honored
        # by HTTP/SSE transports at connect time.
        client._transport_options = replace(
            TransportOptions(),
            forward_incoming_headers=True,
        )
        # 接続確立（Client は reentrant: __aenter__ で接続、close で切断）
        try:
            await client.__aenter__()
        except BaseException:
            # CancelledError（recovery の wait_for タイムアウト等）でも close を
            # 保証。BaseException で捕まえないと誰も close せず、stdio サーバーの
            # サブプロセスが孤児として蓄積する（Client.close → transport close
            # で kill される）。close 自体の例外は握り潰して元例外を優先する
            # （接続済みセッションの切断失敗が元の失敗原因を隠さないように）。
            try:
                await client.close()
            except Exception:
                logger.debug("Client cleanup after failed connect raised", exc_info=True)
            raise
        try:
            # 4.x: FastMCPProxy builds ProxyProvider(client_factory) internally;
            # per-request fresh sessions come from ProxyClient.new() for
            # non-Client targets, while our connected-Client factory keeps the
            # single-session reuse (handshake avoidance + error short-circuit).
            # mode="auto" default is kept (not pinned to legacy) for v1 fallback.
            proxy = FastMCPProxy(
                client_factory=self._make_client_factory(name, client),
                name=name,
                provider_error_strategy="raise",
            )
        except Exception:
            await client.close()  # FastMCPProxy 生成失敗時のリーク防止
            raise
        displaced = None
        stale = False
        async with self._lock:
            if expected_gen is not None and self._generations.get(name) != expected_gen:
                # 世代不一致 — 接続中に削除/再登録/改名された。publish せず破棄
                stale = True
            else:
                # 差し替え後の旧 client を回収（dict から消えると close_all の対象外になるため）。
                # 「closer は必ず先に pop」の不変条件により、dict 内の client は誰にも close されていない
                displaced = self._clients.pop(name, None)
                self._clients[name] = client
        if stale:
            try:
                await client.close()
            except Exception:
                logger.debug("Stale connect cleanup for %s raised", name, exc_info=True)
            raise StaleConnectError(name)
        if displaced is not None and displaced is not client:
            try:
                await displaced.close()  # 差し替えられた旧 client の回収
            except Exception:
                logger.debug("Displaced client close raised for %s", name, exc_info=True)
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
        # 4.x: unmount/remove_provider は不在のため、リセットは providers
        # リストの作り直し + 公開 mount(namespace=) (= add_provider 経路) での
        # 再マウントで行う。self.mcp の object identity は main.py が保持
        # (middleware/http_app 構築元) するため FastMCP(providers=[...]) での
        # 差し替えはしない。local_provider は公開 property。
        # _server_instances には触らない (shutdown terminate 専用)。
        self._rebuild_complete.clear()
        try:
            mounted = self.mcp.providers
            mounted.clear()
            mounted.append(self.mcp.local_provider)

            # 全 proxy を再マウント
            for srv_name, proxy in self._proxies.items():
                self.mcp.mount(proxy, namespace=srv_name)
        finally:
            self._rebuild_complete.set()
