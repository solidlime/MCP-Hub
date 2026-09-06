"""
管理 REST API。
プレフィックス: /admin/api
"""

import asyncio
import logging
import re
import time
from typing import Any
from urllib.parse import urljoin

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from .config import DEFAULT_EMBEDDING_MODEL
from .state import app_state
from .validators import (
    ValidationError,
    validate_command,
    validate_url,
    validate_args,
    validate_env,
    validate_headers,
    validate_server_config,
    validate_server_name,
)

logger = logging.getLogger(__name__)

_ALLOWED_MANAGERS: tuple[str, ...] = ("pip", "uv", "npm")
_PIP_PKG_RE = re.compile(r"^[A-Za-z0-9_.\-]+(\[[A-Za-z0-9_.\-,]+\])?(==[A-Za-z0-9_.\-*+]+)?$")
_NPM_PKG_RE = re.compile(r"^(@[A-Za-z0-9_.\-~]+\/)?[A-Za-z0-9_.\-~]+(@[A-Za-z0-9_.\-~^]+)?$")
_EXTRAS_DIR = "/home/mcp-hub/pip-extras"
_install_sem = asyncio.Semaphore(1)


# --- Schemas ---


class ServerConfig(BaseModel):
    url: str | None = None
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}  # ← 追加: BRAVE_API_KEY 等
    tags: list[str] = []
    headers: dict[str, str] = {}
    disabled: bool = False

    def model_dump_for_config(self) -> dict:
        """空文字・空リストを除外した config dict を返す。"""
        raw = self.model_dump(exclude_none=True)
        for key in ("url", "command", "args", "env", "tags", "headers"):
            if key in raw and not raw[key]:
                del raw[key]
        return raw


class RegisterRequest(BaseModel):
    name: str
    config: ServerConfig


class PatchServerRequest(ServerConfig):
    name: str | None = None  # 新サーバー名。None なら従来の config PATCH


class CallToolRequest(BaseModel):
    arguments: dict[str, Any] = {}


class InstallRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    # Optional にして handler 側で検証する。旧 {command: str} 形式を
    # pydantic の 422 より先に 400 + 移行メッセージで拒否するため。
    manager: str | None = None
    packages: list[str] | None = None


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


