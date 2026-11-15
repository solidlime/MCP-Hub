"""
MCP Hub - MCPプロキシ + 管理Web UI

エントリーポイント:
  python -m mcp_hub.main

環境変数:
  MCP_HUB_PORT      : リスンポート (default: 26263)
  MCP_HUB_HOST      : バインドホスト (default: 0.0.0.0)
  MCP_HUB_DATA_DIR  : データディレクトリ (default: data)
                      設定ファイル → {dir}/hub.config.json
                      DB           → {dir}/hub.db
  MCP_HUB_RESEED    : 1 でDBをクリアして設定ファイルから再シード
  MCP_HUB_LOG       : "json" でJSON形式ログ出力
"""

import asyncio
import json
import logging
import math
import os
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
import fastmcp
from fastmcp import FastMCP

from . import bootstrap as _bootstrap
from .admin_router import _embedding_rebuild_task_ref
from .admin_router import router as admin_router
from .auth import ApiKeyMiddleware
from .config import DEFAULT_DRAIN_TIMEOUT, load_config
from .full_info import FullInfoMiddleware
from .lenient_session_manager import LenientSessionManager
from .masking import mask_text
from .middleware import LimitsMiddleware, ToolLogMiddleware
from .proxy_manager import ProxyManager
from .reload import watch_config
from .store import JsonStore
from .state import LogEntry, app_state, request_tags
from .tag_filter import TagFilterMiddleware

_bootstrap.setup_path()
_bootstrap.setup_env()

logger = logging.getLogger(__name__)

# 設定
_DEFAULT_PORT = 26263


def _get_port() -> int:
    """MCP_HUB_PORT を検証して返す。不正値は警告+既定値フォールバック。"""
    raw = os.environ.get("MCP_HUB_PORT", str(_DEFAULT_PORT))
    try:
        port = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid MCP_HUB_PORT=%r — falling back to %d", raw, _DEFAULT_PORT
        )
        return _DEFAULT_PORT
    if not 1 <= port <= 65535:
        logger.warning(
            "MCP_HUB_PORT=%d out of range — falling back to %d", port, _DEFAULT_PORT
        )
        return _DEFAULT_PORT
    return port


PORT = _get_port()
HOST = os.environ.get("MCP_HUB_HOST", "0.0.0.0")


def _session_idle_timeout() -> float | None:
    """Upstream session idle expiry (seconds) for 4.x session managers.

    4.x removed SessionManager._cleanup_stale(); idle expiry is now owned by
    the SDK via session_idle_timeout=. Default None (= SDK default, no expiry)
    preserves current behavior; set MCP_HUB_SESSION_IDLE_TIMEOUT to trim idle
    upstream sessions. _server_instances is shutdown-terminate only — untouched.
    """
    raw = os.environ.get("MCP_HUB_SESSION_IDLE_TIMEOUT")
    return float(raw) if raw else None


def _watch_interval() -> float:
    """Config hot-reload watch interval (seconds). 0/unset/invalid = disabled."""
    raw = os.environ.get("MCP_HUB_WATCH_CONFIG")
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning("Invalid MCP_HUB_WATCH_CONFIG=%r — file watch disabled", raw)
        return 0.0


def _drain_timeout(config=None) -> float:
    """ドレイン待機時間（秒）。MCP_HUB_DRAIN_TIMEOUT > config > DEFAULT。"""
    fallback = getattr(config, "shutdown_drain_timeout", DEFAULT_DRAIN_TIMEOUT)
    raw = os.environ.get("MCP_HUB_DRAIN_TIMEOUT")
    if not raw:
        return fallback
    try:
        value = float(raw)
        if value < 0:
            raise ValueError("negative")
        return value
    except ValueError:
        logger.warning("Invalid MCP_HUB_DRAIN_TIMEOUT=%r — using %gs", raw, fallback)
        return fallback


