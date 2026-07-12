"""
管理 REST API。
プレフィックス: /admin/api
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .state import app_state

logger = logging.getLogger(__name__)


# --- Schemas ---


class ServerConfig(BaseModel):
    url: str | None = None
    command: str | None = None
    args: list[str] = []

    def model_dump_for_config(self) -> dict:
        """空文字・空リストを除外した config dict を返す。"""
        raw = self.model_dump(exclude_none=True)
        if "url" in raw and not raw["url"]:
            del raw["url"]
        if "command" in raw and not raw["command"]:
            del raw["command"]
        if "args" in raw and not raw["args"]:
            del raw["args"]
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
    pm = _get_proxy_manager()
    return {
        "status": "ok",
        "servers": len(pm._proxies),
    }


@router.get("/servers")
async def list_servers():
    registry = _get_registry()
    pm = _get_proxy_manager()

    servers = await registry.list_servers()
    tools_map = await pm.list_tools()

    result = []
    for srv in servers:
        name = srv["name"]
        info = {
            "name": name,
            "config": srv["config"],
            "tools_count": len(tools_map.get(name, [])),
            "tools": tools_map.get(name, []),
        }
        result.append(info)
    return {"servers": result}


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
    try:
        result = await pm.call_tool(name, tool_name, body.arguments)
        return {"result": result}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.exception("Tool call failed %s/%s", name, tool_name)
        raise HTTPException(status_code=500, detail=str(e)) from e