def _validate_timeout(value: Any, name: str) -> float | None:
    """Validate timeout value: None (=reset) or 0 < float <= 300."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail=f"{name} は 0 より大きく 300 以下の数値、または null である必要があります",
        )
    if not (0 < f <= 300):
        raise HTTPException(
            status_code=422,
            detail=f"{name} は 0 より大きく 300 以下の数値、または null である必要があります",
        )
    return f


def _effective_use_embeddings() -> bool:
    """ライブ index の実効値を返す（ストアの意図値でなく現状値を UI に出すため）。

    meta_app 未初期化なら埋め込み検索は動作し得ないので False が真実。
    """
    index = getattr(getattr(app_state, "meta_app", None), "index", None)
    if index is None:
        return False
    return bool(index.use_embeddings)


@router.get("/settings")
async def get_settings():
    registry = _get_registry()
    data = await registry._read()
    return {
        "meta_mode": data.get("meta_mode", False),
        "full_info_tools": data.get("full_info_tools", []),
        "client_timeout": data.get("client_timeout"),
        "connect_timeout": data.get("connect_timeout"),
        "use_embeddings": _effective_use_embeddings(),
    }


@router.patch("/settings")
async def update_settings(body: dict):
    registry = _get_registry()
    if "meta_mode" in body:
        await registry.set_meta_mode(bool(body["meta_mode"]))
    if "use_embeddings" in body:
        value = body["use_embeddings"]
        # meta_mode の bool() 強制とは違い、厳密に bool を要求する
        if not isinstance(value, bool):
            raise HTTPException(
                status_code=422,
                detail="use_embeddings は真偽値（true/false）である必要があります",
            )
        await registry.set_use_embeddings(value)
        meta_app = getattr(app_state, "meta_app", None)
        if meta_app is not None:
            # 設定→実効フラグ（env ハードキルは index 側で再評価）へ反映し、
            # 検索インデックスを再構築（embed/on 切替で doc テキストが変わる）。
            meta_app.index.set_use_embeddings(value)
            try:
                await meta_app.rebuild_index()
            except Exception:
                logger.exception("Index rebuild after use_embeddings change failed")
    if "full_info_tools" in body:
        tools = body["full_info_tools"]
        if not isinstance(tools, list) or not all(
            isinstance(t, str) and "_" in t for t in tools
        ):
            raise HTTPException(
                status_code=422,
                detail="full_info_tools は '{server}_{tool}' 形式の文字列リストである必要があります",
            )
        await registry.set_full_info_tools(tools)
    if "client_timeout" in body or "connect_timeout" in body:
        data = await registry._read()
        client_timeout = _validate_timeout(
            body.get("client_timeout", data.get("client_timeout")), "client_timeout"
        )
        connect_timeout = _validate_timeout(
            body.get("connect_timeout", data.get("connect_timeout")), "connect_timeout"
        )
        await registry.set_timeouts(client_timeout, connect_timeout)
    data = await registry._read()
    return {
        "meta_mode": data.get("meta_mode", False),
        "full_info_tools": data.get("full_info_tools", []),
        "client_timeout": data.get("client_timeout"),
        "connect_timeout": data.get("connect_timeout"),
        "use_embeddings": _effective_use_embeddings(),
    }


@router.get("/settings/embedding-model")
async def get_embedding_model():
    registry = _get_registry()
    data = await registry._read()
    return {"embedding_model": data.get("embedding_model", DEFAULT_EMBEDDING_MODEL)}


def _validate_embedding_model(value: Any) -> str:
    """埋め込みモデル名の最小検証。HF「org/model」形式は通し、空・パス/URL混入のみ拒否。"""
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=422,
            detail="embedding_model は空でない文字列である必要があります",
        )
    v = value.strip()
    if len(v) > 256:
        raise HTTPException(
            status_code=422,
            detail="embedding_model が長すぎます（最大 256 文字）",
        )
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in v):
        raise HTTPException(
            status_code=422,
            detail="embedding_model に空白・制御文字は使えません",
        )
    if "://" in v or "\\" in v or ".." in v or v.startswith("/") or v.startswith("."):
        raise HTTPException(
            status_code=422,
            detail="embedding_model にパス・URL は使えません（'org/model' 形式で指定）",
        )
    return v


@router.patch("/settings/embedding-model")
async def update_embedding_model(body: dict):
    registry = _get_registry()
    if "embedding_model" not in body:
        raise HTTPException(
            status_code=400,
            detail="embedding_model が必要です。例: {'embedding_model': "
            "'sentence-transformers/all-MiniLM-L6-v2'}",
        )
    model = _validate_embedding_model(body["embedding_model"])
    await registry.set_embedding_model(model)
    data = await registry._read()
    return {"embedding_model": data.get("embedding_model", DEFAULT_EMBEDDING_MODEL)}


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
    # metrics の fan-out を避ける: 上流への list_tools() は呼ばず、キャッシュ済みカウントを使う
    try:
        total_tools = sum(getattr(pm, "_tool_counts", {}).values())
    except Exception:
        total_tools = 0

    return {
        "uptime_seconds": round(uptime, 1),
        "servers_registered": len(servers),
        "servers_active": len(pm._proxies),
        "total_tools": total_tools,
        "tool_calls_total": app_state.tool_calls_total,
        "tool_call_errors": app_state.tool_call_errors,
    }


@router.get("/logs")
async def get_logs(
    type: str | None = None,
    server: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 100,
):
    """ツールログ一覧（新しい順）。サーバー側フィルタ付き。"""
    limit = max(1, min(limit, 500))
    entries = app_state.snapshot_logs()
    filtered = [
        e for e in entries
        if (type is None or e.type == type)
        and (server is None or e.server == server)
        and (status is None or e.status == status)
        and (q is None or q in (e.tool or "") or (e.args and q in e.args) or (e.error and q in e.error))
    ]
    total = len(filtered)
    # 新しい順（id 降順）
    filtered.sort(key=lambda e: e.id, reverse=True)
    return {
        "entries": [e.to_dict() for e in filtered[:limit]],
        "total": total,
    }


@router.get("/servers")
async def list_servers(include_tools: bool = False):
    """List all servers. include_tools=True (default) returns tool names (backward compat).

    Set include_tools=false for fast listing without per-server network calls.
    """
    registry = _get_registry()
    pm = _get_proxy_manager()

    servers = await registry.list_servers()
    status_map = pm.get_all_status()

    if include_tools:
        tools_map = await pm.list_tools()
    else:
        tools_map = {name: [] for name in [s["name"] for s in servers]}

    result = []
    for srv in servers:
        name = srv["name"]
        config = srv["config"]
        tools = tools_map.get(name, [])
        # Fall back to cached tool count for servers not in _proxies (e.g. errored/connecting)
        cached_count = pm._tool_counts.get(name, 0)
        effective_count = len(tools) if tools else cached_count
        info = {
            "name": name,
            "config": config,
            "disabled": config.get("disabled", False),
            "status": status_map.get(name, "unknown"),
            "tools_count": effective_count,
            "tools": tools,
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

    existing = await registry.get_server(body.name)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Server '{body.name}' already exists",
        )

    config = body.config.model_dump_for_config()
    try:
        config = validate_server_config(body.name, config)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    try:
        result = await pm.register_server(body.name, config)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Failed to register server %s", body.name)
        raise HTTPException(status_code=500, detail=str(e)) from e

    return {
        "name": body.name,
        "config": config,
        "status": result["status"],
    }


@router.patch("/servers/{name}")
async def patch_server(name: str, body: PatchServerRequest):
    """サーバー設定の部分更新（PATCH）。exclude_unset で送信フィールドのみ適用。

    body.name が指定され旧名と異なる場合はサーバー名のリネームも行う。
    """
    registry = _get_registry()
    pm = _get_proxy_manager()

    existing = await registry.get_server(name)
    if not existing:
        raise HTTPException(status_code=404, detail="Server not found")

    new_name = body.name
    rename = new_name is not None and new_name != name
    target_name: str | None = None

    if rename:
        assert new_name is not None  # rename 条件より非 None
        try:
            target_name = validate_server_name(new_name)
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    # 部分更新: 送信されたフィールドのみ既存 config にマージ
    updates = body.model_dump(exclude_unset=True)
    updates.pop("name", None)  # config への name 混入防止
    merged_config = existing["config"] | updates

    # Partial validation for PATCH (may not have both url/command)
    if "url" in merged_config and merged_config["url"]:
        try:
            merged_config["url"] = validate_url(merged_config["url"])
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
    if "command" in merged_config and merged_config["command"]:
        try:
            merged_config["command"] = validate_command(merged_config["command"])
            if "args" in merged_config:
                merged_config["args"] = validate_args(merged_config["args"])
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
    # env は URL サーバーでも検証する (BLOCKED 拒否。bearer 導出用に URL 側も env を持つため)
    if "env" in merged_config:
        try:
            merged_config["env"] = validate_env(merged_config["env"])
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
    if "headers" in merged_config and merged_config["headers"]:
        try:
            merged_config["headers"] = validate_headers(merged_config["headers"])
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
    if "tags" in merged_config:
        tags = merged_config["tags"]
        if not isinstance(tags, list):
            raise HTTPException(status_code=422, detail="Tags must be a list")
        for tag in tags:
            if not isinstance(tag, str) or len(tag) > 64:
                raise HTTPException(status_code=422, detail=f"Invalid tag: {tag}")

    if rename:
        assert target_name is not None
        ok = await registry.rename_server(name, target_name)
        if not ok:
            raise HTTPException(status_code=409, detail="Server already exists")
        try:
            await pm.rename_server(name, target_name, merged_config)
        except Exception as e:
            try:
                await registry.rename_server(target_name, name)
            except Exception:
                logger.critical(
                    "rename rollback failed for %s -> %s: store is authoritative; "
                    "will self-heal on restart",
                    name, target_name,
                )
            raise HTTPException(status_code=500, detail="Failed to rename server") from e
        if updates:
            await pm.refresh_server(target_name, merged_config)
        return {
            "name": target_name,
            "config": merged_config,
        }

    # 恒久化（リネームなしの従来 PATCH）
    await registry.update_server(name, merged_config)
    # tags のみの更新はプロキシ再生成が不要（サブプロセス再起動を防ぐ）
    if set(updates) <= {"tags"}:
        await pm.update_config_only(name, merged_config)
    else:
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
        tools = await pm.list_tools_for_server(name, proxy)
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


@router.get("/servers/{name}/resources")
async def list_server_resources(name: str):
    """List resources for a connected server."""
    pm = _get_proxy_manager()
    proxy = pm.get_proxy(name)
    if proxy is None:
        raise HTTPException(404, detail=f"Server {name!r} not found or not connected")
    try:
        resources = await proxy.list_resources()
        return {"resources": [
            {"uri": str(r.uri), "name": r.name, "description": r.description or ""}
            for r in resources
        ]}
    except Exception:
        raise HTTPException(502, detail=f"Failed to list resources from {name!r}")


@router.get("/servers/{name}/prompts")
async def list_server_prompts(name: str):
    """List prompts for a connected server."""
    pm = _get_proxy_manager()
    proxy = pm.get_proxy(name)
    if proxy is None:
        raise HTTPException(404, detail=f"Server {name!r} not found or not connected")
    try:
        prompts = await proxy.list_prompts()
        return {"prompts": [
            {"name": p.name, "description": p.description or ""}
            for p in prompts
        ]}
    except Exception:
        raise HTTPException(502, detail=f"Failed to list prompts from {name!r}")


@router.get("/servers/{name}/resource-templates")
async def list_server_resource_templates(name: str):
    """List resource templates for a connected server."""
    pm = _get_proxy_manager()
    proxy = pm.get_proxy(name)
    if proxy is None:
        raise HTTPException(404, detail=f"Server {name!r} not found or not connected")
    try:
        templates = await proxy.list_resource_templates()
        return {"resource_templates": [
            {"uriTemplate": str(rt.uri_template), "name": rt.name, "description": rt.description or ""}
            for rt in templates
        ]}
    except Exception:
        raise HTTPException(502, detail=f"Failed to list resource templates from {name!r}")


@router.post("/tools/install")
async def install_dependency(body: InstallRequest):
    """Install packages via pip/uv/npm only (no shell).

    Request: {"manager": "pip"|"uv"|"npm", "packages": [...]}.
    pip/uv are pinned to --target <EXTRAS_DIR>; extra flags rejected.
    """
    import os

    extra = getattr(body, "model_extra", None) or {}
    if "command" in extra:
        raise HTTPException(
            400,
            detail="旧形式 {command: str} は廃止。{manager: pip|uv|npm, packages: string[]} で送ってください",
        )

    manager: str | None = body.manager
    if manager is None or body.packages is None:
        raise HTTPException(422, detail="manager と packages は必須です")
    if manager not in _ALLOWED_MANAGERS:
        raise HTTPException(400, detail=f"manager は {list(_ALLOWED_MANAGERS)} のいずれかである必要があります")

    packages = body.packages
    if not isinstance(packages, list) or not packages:
        raise HTTPException(400, detail="packages は1件以上の文字列リストである必要があります")

    pattern = _NPM_PKG_RE if manager == "npm" else _PIP_PKG_RE
    for pkg in packages:
        if not isinstance(pkg, str) or not pkg or pkg.startswith("-") or not pattern.match(pkg):
            raise HTTPException(400, detail=f"Invalid package: {pkg!r}")

    if manager == "pip":
        argv = ["pip", "install", "--target", _EXTRAS_DIR, *packages]
    elif manager == "uv":
        argv = ["uv", "pip", "install", "--target", _EXTRAS_DIR, *packages]
    else:
        argv = ["npm", "install", *packages]

    # Ensure EXTRAS_DIR exists
    os.makedirs(_EXTRAS_DIR, exist_ok=True)

    try:
        async with _install_sem:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=120.0
                )
            except asyncio.TimeoutError:
                try:
                    process.kill()
                finally:
                    await process.wait()
                raise HTTPException(504, detail="Install command timed out (120s)")

        return {
            "success": process.returncode == 0,
            "returncode": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Install command failed")
        raise HTTPException(500, detail=str(e))


@router.post("/servers/{name}/tools/{tool_name}/call")
async def call_tool(name: str, tool_name: str, body: CallToolRequest):
    pm = _get_proxy_manager()
    await app_state.inc_tool_calls()
    try:
        result = await pm.call_tool(name, tool_name, body.arguments)
        return {"result": result}
    except ValueError as e:
        await app_state.inc_tool_call_errors()
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        await app_state.inc_tool_call_errors()
        logger.exception("Tool call failed %s/%s", name, tool_name)
        raise HTTPException(status_code=500, detail=str(e)) from e
