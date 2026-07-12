"""
MCP Hub - MCPプロキシ + 管理Web UI

エントリーポイント:
  python -m mcp_hub.main

環境変数:
  MCP_HUB_PORT      : リスンポート (default: 26263)
  MCP_HUB_HOST      : バインドホスト (default: 0.0.0.0)
  MCP_HUB_DB_PATH   : DBファイルパス (default: data/hub.db)
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastmcp import FastMCP

from .admin_router import router as admin_router
from .proxy_manager import ProxyManager
from .registry import SqliteStore
from .state import app_state

logger = logging.getLogger(__name__)

# 設定
PORT = int(os.environ.get("MCP_HUB_PORT", "26263"))
HOST = os.environ.get("MCP_HUB_HOST", "0.0.0.0")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI のライフスパン: 起動時/終了時の処理。"""
    # --- 初期化 ---
    registry = SqliteStore()
    await registry.init()
    logger.info("Registry initialized")

    mcp_server = FastMCP("MCP Hub")
    proxy_manager = ProxyManager(mcp_server, registry)

    # 共有状態にセット（admin_router から参照可能に）
    app_state.registry = registry
    app_state.proxy_manager = proxy_manager

    # DB から全サーバーを復元・マウント
    await proxy_manager.load_all()
    logger.info("Loaded %d proxy servers", len(proxy_manager._proxies))

    # FastMCP の HTTP ASGI アプリを生成してマウント
    # path="/" は mount 先が /mcp なので sub-app のルートで受けるため
    mcp_http = mcp_server.http_app(transport="streamable-http", path="/")
    app.mount("/mcp", mcp_http)

    # FastMCP のライフスパンを手動で実行
    # (mounted ASGI サブアプリの lifespan は親から自動実行されない)
    # 内部の StreamableHTTPASGIApp を見つけて session_manager を設定する
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
    return app


def main():
    """エントリーポイント。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = create_app()

    # 管理 API ルーターをマウント
    app.include_router(admin_router)

    # 管理 UI (index.html)
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    index_html = os.path.join(static_dir, "index.html")
    if os.path.exists(index_html):
        # index.html を /admin/ で配信
        from pathlib import Path

        html_content = Path(index_html).read_text(encoding="utf-8")

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
