"""
管理 REST API。
プレフィックス: /admin/api
"""

import logging
import time
from typing import Any
from urllib.parse import urljoin

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .state import app_state

logger = logging.getLogger(__name__)


# --- Schemas ---


class ServerConfig(BaseModel):
    url: str | None = None
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}  # ← 追加: BRAVE_API_KEY 等
    tags: list[str] = []
    disabled: bool = False

    def model_dump_for_config(self) -> dict:
        """空文字・空リストを除外した config dict を返す。"""
        raw = self.model_dump(exclude_none=True)
        for key in ("url", "command", "args", "env", "tags"):
            if key in raw and not raw[key]:
                del raw[key]
        return raw


class RegisterRequest(BaseModel):
    name: str
    config: ServerConfig


class CallToolRequest(BaseModel):
    arguments: dict[str, Any] = {}


# --- Router ---

router = APIRouter(prefix="/admin/api")


def _get_registry():
    if app_state.registry is None:
        raise RuntimeError("Registry not initialized")
    return app_state.registry


def _get_proxy_manager():
    if app_state.proxy_manager is None:
        raise RuntimeError("ProxyManager not initialized")
    return app_state.proxy_manager


@router.get("/health")
async def health():
    try:
        pm = _get_proxy_manager()
        servers = len(pm._proxies)
    except RuntimeError:
        servers = 0
    return {
        "status": "ok",
        "servers": servers,
    }


@router.get("/metrics")
async def metrics():
    pm = _get_proxy_manager()
    registry = _get_registry()
    servers = await registry.list_servers()

    uptime = time.time() - app_state.start_time
    total_tools = sum(len(tools) for tools in (await pm.list_tools()).values())

    return {
        "uptime_seconds": round(uptime, 1),
        "servers_registered": len(servers),
        "servers_active": len(pm._proxies),
        "total_tools": total_tools,
        "tool_calls_total": app_state.tool_calls_total,
        "tool_call_errors": app_state.tool_call_errors,
    }


@router.get("/servers")
async def list_servers():
    registry = _get_registry()
    pm = _get_proxy_manager()

    servers = await registry.list_servers()
    tools_map = await pm.list_tools()
    status_map = pm.get_all_status()

    result = []
    for srv in servers:
        name = srv["name"]
        config = srv["config"]
        info = {
            "name": name,
            "config": config,
            "disabled": config.get("disabled", False),
            "status": status_map.get(name, "unknown"),
            "tools_count": len(tools_map.get(name, [])),
            "tools": tools_map.get(name, []),
        }
        result.append(info)
    return {"servers": result}


@router.get("/servers/{name}/connection")
async def connection_info(name: str, request: Request):
    registry = _get_registry()
    server = await registry.get_server(name)
    if server is None:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")

    tags = server["config"].get("tags", [])
    base_url = urljoin(str(request.base_url), "mcp")
    url = f"{base_url}?tags={','.join(tags)}" if tags else base_url

    return {
        "url": url,
        "tags": tags,
        "example_header": f"X-MCP-Hub-Tags: {','.join(tags)}" if tags else None,
    }


@router.post("/servers", status_code=201)
async def register_server(body: RegisterRequest):
    registry = _get_registry()
    pm = _get_proxy_manager()

    if not body.name.strip():
        raise HTTPException(status_code=422, detail="Server name must not be empty")

    existing = await registry.get_server(body.name)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Server '{body.name}' already exists",
        )

    config = body.config.model_dump_for_config()
    if not config.get("url") and not config.get("command"):
        raise HTTPException(
            status_code=422,
            detail="Either 'url' or 'command' is required",
        )

    try:
        tool_names = await pm.register_server(body.name, config)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Failed to register server %s", body.name)
        raise HTTPException(status_code=500, detail=str(e)) from e

    return {
        "name": body.name,
        "config": config,
        "tools": tool_names,
    }


@router.patch("/servers/{name}")
async def patch_server(name: str, body: ServerConfig):
    """サーバー設定の部分更新（PATCH）。exclude_unset で送信フィールドのみ適用。"""
    registry = _get_registry()
    pm = _get_proxy_manager()

    existing = await registry.get_server(name)
    if not existing:
        raise HTTPException(status_code=404, detail="Server not found")

    # 部分更新: 送信されたフィールドのみ既存 config にマージ
    updates = body.model_dump(exclude_unset=True)
    merged_config = existing["config"] | updates

    # 恒久化
    await registry.update_server(name, merged_config)
    await pm.refresh_server(name, merged_config)

    return {
        "name": name,
        "config": merged_config,
    }


@router.delete("/servers/{name}", status_code=204)
async def remove_server(name: str):
    pm = _get_proxy_manager()
    ok = await pm.unregister_server(name)
    if not ok:
        raise HTTPException(status_code=404, detail="Server not found")


@router.post("/servers/{name}/test")
async def test_server(name: str):
    pm = _get_proxy_manager()
    proxy = pm.get_proxy(name)
    if not proxy:
        raise HTTPException(status_code=404, detail="Server not found")

    try:
        tools = await proxy.list_tools()
        return {
            "success": True,
            "tools_count": len(tools),
            "tools": [{"name": t.name, "description": t.description or ""} for t in tools],
        }
    except Exception as e:
        return {
            "success": False,
            "tools_count": 0,
            "tools": [],
            "error": str(e),
        }


@router.post("/servers/{name}/tools/{tool_name}/call")
async def call_tool(name: str, tool_name: str, body: CallToolRequest):
    pm = _get_proxy_manager()
    app_state.tool_calls_total += 1
    try:
        result = await pm.call_tool(name, tool_name, body.arguments)
        return {"result": result}
    except ValueError as e:
        app_state.tool_call_errors += 1
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        app_state.tool_call_errors += 1
        logger.exception("Tool call failed %s/%s", name, tool_name)
        raise HTTPException(status_code=500, detail=str(e)) from e
