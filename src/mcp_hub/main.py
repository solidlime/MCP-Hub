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
import os
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import fastmcp
from fastmcp import FastMCP

from . import bootstrap as _bootstrap
from .admin_router import router as admin_router
from .auth import ApiKeyMiddleware
from .config import load_config
from .full_info import FullInfoMiddleware
from .lenient_session_manager import LenientSessionManager
from .masking import mask_text
from .middleware import ToolLogMiddleware
from .proxy_manager import ProxyManager
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


class MCPDispatcher:
    """ASGI dispatcher with cached meta_mode. Call invalidate_cache() after toggling."""

    def __init__(self, normal_app, meta_app, normal_sm=None, meta_sm=None):
        self.normal_app = normal_app
        self.meta_app = meta_app
        self._normal_sm = normal_sm
        self._meta_sm = meta_sm
        self._cached_meta_mode: bool | None = None
        self._last_active_side: str | None = None
        import asyncio
        self._cleanup_task = asyncio.create_task(self._session_cleanup_loop())
        self._shutdown = False

    def invalidate_cache(self):
        self._cached_meta_mode = None

    async def shutdown(self):
        """Cancel background cleanup task. Call during lifespan cleanup."""
        self._shutdown = True
        if hasattr(self, '_cleanup_task'):
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.exceptions.CancelledError:
                pass

    async def _session_cleanup_loop(self):
        """Cadence/shutdown hook for session hygiene.

        4.x: idle expiry is owned by the SDK (session_idle_timeout= on SM
        construction). _cleanup_stale no longer exists, so this loop only
        sleeps until shutdown — kept as the lifecycle hook.
        """
        import asyncio
        while not self._shutdown:
            try:
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                break

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI のライフスパン: 起動時/終了時の処理。"""
    # --- 初期化 ---
    from .streamable_http_patch import apply_patch

    apply_patch()

    app_state.start_time = time.time()

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

    # === meta app (Progressive Discovery, used when meta_mode=True) ===
    # Create meta_app early and wire on_change BEFORE load_all
    # so that background connections trigger index rebuilds.
    from .meta_provider import create_meta_app

    meta_app = create_meta_app(
        proxy_manager,
        embedding_model=config.embedding_model,
        use_embeddings=config.use_embeddings,
    )

    app_state.meta_app = meta_app

    # ツールログ記録ミドルウェア（meta 側）— meta モード時はリクエスト全体が
    # meta_app に回るため、こちらにも登録しないとログが欠落する
    meta_app.mcp.add_middleware(ToolLogMiddleware(proxy_manager))

    # フル公開（メタOFF）ミドルウェア。ToolLog の直後に登録する（先に add した
    # 方が外側で先に実行される）。FullInfo が on_call_tool で直接転送
    # （call_next スキップ）しても ToolLog が必ず実行され、ログ・metrics が保証される。
    meta_app.mcp.add_middleware(
        FullInfoMiddleware(proxy_manager, get_schema_fn=meta_app.index.get_schema)
    )

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
        logger.info("MCP Hub started on %s:%s", HOST, PORT)
        yield

    # --- 終了処理 ---
    await dispatcher.shutdown()
    await proxy_manager.stop_health_monitor()
    await proxy_manager.close_all()  # upstream 接続の切断（ゾンビ防止）
    logger.info("MCP Hub shutting down")
    app_state.registry = None
    app_state.proxy_manager = None



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

    return app


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

    # 管理 UI (index.html)
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    index_html = os.path.join(static_dir, "index.html")
    if os.path.exists(index_html):
        # index.html を /admin/ で配信
        from pathlib import Path

        html_content = Path(index_html).read_text(encoding="utf-8")

        @app.get("/")
        @app.get("/admin/")
        @app.get("/admin")
        async def admin_index():
            return HTMLResponse(html_content)

    # 静的ファイル配信 (admin UI 用の追加アセット用)
    if os.path.isdir(static_dir):
        app.mount(
            "/admin/static",
            StaticFiles(directory=static_dir),
            name="admin-static",
        )

    import uvicorn

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