class MCPDispatcher:
    """ASGI dispatcher with cached meta_mode. Call invalidate_cache() after toggling."""

    def __init__(self, normal_app, meta_app, normal_sm=None, meta_sm=None):
        self.normal_app = normal_app
        self.meta_app = meta_app
        self._normal_sm = normal_sm
        self._meta_sm = meta_sm
        self._cached_meta_mode: bool | None = None

    def invalidate_cache(self):
        self._cached_meta_mode = None

    async def shutdown(self):
        """Lifespan cleanup hook. Idle session expiry is owned by the SDK."""

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.normal_app(scope, receive, send)
            return

        if self._cached_meta_mode is None:
            from .state import app_state
            try:
                registry = app_state.registry
                data = await registry._read() if registry is not None else {}
                self._cached_meta_mode = data.get("meta_mode", False)
            except Exception:
                self._cached_meta_mode = False

        target = self.meta_app if self._cached_meta_mode else self.normal_app
        await target(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI のライフスパン: 起動時/終了時の処理。"""
    # --- 初期化 ---
    from .streamable_http_patch import apply_patch

    apply_patch()

    app_state.start_time = time.time()
    # draining/inflight はプロセス全体のフラグ。lifespan が同一プロセスで
    # 再利用される経路（テスト・ホットリロード）で前回の停止状態を残さない。
    app_state.draining = False
    app_state.inflight = 0

    # 設定ファイルをロード
    config = load_config()
    logger.info("Loaded config: %d servers", len(config.servers))

    # ログレベルを設定ファイルから適用
    if config.log_level and isinstance(config.log_level, str):
        logging.getLogger().setLevel(config.log_level.upper())

    registry = JsonStore()
    await registry.init(seed_servers=config.servers)
    logger.info("Registry initialized")

    mcp_server = FastMCP("MCP Hub")

    # Check FastMCP version compatibility
    # NOTE: We use FastMCP internal/private APIs (_mcp_server,
    # _lifespan_manager, session_manager, providers, local_provider).
    # These may break across FastMCP major version updates.
    # Tested against <5.0.0.
    try:
        from packaging import version as _v

        _fver = _v.parse(fastmcp.__version__)
        if _fver >= _v.parse("5.0.0"):
            logger.warning(
                "FastMCP %s may not be compatible (tested against <5.0.0). "
                "Internal APIs used by MCP-Hub may have changed.",
                fastmcp.__version__,
            )
    except ImportError:
        pass

    proxy_manager = ProxyManager(mcp_server, registry)

    # 共有状態にセット（admin_router から参照可能に）
    app_state.registry = registry
    app_state.proxy_manager = proxy_manager
    # 任意機能: ツール結果キャッシュの設定を適用（既定は無効）
    app_state.tool_cache.configure((registry._data or {}).get("tool_cache"))

    # === meta app (Progressive Discovery, used when meta_mode=True) ===
    # Create meta_app early and wire on_change BEFORE load_all
    # so that background connections trigger index rebuilds.
    from .meta_provider import create_meta_app

    meta_app = await create_meta_app(
        proxy_manager,
        embedding_model=config.embedding_model,
        use_embeddings=config.use_embeddings,
    )

    app_state.meta_app = meta_app

    # フル公開（メタOFF）ミドルウェア。先に登録する（FastMCP は後から add した
    # ものを外側で実行するため、後に登録する ToolLog が FullInfo を包む）。
    # FullInfo が on_call_tool で直接転送（call_next スキップ）しても
    # ToolLog が必ず実行され、ログ・metrics・結果キャッシュが保証される。
    meta_app.mcp.add_middleware(
        FullInfoMiddleware(proxy_manager, get_schema_fn=meta_app.index.get_schema)
    )

    # ツールログ記録ミドルウェア（meta 側）— meta モード時はリクエスト全体が
    # meta_app に回るため、こちらにも登録しないとログが欠落する
    meta_app.mcp.add_middleware(ToolLogMiddleware(proxy_manager))

    # サーバー単位の timeout / 同時実行 / レート制限（最外殻: 後から add = 外側）
    meta_app.mcp.add_middleware(LimitsMiddleware(proxy_manager))

    # Debounced rebuild wrapper — coalesces rapid on_change calls (e.g. startup
    # cascade where multiple servers connect within milliseconds) into a single
    # rebuild_index() run.  Explicit await meta_app.rebuild_index() calls are
    # NOT debounced.
    #
    # A server that just recovered may still be warming up, so a failed rebuild
    # is retried with backoff before giving up (see meta_provider.rebuild_index
    # returning the list of failed servers).
    _rebuild_task: asyncio.Task | None = None
    _REBUILD_RETRY_DELAYS = (10, 30, 60)

    async def _on_change_rebuild(name: str | None = None, event: str | None = None, detail: dict | None = None):
        nonlocal _rebuild_task
        if app_state.draining:
            # 停止中に再接続イベントが遅れて届いても再構築を起こし直さない
            # （_graceful_drain が cancel した後にタスクが残るのを防ぐ）。
            return
        if _rebuild_task and not _rebuild_task.done():
            _rebuild_task.cancel()
        async def _delayed():
            await asyncio.sleep(0.5)
            failed = await meta_app.rebuild_index()
            for delay in _REBUILD_RETRY_DELAYS:
                if not failed:
                    break
                logger.warning(
                    "Meta index missing %d server(s): %s — retrying in %ds",
                    len(failed), failed, delay,
                )
                await asyncio.sleep(delay)
                failed = await meta_app.rebuild_index()
            if failed:
                logger.warning("Meta index still missing servers after retries: %s", failed)
        _rebuild_task = asyncio.create_task(_delayed())

    async def _on_log_event(name: str, event: str, detail: dict | None = None):
        """サーバー接続イベントをログバッファに記録する。"""
        status = event
        error = None
        if detail and detail.get("error"):
            error = mask_text(str(detail["error"])[:500])
        app_state.append_log(LogEntry(
            ts=time.time(),
            type="server_event",
            server=name,
            tool="-",
            status=status,
            error=error,
        ))

    proxy_manager.on_change(_on_change_rebuild)
    proxy_manager.on_change(_on_log_event)

    # DB から全サーバーを復元・マウント
    await proxy_manager.load_all()
    logger.info("Loaded %d proxy servers", len(proxy_manager._proxies))

    # タグフィルタリングミドルウェアを登録
    # tools/list, prompts/list, resources/list の応答を
    # X-MCP-Hub-Tags ヘッダーに基づいてフィルタする
    mcp_server.add_middleware(TagFilterMiddleware(proxy_manager))

    # ツールログ記録ミドルウェア（normal 側）
    mcp_server.add_middleware(ToolLogMiddleware(proxy_manager))

    # サーバー単位の timeout / 同時実行 / レート制限（最外殻: 後から add = 外側）
    mcp_server.add_middleware(LimitsMiddleware(proxy_manager))

    # 内部リソース: hub://servers — 接続サーバーのJSONスナップショット
    @mcp_server.resource("hub://servers")
    def get_hub_servers() -> str:
        """Return JSON snapshot of connected servers."""
        servers_info = proxy_manager.get_servers_info()
        return json.dumps(servers_info, indent=2, ensure_ascii=False)

    # FastMCP の HTTP ASGI アプリを生成
    # path="/" は mount 先が /mcp なので sub-app のルートで受けるため
    # 4.x: http_app(path, middleware, json_response, stateless_http, transport, ...) — path先頭
    mcp_http = mcp_server.http_app(path="/", transport="streamable-http")

    # FastMCP のライフスパンを手動で実行
    # (mounted ASGI サブアプリの lifespan は親から自動実行されない)
    # 内部の StreamableHTTPASGIApp を見つけて session_manager を設定する
    # NOTE: The following uses FastMCP internal/private APIs (_mcp_server,
    # _lifespan_manager, session_manager). These may break across FastMCP
    # major version updates. FastMCP is >=4.0,<5.0 in pyproject.toml.
    # When upgrading FastMCP, verify these attributes still exist.
    # Route scan matches ONLY the StreamableHTTPASGIApp endpoint: modern-era
    # extra routes (server/discover, auth RequireAuthMiddleware, HostOriginGuard)
    # never match this isinstance check, so no false positives.
    from fastmcp.server.http import StreamableHTTPASGIApp

    inner_app: StreamableHTTPASGIApp | None = None
    for route in mcp_http.routes:
        if isinstance(getattr(route, "endpoint", None), StreamableHTTPASGIApp):  # type: ignore[attr-defined]
            inner_app = route.endpoint  # type: ignore[attr-defined]
            break

    if inner_app is None:
        raise RuntimeError("Could not find StreamableHTTPASGIApp in mounted routes")

    sm = LenientSessionManager(
        app=mcp_server._mcp_server,
        session_idle_timeout=_session_idle_timeout(),
    )
    inner_app.session_manager = sm

    # === meta app (Progressive Discovery, used when meta_mode=True) ===
    meta_mcp = meta_app.mcp
    meta_http = meta_mcp.http_app(path="/", transport="streamable-http")

    meta_inner_app: StreamableHTTPASGIApp | None = None
    for route in meta_http.routes:
        if isinstance(getattr(route, "endpoint", None), StreamableHTTPASGIApp):  # type: ignore[attr-defined]
            meta_inner_app = route.endpoint  # type: ignore[attr-defined]
            break

    if meta_inner_app is None:
        raise RuntimeError("Could not find StreamableHTTPASGIApp in meta routes")

    meta_sm = LenientSessionManager(
        app=meta_mcp._mcp_server,
        session_idle_timeout=_session_idle_timeout(),
    )
    meta_inner_app.session_manager = meta_sm

    # Rebuild index after initial load (safety net — background
    # connections that completed during setup will have already
    # triggered rebuilds via on_change callbacks)
    await meta_app.rebuild_index()

    # /mcp に動的ディスパッチャをマウント
    dispatcher = MCPDispatcher(mcp_http, meta_http, sm, meta_sm)
    app_state.mcp_dispatcher = dispatcher
    app.mount("/mcp", dispatcher)

    # Python 3.12+ parenthesized context managers
    # 4.x: _mcp_server 注入 + _lifespan_manager()+sm.run() を1回ずつ維持
    # (lifespan exactly-once 前提 — verified present in 4.0.2)
    async with (
        mcp_server._lifespan_manager(),
        sm.run(),
        meta_mcp._lifespan_manager(),
        meta_sm.run(),
    ):
        proxy_manager.start_health_monitor()
        # 任意: 設定ファイルの mtime 監視（MCP_HUB_WATCH_CONFIG=<秒>, 0=off）
        _watch_secs = _watch_interval()
        watch_task = (
            asyncio.create_task(watch_config(_watch_secs, registry=registry))
            if _watch_secs > 0
            else None
        )
        logger.info("MCP Hub started on %s:%s", HOST, PORT)
        try:
            yield
        finally:
            # --- グレースフルシャットダウン ---
            # 1) 新規受付停止（HTTP 503 / tool call 拒否）
            # 2) 進行中リクエストをタイムアウト付きで待つ
            # 3) ウォッチ/再接続/再構築/ヘルスループを止め、upstream を閉じる
            await _graceful_drain(
                config, proxy_manager, watch_task, _rebuild_task
            )

    # --- 終了処理 ---
    await dispatcher.shutdown()
    await proxy_manager.close_all()  # upstream 接続の切断（ゾンビ防止）
    logger.info("MCP Hub shutting down")
    app_state.registry = None
    app_state.proxy_manager = None


async def _graceful_drain(
    config,
    proxy_manager: ProxyManager,
    watch_task: asyncio.Task | None,
    rebuild_task: asyncio.Task | None,
) -> None:
    """SIGTERM/SIGINT 後の停止シーケンス（新規拒否→ドレイン→クローズ）。

    各段階は失敗しても次に進む（1 個の失敗でゾンビプロセスを残さない）。
    """
    timeout = _drain_timeout(config)
    proxy_manager.begin_drain()
    remaining = app_state.begin_drain()
    logger.info(
        "Shutdown: refusing new requests, draining %d in-flight request(s) "
        "(timeout=%gs)",
        remaining, timeout,
    )
    started = time.monotonic()
    remaining = await app_state.wait_idle(timeout)
    elapsed = time.monotonic() - started
    if remaining:
        logger.warning(
            "Shutdown: %d request(s) still in flight after %.2fs — closing anyway",
            remaining, elapsed,
        )
    else:
        logger.info("Shutdown: drained in %.2fs", elapsed)

    # 設定ファイル監視 / 索引再構築 / 埋め込み再構築のバックグラウンドタスク
    for task in (watch_task, rebuild_task, _embedding_rebuild_task_ref()):
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — 停止処理は継続する
                logger.warning("Background task failed during shutdown", exc_info=True)

    await proxy_manager.stop_health_monitor()
    # 連接/再接続タスクの回収（session manager 停止前に子プロセスの親を切る）
    await proxy_manager.cancel_background_tasks()
    logger.info("Shutdown: background loops stopped (drain %.2fs)", time.monotonic() - started)



def create_app() -> FastAPI:
    """FastAPI アプリケーションを生成。"""
    app = FastAPI(
        title="MCP Hub Admin",
        version="0.1.0",
        lifespan=lifespan,
    )

    # X-API-Key 認証ミドルウェア (MCP_HUB_API_KEY 環境変数で有効化)
    app.add_middleware(ApiKeyMiddleware)

    # 管理 API ルーターをマウント
    app.include_router(admin_router)

    # --- tag filtering + slash normalization middleware (/mcp のみ) ---
    @app.middleware("http")
    async def tag_middleware(request: Request, call_next):
        # グレースフルシャットダウン中は新規リクエストを拒否（ドレイン）
        if app_state.draining:
            return PlainTextResponse(
                "Server is shutting down", status_code=503, headers={"Retry-After": "1"}
            )
        app_state.enter_request()
        try:
            if request.url.path.startswith("/mcp"):
                # 正規化: Mount("/mcp") の正規表現 ^/mcp/(?P<path>.*)$ は
                # 末尾スラッシュ必須のため、/mcp への POST が 307 redirect を
                # 引き起こす。ASGI scope でパスを /mcp/ に書き換えて回避。
                if request.url.path == "/mcp":
                    request.scope["path"] = "/mcp/"
                    request.scope["raw_path"] = b"/mcp/"
                header_tags = request.headers.get("X-MCP-Hub-Tags", "")
                query_tags = request.query_params.get("tags", "")
                tags_raw = header_tags if header_tags else query_tags
                if tags_raw:
                    request_tags.set([t.strip() for t in tags_raw.split(",") if t.strip()])
            response = await call_next(request)
            return response
        finally:
            request_tags.set(None)
            app_state.leave_request()

    # --- Prometheus メトリクス（/metrics）---
    # スクレイパ向けに /admin/api の外に置く（ApiKeyMiddleware は /admin/api/*
    # のみ隤す。可視性は /mcp と同じで bind ホスト依存）。
    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics():
        gauges: list[tuple[str, dict[str, str], float]] = [
            ("mcp_hub_uptime_seconds", {}, round(time.time() - app_state.start_time, 1)),
        ]
        registry = app_state.registry
        if registry is not None:
            try:
                gauges.append(
                    ("mcp_hub_servers_registered", {}, float(len(await registry.list_servers())))
                )
            except Exception:
                logger.debug("metrics: registry snapshot failed", exc_info=True)
        pm = app_state.proxy_manager
        if pm is not None:
            gauges.append(("mcp_hub_servers_active", {}, float(len(pm._proxies))))
            gauges.append(
                ("mcp_hub_tools_total", {}, float(sum(pm._tool_counts.values())))
            )
            for name, status in pm.get_all_status().items():
                gauges.append(
                    ("mcp_hub_server_up", {"server": name}, 1.0 if status == "connected" else 0.0)
                )
        index = getattr(app_state.meta_app, "index", None)
        if index is not None:
            gauges.append(
                (
                    "mcp_hub_tools_indexed",
                    {},
                    float(len(getattr(index, "_documents", None) or [])),
                )
            )
        return PlainTextResponse(
            app_state.metrics.render(gauges),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    _mount_admin_ui(app)

    return app


def _mount_admin_ui(app: FastAPI) -> None:
    """管理 UI とアイコンを配信する。index.html が無ければ何もしない。"""
    from pathlib import Path

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    index_html = os.path.join(static_dir, "index.html")
    if not os.path.exists(index_html):
        return

    html_content = Path(index_html).read_text(encoding="utf-8")

    @app.get("/")
    @app.get("/admin/")
    @app.get("/admin")
    async def admin_index():
        return HTMLResponse(html_content)

    # アイコンは HTML からの相対参照（index.html の icon.svg は二つの URL に当たる）
    @app.get("/icon.svg", include_in_schema=False)
    @app.get("/admin/icon.svg", include_in_schema=False)
    async def admin_icon():
        return FileResponse(os.path.join(static_dir, "icon.svg"), media_type="image/svg+xml")


class JsonFormatter(logging.Formatter):
    """JSON 構造化ログフォーマッター。"""

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


def main():
    """エントリーポイント。"""
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

    app = create_app()

    import uvicorn

    # uvicorn に待機上限を渡さないと、SSE などの長期接続が閉じるまで lifespan
    # shutdown（ドレイン）に到達せず、SIGTERM が終わらない。int 秒なので、
    # drain の待機より短くならないよう切り上げる。
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
        timeout_graceful_shutdown=math.ceil(_drain_timeout(load_config())),
    )


if __name__ == "__main__":
    main()
