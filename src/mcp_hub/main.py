"""
MCP Hub - MCPプロキシ + 管理Web UI

エントリーポイント:
  python -m mcp_hub.main

環境変数:
  MCP_HUB_PORT      : リスンポート (default: 26263)
  MCP_HUB_HOST      : バインドホスト (default: 0.0.0.0)
  MCP_HUB_DB_PATH   : DBファイルパス (default: data/hub.db)
  MCP_HUB_CONFIG    : 設定ファイルパス (default: hub.config.json)
  MCP_HUB_RESEED    : 1 でDBを初期化し設定ファイルから再シード
  MCP_HUB_LOG       : "json" でJSON形式ログ出力
"""

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastmcp import FastMCP

from .admin_router import router as admin_router
from .config import load_config
from .proxy_manager import ProxyManager
from .registry import SqliteStore
from .state import app_state, request_tags

logger = logging.getLogger(__name__)

# 設定
PORT = int(os.environ.get("MCP_HUB_PORT", "26263"))
HOST = os.environ.get("MCP_HUB_HOST", "0.0.0.0")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI のライフスパン: 起動時/終了時の処理。"""
    # --- 初期化 ---
    app_state.start_time = time.time()

    # 設定ファイルをロード
    config = load_config()
    logger.info("Loaded config: %d servers", len(config.servers))

    # ログレベルを設定ファイルから適用
    if config.log_level:
        logging.getLogger().setLevel(config.log_level.upper())

    registry = SqliteStore()
    await registry.init(seed_servers=config.servers)
    logger.info("Registry initialized")

    mcp_server = FastMCP("MCP Hub")
    proxy_manager = ProxyManager(mcp_server, registry)

    # 共有状態にセット（admin_router から参照可能に）
    app_state.registry = registry
    app_state.proxy_manager = proxy_manager

    # DB から全サーバーを復元・マウント
    await proxy_manager.load_all()
    logger.info("Loaded %d proxy servers", len(proxy_manager._proxies))

    # 内部リソース: hub://servers — 接続サーバーのJSONスナップショット
    @mcp_server.resource("hub://servers")
    def get_hub_servers() -> str:
        """Return JSON snapshot of connected servers."""
        servers_info = []
        for name, config in proxy_manager._server_configs.items():
            servers_info.append(
                {
                    "name": name,
                    "disabled": config.get("disabled", False),
                    "tags": config.get("tags", []),
                    "status": proxy_manager._status.get(name, "unknown"),
                    "tool_count": len(proxy_manager._proxies.get(name, [])),
                }
            )
        return json.dumps(servers_info, indent=2, ensure_ascii=False)

    # FastMCP の HTTP ASGI アプリを生成してマウント
    # path="/" は mount 先が /mcp なので sub-app のルートで受けるため
    mcp_http = mcp_server.http_app(transport="streamable-http", path="/")
    app.mount("/mcp", mcp_http)

    # FastMCP のライフスパンを手動で実行
    # (mounted ASGI サブアプリの lifespan は親から自動実行されない)
    # 内部の StreamableHTTPASGIApp を見つけて session_manager を設定する
    # NOTE: The following uses FastMCP internal/private APIs (_mcp_server,
    # _lifespan_manager, session_manager). These may break across FastMCP
    # minor version updates. FastMCP is pinned to <3.5.0 in pyproject.toml.
    # When upgrading FastMCP, verify these attributes still exist.
    from fastmcp.server.http import (
        FastMCPStreamableHTTPSessionManager,
        StreamableHTTPASGIApp,
    )

    inner_app: StreamableHTTPASGIApp | None = None
    for route in mcp_http.routes:
        if isinstance(getattr(route, "endpoint", None), StreamableHTTPASGIApp):
            inner_app = route.endpoint
            break

    if inner_app is None:
        raise RuntimeError("Could not find StreamableHTTPASGIApp in mounted routes")

    sm = FastMCPStreamableHTTPSessionManager(
        app=mcp_server._mcp_server,
    )
    inner_app.session_manager = sm

    async with mcp_server._lifespan_manager(), sm.run():
        logger.info("MCP Hub started on %s:%s", HOST, PORT)
        yield

    # --- 終了処理 ---
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

    # 管理 API ルーターをマウント
    app.include_router(admin_router)

    # --- tag filtering middleware (/mcp のみ) ---
    @app.middleware("http")
    async def tag_middleware(request: Request, call_next):
        try:
            if request.url.path.startswith("/mcp"):
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
        return json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            },
            ensure_ascii=False,
        )


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
