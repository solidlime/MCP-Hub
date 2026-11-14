"""
管理 REST API。
プレフィックス: /admin/api
"""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from .config import DEFAULT_EMBEDDING_MODEL
from .masking import mask_text
from .meta_provider import ALL_KINDS, KIND_TOOL, resolve_kind
from .reload import ReloadError, reload_runtime
from .schema_diff import DIFF_MAX
from .state import app_state
from .tool_cache import normalize_config as _normalize_tool_cache
from .validators import (
    MAX_DESCRIPTION_LENGTH,
    ValidationError,
    validate_args,
    validate_command,
    validate_env,
    validate_headers,
    validate_limits,
    validate_server_config,
    validate_server_name,
    validate_url,
)

logger = logging.getLogger(__name__)

_ALLOWED_MANAGERS: tuple[str, ...] = ("pip", "uv", "uv-tool", "npm")
_PIP_PKG_RE = re.compile(r"^[A-Za-z0-9_.\-]+(\[[A-Za-z0-9_.\-,]+\])?(==[A-Za-z0-9_.\-*+]+)?$")
# pip/uv(-tool) の緩和パターン: PEP 508 バージョン範囲と URL 仕様。
# 許可リスト方式 — 空白・制御文字・先頭 '-'/'.'・シェルメタ文字は
# _PKG_BAD_RE 側で拒否（フラグ注入・パストラバーサル対策）。
_PIP_RANGE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.\-]*"
    r"(?:\[[A-Za-z0-9_.\-,]+\])?"
    r"(?:[<>=!~]=?[A-Za-z0-9_.\-*+!,]+)*$"
)
_PIP_URL_RE = re.compile(r"^(?:git\+)?https://[A-Za-z0-9._~%!&+,=:@/?#-]+$")
_PKG_BAD_RE = re.compile(r"[\s\x00-\x1f\x7f;|`$\\\"']")
_NPM_PKG_RE = re.compile(r"^(@[A-Za-z0-9_.\-~]+\/)?[A-Za-z0-9_.\-~]+(@[A-Za-z0-9_.\-~^]+)?$")
# constraints（依存ピン）用: 名前 + 範囲指定子。バージョン指定子
# < > ! = ~ , ( ) は許可（PEP 508/440）。空白・制御文字・シェルメタ・
# '/' は _PKG_BAD_RE とキャラクラス外で拒否（先頭 '-'/'.' も別途弾く）。
_CONSTRAINT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.\-]*"
    r"(?:\[[A-Za-z0-9_.\-,]+\])?"
    r"[<>=!~(),.\-*+A-Za-z0-9]*$"
)
_EXTRAS_DIR = "/home/mcp-hub/pip-extras"
_INSTALL_TIMEOUT = 300.0
_install_sem = asyncio.Semaphore(1)

# --- 供給網保護（#8） ---
# MCP_HUB_INSTALL_ALLOWED_HOSTS（カンマ区切り）設定時は、URL 仕様の取得元
# ホストをこのリストに限定する。未設定は従来通り任意の https ホストを許可
# （後方互換。docs/security.md に制限として明記）。
_ALLOWED_HOSTS_ENV = "MCP_HUB_INSTALL_ALLOWED_HOSTS"
_OUTPUT_MAX_LEN = 8000


def _install_allowed_hosts() -> tuple[str, ...]:
    raw = os.environ.get(_ALLOWED_HOSTS_ENV, "")
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def _https_env_url(env_name: str, default: str | None) -> str | None:
    """レジストリ URL を返す。https 以外は警告して無視（既定に任せる）。"""
    url = os.environ.get(env_name) or default
    if url is None:
        return None
    if not url.startswith("https://"):
        logger.warning("insecure %s %r ignored", env_name, url)
        return None
    return url


def _spec_host(spec: str) -> str | None:
    """パッケージ仕様が URL なら取得元ホストを返す（名前仕様は None）。"""
    if spec.startswith("git+"):
        spec = spec[len("git+"):]
    if not spec.startswith("https://"):
        return None
    return (urlsplit(spec).hostname or "").lower()


def _audit(event: str, **fields) -> None:
    """install/uninstall の監査ログ（JSON 1 行、grep しやすい固定形式）。"""
    logger.info(
        "[audit] %s",
        json.dumps({"event": event, **fields}, ensure_ascii=False, sort_keys=True),
    )

# --- 事前ソーススキャン（git+URL → mcp<2 自動ピン） ---
# mcp 2.x は mcp.server.fastmcp を削除したため、1.x API
# (from mcp.server.fastmcp import FastMCP) で書かれたサーバーは
# 解決で mcp>=2 を拾うと起動即死する。git+URL ソースを shallow
# clone して import を検出したら、明示指定がなければ mcp<2 を自動適用する。
_FASTMCP_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+mcp\.server\.fastmcp\s+import|import\s+mcp\.server\.fastmcp)\b",
    re.MULTILINE,
)
_SCAN_CLONE_TIMEOUT = 60.0
_SCAN_MAX_FILES = 2000
_SCAN_MAX_BYTES = 2_000_000


# --- Schemas ---


class ServerConfig(BaseModel):
    url: str | None = None
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}  # ← 追加: BRAVE_API_KEY 等
    tags: list[str] = []
    headers: dict[str, str] = {}
    disabled: bool = False
    description: str | None = None
    # ツール名 → 日本語1文（80字以内目安）。索引テキストに前置され意味検索の
    # 語彙の壁を越える。表示用ではない（PATCH /servers/{name} の部分更新に乗る）。
    tool_search_desc: dict[str, str] = {}
    # サーバー単位の呼び出し制限（timeout / 同時実行 / レート）。
    # 未指定キーはグローバル既定にフォールバックする。
    limits: dict[str, float | int] | None = None

    def model_dump_for_config(self) -> dict:
        """空文字・空リストを除外した config dict を返す。"""
        raw = self.model_dump(exclude_none=True)
        for key in (
            "url",
            "command",
            "args",
            "env",
            "tags",
            "headers",
            "description",
            "tool_search_desc",
            "limits",
        ):
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
    # 依存ピン（例: "mcp<2"）。install のみ。npm / uninstall は 400。
    constraints: list[str] | None = None


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
        ) from None
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


def _effective_embedding_status() -> str:
    """ライブ index の埋め込み実効状態（A3）。

    "active"/"building"/"inactive:*"/"error:*" を返す。meta_app 未初期化で
    index が無い場合は "inactive:setting"（埋め込みは動作し得ない）。
    """
    index = getattr(getattr(app_state, "meta_app", None), "index", None)
    if index is None:
        return "inactive:setting"
    return str(index.embedding_status)


# 埋め込み設定変更で走る再構築タスク（最新1本だけ保持し、置き換え時に cancel）。
_embedding_rebuild_task: asyncio.Task | None = None


def _embedding_rebuild_task_ref() -> asyncio.Task | None:
    """実行中の埋め込み索引再構築タスク（シャットダウン時の停止用）。"""
    return _embedding_rebuild_task


def _embedding_rebuild_snapshot() -> dict[str, Any]:
    """再構築の進捗スナップショット（要求値と実効値を分けて返す）。

    UI は requested_model（保存済み設定）と active_model（索引に適用済み）の
    差で「再構築中 / 未反映」を判定できる。
    """
    state = app_state.embedding_rebuild
    index = getattr(getattr(app_state, "meta_app", None), "index", None)
    return {
        **state.to_dict(),
        "in_progress": state.status == "running",
        "active_model": getattr(index, "embedding_model", None),
        "documents": getattr(index, "document_count", 0),
        "embedding_status": _effective_embedding_status(),
    }


async def _run_embedding_rebuild(meta_app: Any) -> None:
    """1 回分の再構築を実行し、結果を app_state.embedding_rebuild に反映する。

    再構築失敗は致命的ではない: ToolIndex は BM25 検索で動き続ける
    （旧モデルのベクトルは set_embedding_model で破棄済みなのでモデル混在の
    誤ヒットは起きない）。
    """
    current = asyncio.current_task()
    state = app_state.embedding_rebuild

    def _update(**changes: Any) -> None:
        # 後続の再構築に置き換えられた（cancel 済み）なら状態を触らない。
        if _embedding_rebuild_task is current:
            for key, value in changes.items():
                setattr(state, key, value)

    try:
        failed = await meta_app.rebuild_index()
        index = getattr(meta_app, "index", None)
        error = getattr(index, "embedding_error", None)
        if error:
            _update(status="error", error=str(error))
        elif failed:
            _update(status="error", error=f"ツール取得に失敗: {', '.join(failed)}")
        else:
            _update(status="success", error=None)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — 失敗は status に残す（応答は既に返却済）
        logger.exception("Embedding index rebuild failed")
        _update(status="error", error=f"{type(exc).__name__}: {str(exc)[:160]}")
    finally:
        _update(finished_at=time.time())


def _schedule_embedding_rebuild(
    trigger: str, meta_app: Any, requested_model: str | None = None
) -> dict[str, Any]:
    """索引再構築をバックグラウンドで開始する（応答はブロックしない）。

    実行中の再構築があればキャンセルして置き換える（設定の最終値だけが意味を持つ）。
    """
    global _embedding_rebuild_task
    previous = _embedding_rebuild_task
    if previous is not None and not previous.done():
        previous.cancel()
    state = app_state.embedding_rebuild
    state.status = "running"
    state.trigger = trigger
    state.started_at = time.time()
    state.finished_at = None
    state.error = None
    if requested_model is not None:
        state.requested_model = requested_model
    _embedding_rebuild_task = asyncio.create_task(_run_embedding_rebuild(meta_app))
    return _embedding_rebuild_snapshot()


def _tool_cache_public(data: dict) -> dict:
    """GET/PATCH /settings 用: tool_cache 設定（未設定は既定値）+ 実効メトリクス。"""
    try:
        cfg = _normalize_tool_cache(data.get("tool_cache"))
    except ValueError:
        cfg = _normalize_tool_cache(None)  # 壊れた設定は既定値として公開
    return {**cfg, "stats": app_state.tool_cache.stats()}


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
        "embedding_status": _effective_embedding_status(),
        "embedding_rebuild": _embedding_rebuild_snapshot(),
        "llm": _llm_public(data.get("llm")),
        "tool_cache": _tool_cache_public(data),
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
            # 検索インデックスを非同期で再構築（embed/on 切替で doc テキストが変わる）。
            # 応答を待たせない: 進捗は GET /settings の embedding_rebuild で見る。
            meta_app.index.set_use_embeddings(value)
            _schedule_embedding_rebuild("use_embeddings", meta_app)
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
    if "llm" in body:
        value = body["llm"]
        if not isinstance(value, dict):
            raise HTTPException(
                status_code=422, detail="llm はオブジェクトである必要があります"
            )
        # 部分更新: 既存とマージする（api_key を毎回送らせない）。空 dict で機能オフ。
        if not value:
            await registry.set_llm({})
        else:
            data = await registry._read()
            current = data.get("llm")
            merged = (current if isinstance(current, dict) else {}) | value
            await registry.set_llm(merged)
    if "tool_cache" in body:
        value = body["tool_cache"]
        if not isinstance(value, dict):
            raise HTTPException(
                status_code=422, detail="tool_cache はオブジェクトである必要があります"
            )
        data = await registry._read()
        current = data.get("tool_cache")
        merged = (current if isinstance(current, dict) else {}) | value
        try:
            cfg = _normalize_tool_cache(merged)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
        await registry.set_tool_cache(cfg)
        # 設定変更（TTL/allow/deny/enabled）は既存エントリの意味を変えるので全破棄
        app_state.tool_cache.clear()
        app_state.tool_cache.configure(cfg)
    data = await registry._read()
    return {
        "meta_mode": data.get("meta_mode", False),
        "full_info_tools": data.get("full_info_tools", []),
        "client_timeout": data.get("client_timeout"),
        "connect_timeout": data.get("connect_timeout"),
        "use_embeddings": _effective_use_embeddings(),
        "embedding_status": _effective_embedding_status(),
        "embedding_rebuild": _embedding_rebuild_snapshot(),
        "llm": _llm_public(data.get("llm")),
        "tool_cache": _tool_cache_public(data),
    }


@router.delete("/cache")
async def clear_tool_cache_entries():
    """結果キャッシュの全エントリを破棄する（設定とヒット/ミス統計は保持）。"""
    cleared = app_state.tool_cache.clear()
    return {"cleared": cleared, "stats": app_state.tool_cache.stats()}


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
    if "://" in v or "\\" in v or ".." in v or v.startswith(("/", ".")):
        raise HTTPException(
            status_code=422,
            detail="embedding_model にパス・URL は使えません（'org/model' 形式で指定）",
        )
    return v


@router.patch("/settings/embedding-model")
async def update_embedding_model(body: dict):
    """モデルを保存し、バックグラウンドで索引を再構築する（再起動待ちなし）。

    応答は受け付け状態のみ（rebuild.status="running"）。進捗・成功/失敗は
    GET /settings の embedding_rebuild で追跡する。
    """
    registry = _get_registry()
    if "embedding_model" not in body:
        raise HTTPException(
            status_code=400,
            detail="embedding_model が必要です。例: {'embedding_model': "
            "'cl-nagoya/ruri-v3-30m'}",
        )
    model = _validate_embedding_model(body["embedding_model"])
    await registry.set_embedding_model(model)
    meta_app = getattr(app_state, "meta_app", None)
    rebuild: dict[str, Any] | None = None
    if meta_app is not None:
        # 旧モデルのベクトル/embedder を捨ててから再構築（混在は誤ヒットの元）。
        meta_app.index.set_embedding_model(model)
        rebuild = _schedule_embedding_rebuild("embedding_model", meta_app, model)
    data = await registry._read()
    return {
        "embedding_model": data.get("embedding_model", DEFAULT_EMBEDDING_MODEL),
        "embedding_status": _effective_embedding_status(),
        "rebuild": rebuild or _embedding_rebuild_snapshot(),
    }


# --- LLM による説明文生成（/llm/generate） ---

# OpenAI 互換エンドポイントの既定。provider で base_url を選べる（不明な provider は
# base_url を明示しない限り OpenAI にフォールバック）。
_LLM_BASE_URLS: dict[str, str] = {"openai": "https://api.openai.com/v1"}
_LLM_DEFAULT_PROVIDER = "openai"
_LLM_DEFAULT_MODEL = "gpt-4o-mini"
_LLM_TIMEOUT = 30.0
# server_bundle はサーバー説明＋最大 _LLM_MAX_BUNDLE_TOOLS 件のツール説明を
# 1 応答で返させるため出力が大きい。既存 kind の 30s では足りないことがあるので
# この kind だけ余裕を持たせる（既存 kind の値は変えない）。
_LLM_BUNDLE_TIMEOUT = 60.0
# 1 回のまとめ生成で扱うツール数の上限。プロンプト／出力の肥大を防ぐ。
_LLM_MAX_BUNDLE_TOOLS = 30
_LLM_KINDS = ("server_description", "tool_search_desc", "server_bundle")


class LLMGenerateRequest(BaseModel):
    kind: str
    server: str
    tool_name: str | None = None
    # server_bundle の生成対象ツール名（空欄のみを渡す想定）。
    tools: list[str] = []


def _llm_public(llm: Any) -> dict:
    """GET /settings 用: api_key の値は返さず、設定有無のみを公開する。"""
    llm = llm if isinstance(llm, dict) else {}
    return {
        "provider": llm.get("provider"),
        "model": llm.get("model"),
        "base_url": llm.get("base_url"),
        "api_key_set": bool(llm.get("api_key")),
    }


def _llm_prompt(
    kind: str,
    *,
    server: str,
    server_desc: str,
    tool_name: str | None,
    tool_desc: str,
    tools: list[dict[str, Any]],
) -> str:
    """kind ごとの生成プロンプト。出力は日本語1文・80字以内を指示する。

    プロンプトはサーバー側に置く（フロントには材料だけ渡す）。tool_search_desc は
    ツールの docstring とサーバー説明を材料にし、検索ヒットしやすい語を含めさせる。
    """
    if kind == "server_description":
        lines = [
            f"- {t.get('name', '')}: {(t.get('description') or '').strip()[:200]}"
            for t in tools
        ]
        material = "\n".join(lines) or "(ツール情報なし)"
        return (
            f"次の MCP サーバー '{server}' が提供する機能を、日本語の1文（80文字以内）で"
            "説明してください。日本語で検索したときに見つけやすいよう、用途・対象・代表的な"
            "操作語を含めてください。説明文だけを出力し、前後の引用符・見出し・箇条書きは"
            "付けないでください。\n\n"
            f"サーバー名: {server}\nツール一覧:\n{material}"
        )
    if kind == "server_bundle":
        lines = [
            f"- {t.get('name', '')}: {(t.get('description') or '').strip()[:200]}"
            for t in tools
        ]
        material = "\n".join(lines) or "(ツール情報なし)"
        return (
            f"次の MCP サーバー '{server}' について、次の2つを含む JSON オブジェクトを"
            "1つだけ出力してください。\n"
            "- server_description: サーバーが提供する機能の日本語1文（80文字以内）。"
            "日本語で検索したときに見つけやすいよう、用途・対象・代表的な操作語を含める。\n"
            "- tools: 対象ツールごとの検索用の日本語1文（80文字以内）のオブジェクト。"
            "キーはツール名、値は説明。\n\n"
            "出力は次の形式の JSON のみとし、前後に説明・見出しを付けないでください。\n"
            '{"server_description": "...", "tools": {"ツール名": "..."}}\n\n'
            f"サーバー名: {server}\n対象ツール:\n{material}"
        )
    return (
        f"次の MCP ツール '{tool_name}'（サーバー '{server}'）を、日本語の1文"
        "（80文字以内）で要約してください。ユーザーが日本語で検索したときにヒットしやすい"
        "語（用途・対象・同義語）を含めてください。要約だけを出力し、前後の引用符・見出しは"
        "付けないでください。\n\n"
        f"サーバー説明: {server_desc or '(なし)'}\n"
        f"ツール説明: {tool_desc or '(なし)'}"
    )


def _strip_json_fence(text: str) -> str:
    """LLM 応答からマークダウンのコードフェンスを剥がす（無ければそのまま）。

    ```json ... ``` / ``` ... ``` のどちらにも対応する。
    """
    s = text.strip()
    match = re.match(r"^```[A-Za-z0-9_-]*[ \t]*\r?\n(.*?)```[ \t]*$", s, re.DOTALL)
    return match.group(1).strip() if match else s


def _parse_bundle_response(text: str) -> dict[str, Any]:
    """server_bundle 応答の JSON をパースする（失敗は ValueError）。

    実 LLM は前後に前置き文・コードフェンス・締めの一文を付けることがあるため、
    寛容に 3 段階で試す: (1) 生のまま、(2) コードフェンス除去後、(3) 最初の
    `{` から最後の `}` までを切り出した部分。server_description（空でない文字列）
    と tools（オブジェクト）を要求する。tools の値検証・要求集合への絞り込みは
    呼び出し側の責務。
    """
    stripped = _strip_json_fence(text)
    candidates = [text, stripped]
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])

    data: Any = None
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            break
        except (ValueError, TypeError):
            continue
    else:
        raise ValueError("LLM の応答を JSON として解析できませんでした")
    if not isinstance(data, dict):
        raise ValueError("LLM の応答が JSON オブジェクトではありません")
    desc = data.get("server_description")
    tools = data.get("tools")
    if not isinstance(desc, str) or not desc.strip():
        raise ValueError("server_description がありません")
    if not isinstance(tools, dict):
        raise ValueError("tools がオブジェクトではありません")
    return {"description": desc.strip(), "tools": tools}


@router.post("/llm/generate")
async def llm_generate(body: LLMGenerateRequest):
    """OpenAI 互換 API で日本語の説明文（1文）を生成する。

    api_key 未設定は 400（フロントは ✨ を出さない設計）。kind は
    server_description / tool_search_desc / server_bundle のいずれか。
    """
    registry = _get_registry()
    data = await registry._read()
    llm_raw = data.get("llm")
    llm: dict[str, Any] = llm_raw if isinstance(llm_raw, dict) else {}
    api_key = llm.get("api_key")
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="llm.api_key が未設定のため生成できません",
        )
    if body.kind not in _LLM_KINDS:
        raise HTTPException(
            status_code=422,
            detail="kind は server_description / tool_search_desc / server_bundle を指定してください",
        )
    if body.kind == "server_bundle" and len(body.tools) > _LLM_MAX_BUNDLE_TOOLS:
        raise HTTPException(
            status_code=400,
            detail=f"tools は最大 {_LLM_MAX_BUNDLE_TOOLS} 件までです",
        )

    pm = _get_proxy_manager()
    server = body.server
    desc_fn = getattr(pm, "server_description", None)
    raw_server_desc = desc_fn(server) if callable(desc_fn) else ""
    server_desc = raw_server_desc if isinstance(raw_server_desc, str) else ""

    proxy = pm.get_proxy(server)
    if proxy is None:
        raise HTTPException(
            status_code=404, detail=f"Server '{server}' は接続されていません"
        )
    raw_tools = await pm.list_tools_for_server(server, proxy)
    tools = [
        {"name": t.name, "description": (t.description or "")} for t in raw_tools
    ]

    tool_desc = ""
    if body.kind == "tool_search_desc":
        if not body.tool_name:
            raise HTTPException(
                status_code=400,
                detail="tool_search_desc には tool_name が必要です",
            )
        match = [t for t in tools if t["name"] == body.tool_name]
        if not match:
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{body.tool_name}' は '{server}' に見つかりません",
            )
        tool_desc = match[0]["description"].strip()

    # server_bundle は対象ツール（空欄のみ＝リクエストの tools）だけを材料にする。
    prompt_tools = tools
    if body.kind == "server_bundle":
        requested = set(body.tools)
        prompt_tools = [t for t in tools if t["name"] in requested]

    prompt = _llm_prompt(
        body.kind,
        server=server,
        server_desc=server_desc,
        tool_name=body.tool_name,
        tool_desc=tool_desc,
        tools=prompt_tools,
    )

    provider = str(llm.get("provider") or _LLM_DEFAULT_PROVIDER)
    base_url = str(
        llm.get("base_url")
        or _LLM_BASE_URLS.get(provider)
        or _LLM_BASE_URLS[_LLM_DEFAULT_PROVIDER]
    ).rstrip("/")
    model = str(llm.get("model") or _LLM_DEFAULT_MODEL)

    # httpx は mcp の推移依存（主依存）。admin 起動を重くしないよう遅延 import。
    import httpx

    timeout = _LLM_BUNDLE_TIMEOUT if body.kind == "server_bundle" else _LLM_TIMEOUT
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                },
            )
    except httpx.HTTPError as e:
        logger.warning("LLM request failed: %s", mask_text(str(e)))
        raise HTTPException(
            status_code=502, detail="LLM への接続に失敗しました"
        ) from e

    if resp.status_code >= 400:
        logger.warning("LLM upstream error HTTP %s", resp.status_code)
        raise HTTPException(
            status_code=502,
            detail=f"LLM がエラーを返しました (HTTP {resp.status_code})",
        )
    try:
        text = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise HTTPException(
            status_code=502, detail="LLM の応答を解析できませんでした"
        ) from e
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=502, detail="LLM が空の応答を返しました")
    if body.kind == "server_bundle":
        try:
            parsed = _parse_bundle_response(text)
        except ValueError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        requested = set(body.tools)
        # 指定外ツール・非文字列・空白のみは返さない（幻覚の混入を防ぐ）。
        tool_search_desc = {
            k: v.strip()
            for k, v in parsed["tools"].items()
            if k in requested and isinstance(v, str) and v.strip()
        }
        return {
            "description": parsed["description"],
            "tool_search_desc": tool_search_desc,
        }
    return {"text": text.strip()}


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
        "embedding_status": _effective_embedding_status(),
    }


@router.post("/reload")
async def reload_config():
    """hub.config.json を読み直して差分適用する（再起動不要）。

    不正な設定ファイルは 500 で弾く（ランタイムは変更前のまま）。
    一部サーバーの再接続に失敗しても、そのサーバーが error 状態になるだけで
    全体は稼働を続ける（部分的に適用済みの状態を ok/errors で返す）。
    """
    try:
        return await reload_runtime()
    except ReloadError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


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
        "tool_cache": app_state.tool_cache.stats(),
    }


@router.get("/schema-diffs")
async def schema_diffs(server: str | None = None, limit: int = 50):
    """スキーマ差分の履歴（新しい順）。no-op な refresh は記録されない。"""
    limit = max(1, min(limit, DIFF_MAX))
    entries = [e.to_dict() for e in app_state.snapshot_diffs()]
    if server:
        entries = [e for e in entries if e["server"] == server]
    entries.reverse()
    return {"entries": entries[:limit], "total": len(entries)}


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
    """List all servers. include_tools=False (default) returns a fast listing without per-server network calls.

    Set include_tools=true to include tool names (one network call per server).
    """
    registry = _get_registry()
    pm = _get_proxy_manager()

    servers = await registry.list_servers()

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
            "tools_count": effective_count,
            "tools": tools,
        }
        # 接続状態（effective_state / 連続失敗 / 直近エラー / 次回リトライ / 件数 / 履歴）
        info.update(pm.get_connection_state(name))
        if info["tool_count"] == 0:
            info["tool_count"] = effective_count
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
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
    # args は command の有無に関わらず必ず検証する（URL サーバーへの args PATCH も対象）
    if "args" in merged_config:
        try:
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
    if "limits" in merged_config:
        if merged_config["limits"] is None:
            merged_config.pop("limits")
        else:
            try:
                merged_config["limits"] = validate_limits(merged_config["limits"])
            except ValidationError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e
    if merged_config.get("description") is not None:
        desc = merged_config["description"]
        if not isinstance(desc, str):
            raise HTTPException(status_code=422, detail="Description must be a string")
        if len(desc) > MAX_DESCRIPTION_LENGTH:
            raise HTTPException(
                status_code=422,
                detail=f"Description too long (max {MAX_DESCRIPTION_LENGTH} chars)",
            )
    if "tool_search_desc" in merged_config:
        tsd = merged_config["tool_search_desc"]
        if not isinstance(tsd, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in tsd.items()
        ):
            raise HTTPException(
                status_code=422,
                detail="tool_search_desc は {ツール名: 文字列} のオブジェクトである必要があります",
            )

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
    # tags / description / tool_search_desc のみの更新はプロキシ再生成が不要
    # （サブプロセス再起動を防ぐ）。update_config_only は tags_updated 通知で
    # 索引再構築を起こすため、tool_search_desc も即座に検索へ反映される。
    # limits のみの更新もプロキシ再生成が不要（制限は呼び出し毎に参照される）
    if set(updates) <= {"limits"}:
        await pm.update_limits(name, merged_config)
    elif set(updates) <= {"tags", "description", "tool_search_desc", "limits"}:
        await pm.update_config_only(name, merged_config)
    else:
        await pm.refresh_server(name, merged_config)

    return {
        "name": name,
        "config": merged_config,
    }


@router.patch("/servers/{name}/limits")
async def patch_server_limits(name: str, body: dict[str, Any]):
    """サーバー単位の呼び出し制限を部分更新する（null でキー削除）。

    全体が空になったら config から `limits` キーごと落とす（＝全項目グローバル既定）。
    """
    registry = _get_registry()
    pm = _get_proxy_manager()

    existing = await registry.get_server(name)
    if not existing:
        raise HTTPException(status_code=404, detail="Server not found")

    current = existing["config"].get("limits")
    merged: dict[str, Any] = dict(current) if isinstance(current, dict) else {}
    merged.update(body)
    try:
        limits = validate_limits(merged)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    config = dict(existing["config"])
    if limits:
        config["limits"] = limits
    else:
        config.pop("limits", None)

    await registry.update_server(name, config)
    # 制限は呼び出し毎に参照されるためプロキシ再生成は不要。ただし
    # 既存の Semaphore / レート状態は旧設定由来なので破棄する。
    await pm.update_limits(name, config)

    return {"name": name, "limits": limits}


@router.delete("/servers/{name}", status_code=204)
async def remove_server(name: str):
    pm = _get_proxy_manager()
    ok = await pm.unregister_server(name)
    if not ok:
        raise HTTPException(status_code=404, detail="Server not found")


@router.post("/servers/{name}/test")
async def test_server(name: str):
    pm = _get_proxy_manager()
    proxy, reason = await pm.test_connect(name)
    if proxy is None:
        if reason == "not_found":
            raise HTTPException(status_code=404, detail="Server not found")
        error = {
            "disabled": "Server is disabled — enable it before testing",
            "in_progress": "Connection attempt already in progress; try again shortly",
        }.get(reason)
        if error is None:
            status = reason.split(":", 1)[1] if reason.startswith("failed:") else reason
            error = f"Connection failed (status: {status})"
        return {
            "success": False,
            "tools_count": 0,
            "tools": [],
            "error": error,
        }

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
        pm.record_catalog_counts(name, resources=len(resources))
        return {"resources": [
            {"uri": str(r.uri), "name": r.name, "description": r.description or ""}
            for r in resources
        ]}
    except Exception as e:
        raise HTTPException(502, detail=f"Failed to list resources from {name!r}") from e


@router.get("/servers/{name}/prompts")
async def list_server_prompts(name: str):
    """List prompts for a connected server."""
    pm = _get_proxy_manager()
    proxy = pm.get_proxy(name)
    if proxy is None:
        raise HTTPException(404, detail=f"Server {name!r} not found or not connected")
    try:
        prompts = await proxy.list_prompts()
        pm.record_catalog_counts(name, prompts=len(prompts))
        return {"prompts": [
            {"name": p.name, "description": p.description or ""}
            for p in prompts
        ]}
    except Exception as e:
        raise HTTPException(502, detail=f"Failed to list prompts from {name!r}") from e


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
    except Exception as e:
        raise HTTPException(502, detail=f"Failed to list resource templates from {name!r}") from e


@router.get("/discovery/search")
async def discovery_search(
    query: str,
    top_k: int = 10,
    kind: str | None = None,
    server: str | None = None,
):
    """ツール/リソース/テンプレート/プロンプトを横断検索する（管理 UI 用）。

    索引はメタ経路と同一（ToolIndex を kind で分ける）。管理 API は認証済みの
    全権限ビューなのでタグフィルタは適用しない（結果に tags を含める）。
    kind は複数形・ハイフン形の別名も受ける。
    """
    index = getattr(getattr(app_state, "meta_app", None), "index", None)
    if index is None:
        return {"results": [], "kinds": list(ALL_KINDS)}
    kinds = None
    if kind:
        resolved = resolve_kind(kind)
        if resolved is None:
            raise HTTPException(
                400,
                detail=f"unknown kind: {kind!r} (kinds: {', '.join(ALL_KINDS)})",
            )
        kinds = (resolved,)
    results = index.search(query, top_k, kinds)
    # 管理 UI は kind で表示を分けるので、ツールヒットにも kind を明示する
    # （メタ経路の応答形状は tool で kind を付けない既存契約のまま）。
    results = [{**r, "kind": r.get("kind") or KIND_TOOL} for r in results]
    if server:
        results = [r for r in results if r["server"].lower() == server.lower()]
    return {"query": query, "kinds": list(ALL_KINDS), "results": results}


def _pkg_spec_ok(manager: str, pkg: str) -> bool:
    """pkg がマネージャーの要求仕様として妥当なら True。"""
    if manager == "npm":
        return bool(_NPM_PKG_RE.match(pkg))
    return bool(
        _PIP_PKG_RE.match(pkg)
        or _PIP_RANGE_RE.match(pkg)
        or _PIP_URL_RE.match(pkg)
    )


def _validate_constraints(constraints: Any) -> list[str]:
    """constraints の安全検査（packages と同等、範囲指定子は許可）。"""
    if not isinstance(constraints, list) or not constraints:
        raise HTTPException(400, detail="constraints は1件以上の文字列リストである必要があります")
    for c in constraints:
        if (
            not isinstance(c, str)
            or not c
            or c.startswith(("-", "."))
            or _PKG_BAD_RE.search(c)
            or not _CONSTRAINT_RE.match(c)
        ):
            raise HTTPException(400, detail=f"Invalid constraint: {c!r}")
    return constraints


def _validate_manager_packages(body: InstallRequest) -> tuple[str, list[str]]:
    """install/uninstall 共通のリクエスト検証。

    旧 {command: str} 形式・未知マネージャー・フラグ注入（先頭 '-'）・
    パストラバーサル（先頭 '.' / パス区切り）・空白・制御文字・
    シェルメタ文字を拒否する。
    """
    extra = getattr(body, "model_extra", None) or {}
    if "command" in extra:
        raise HTTPException(
            400,
            detail="旧形式 {command: str} は廃止。{manager: pip|uv|uv-tool|npm, packages: string[]} で送ってください",
        )

    manager: str | None = body.manager
    if manager is None or body.packages is None:
        raise HTTPException(422, detail="manager と packages は必須です")
    if manager not in _ALLOWED_MANAGERS:
        raise HTTPException(400, detail=f"manager は {list(_ALLOWED_MANAGERS)} のいずれかである必要があります")

    packages = body.packages
    if not isinstance(packages, list) or not packages:
        raise HTTPException(400, detail="packages は1件以上の文字列リストである必要があります")

    for pkg in packages:
        if (
            not isinstance(pkg, str)
            or not pkg
            or pkg.startswith(("-", "."))
            or _PKG_BAD_RE.search(pkg)
            or not _pkg_spec_ok(manager, pkg)
        ):
            raise HTTPException(400, detail=f"Invalid package: {pkg!r}")

    allowed = _install_allowed_hosts()
    if allowed:
        for pkg in packages:
            host = _spec_host(pkg)
            if host is not None and host not in allowed:
                raise HTTPException(
                    400,
                    detail=(
                        f"Package source host {host!r} is not in the allowlist "
                        f"({list(allowed)}; set {_ALLOWED_HOSTS_ENV})"
                    ),
                )
    return manager, packages


async def _run_pkg_command(argv: list[str], op: str) -> dict:
    """パッケージマネージャーの固定 argv をシェルなしで実行する。

    asyncio.create_subprocess_exec のまま（create_subprocess_shell は使わない）。
    セマフォで直列化、_INSTALL_TIMEOUT 秒でタイムアウト。実行結果は監査ログに残し、
    stdout/stderr はマスキング + 切り詰めて返す（トークン混入・大量出力対策）。
    """
    event = f"pkg.{op.lower()}"
    started = time.monotonic()
    async with _install_sem:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            _audit(event, ok=False, manager=argv[0], error="manager not found")
            raise HTTPException(
                503,
                detail=(
                    f"{op}: パッケージマネージャー {argv[0]!r} が見つかりません"
                    "（bootstrap 未完了または PATH 未設定）"
                ),
            ) from None
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=_INSTALL_TIMEOUT
            )
        except TimeoutError:
            try:
                process.kill()
            finally:
                await process.wait()
            _audit(event, ok=False, manager=argv[0], error="timeout")
            raise HTTPException(
                504, detail=f"{op} command timed out ({int(_INSTALL_TIMEOUT)}s)"
            ) from None

    _audit(
        event,
        ok=process.returncode == 0,
        manager=argv[0],
        returncode=process.returncode,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return {
        "success": process.returncode == 0,
        "returncode": process.returncode,
        "stdout": mask_text(
            stdout.decode("utf-8", errors="replace"), max_len=_OUTPUT_MAX_LEN
        ),
        "stderr": mask_text(
            stderr.decode("utf-8", errors="replace"), max_len=_OUTPUT_MAX_LEN
        ),
    }


def _normalize_pkg_name(name: str) -> str:
    """PEP 503 正規化: 小文字化と '-_.' 連続の '-' 統一。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _remove_extras_dirs(packages: list[str]) -> list[str]:
    """_EXTRAS_DIR 配下のパッケージディレクトリを削除し、削除名を返す。

    `pip uninstall` は --target 非対応のため直接削除する。マッチは PEP 503
    正規化 + 大文字小文字無視の前方一致（<pkg> / <pkg>-*.dist-info）。
    realpath プレフィックス検証で _EXTRAS_DIR 配下に固定しており、
    バリデーションでパス区切りを弾いているためトラバーサルは不可能。
    """
    removed: list[str] = []
    if not os.path.isdir(_EXTRAS_DIR):
        return removed
    base = os.path.realpath(_EXTRAS_DIR)
    try:
        entries = os.listdir(_EXTRAS_DIR)
    except OSError:
        return removed
    for pkg in packages:
        norm = _normalize_pkg_name(pkg)
        for entry in entries:
            entry_norm = _normalize_pkg_name(entry)
            if entry_norm != norm and not entry_norm.startswith(norm + "-"):
                continue
            target = os.path.join(_EXTRAS_DIR, entry)
            real = os.path.realpath(target)
            if not real.startswith(base + os.sep):
                continue
            if os.path.islink(target) or not os.path.isdir(target):
                continue
            try:
                shutil.rmtree(target)
            except OSError:
                logger.exception("Failed to remove %s", target)
                continue
            removed.append(entry)
    return removed


def _git_clone_url(spec: str) -> str | None:
    """pip 形式の git+https 仕様から clone 用 URL を抽出する。

    `git+https://host/repo.git@v1.0#egg=x` → `https://host/repo.git`。
    ref (`@ref`) / fragment (`#...`) は除去するが、userinfo の `@`
    (user:pass@host) は壊さないため、パス区切り `/' 以降の最初の
    `@` のみを切断点とする。解釈不能なら None。
    """
    if not spec.startswith("git+"):
        return None
    url = spec[len("git+"):]
    url = url.split("#", 1)[0]
    scheme_sep = url.find("//")
    slash = url.find("/", scheme_sep + 2) if scheme_sep != -1 else -1
    at = url.find("@", slash + 1) if slash != -1 else -1
    if at != -1:
        url = url[:at]
    return url or None


def _scan_tree_for_fastmcp_import(root: str, url: str) -> bool | None:
    """同期スキャン: True 検出 / False 検出なし / None 判定不能（大きすぎ）。

    .py ファイルのみ対象。.git / node_modules / __pycache__ は除外。
    """
    checked = 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            checked += 1
            if checked > _SCAN_MAX_FILES:
                logger.warning("Source scan too large (%s), giving up", url)
                return None
            path = os.path.join(dirpath, fn)
            if os.path.islink(path):
                continue  # 悪意ある repo 内 symlink で clone 外ファイルを開かない
            try:
                with open(path, "rb") as f:
                    text = f.read(_SCAN_MAX_BYTES).decode("utf-8", errors="replace")
            except OSError:
                continue
            if _FASTMCP_IMPORT_RE.search(text):
                return True
    return False


async def _git_source_has_fastmcp_import(url: str) -> bool | None:
    """shallow clone したソースに mcp.server.fastmcp import があるか。

    True: 検出 / False: 検出なし / None: 判定不能（clone 失敗・タイム
    アウト・異常に大きいリポジトリ）。判定不能時は呼び出し側が
    通常インストールにフォールバックする（失敗させない）。
    """
    tmpdir: str | None = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="mcphub-srcscan-")
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", "--quiet", url, tmpdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=_SCAN_CLONE_TIMEOUT)
        except TimeoutError:
            try:
                proc.kill()
            finally:
                await proc.wait()
            logger.warning("Source scan clone timed out for %s", url)
            return None
        if proc.returncode != 0:
            logger.warning("Source scan clone failed (rc=%s) for %s", proc.returncode, url)
            return None

        return await asyncio.to_thread(_scan_tree_for_fastmcp_import, tmpdir, url)
    except Exception:
        logger.exception("Source scan failed for %s", url)
        return None
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


async def _detect_auto_mcp_pin(packages: list[str]) -> tuple[list[str], str | None]:
    """git+URL パッケージから mcp<2 自動ピンの要否を判定する。

    戻り値: (ピンリスト, 理由)。検出なし・判定不能は ([], None)。
    複数 git+URL のうち最初に検出された URL を理由に含める。
    """
    for spec in packages:
        if not spec.startswith("git+"):
            continue
        url = _git_clone_url(spec)
        if not url:
            continue
        if await _git_source_has_fastmcp_import(url):
            return ["mcp<2"], f"mcp.server.fastmcp import detected in {url}"
    return [], None


@router.post("/tools/install")
async def install_dependency(body: InstallRequest):
    """Install packages via pip/uv/uv-tool/npm (fixed argv, no shell).

    Request: {"manager": "pip"|"uv"|"uv-tool"|"npm", "packages": [...]}.
    pip/uv are pinned to --target <EXTRAS_DIR>; extra flags rejected.
    pip/uv(-tool) additionally accept URL specs (git+https://...,
    https://...tar.gz/.whl) and PEP 508 ranges. URL specs download and
    run upstream code at the package author's discretion — only install
    sources you trust.

    constraints（依存ピン、例 "mcp<2"）: uv-tool は各項を --with <spec>
    として packages の前に、pip/uv は argv 末尾に連結（--target は不変）。
    固定 argv・シェル無しは維持。npm は 400（非対応）。

    自動ピン: 呼び出し側が constraints を明示せず、かつ git+URL 仕様を
    含む場合、ソースを shallow clone して mcp.server.fastmcp (mcp 1.x
    API) import を検出したら mcp<2 を自動適用する。検出結果はレスポンスの
    auto_constraints / auto_constraints_reason に返す。判定不能
    （clone 失敗等）時は自動ピンなしで通常インストールに続行する。
    """
    manager, packages = _validate_manager_packages(body)

    constraints: list[str] = list(body.constraints or [])
    if constraints and manager == "npm":
        raise HTTPException(400, detail="npm は constraints 未対応です")
    if constraints:
        constraints = _validate_constraints(constraints)

    # git+URL ソースの事前スキャン: 呼び出し側が明示していない場合のみ
    # mcp<2 自動ピンを判定する（明示指定は上書きしない）。
    auto_constraints: list[str] = []
    auto_reason: str | None = None
    if (
        manager in ("pip", "uv", "uv-tool")
        and not constraints
        and any(p.startswith("git+") for p in packages)
    ):
        auto_constraints, auto_reason = await _detect_auto_mcp_pin(packages)
        if auto_constraints:
            constraints = auto_constraints
            logger.info("Auto-pinning %s: %s", auto_constraints, auto_reason)

    # レジストリ固定: env が https のときだけ --index-url / --registry を挿す
    # （未設定・非 https は既定レジストリに任せ、argv を従来通り保つ）。
    index_url = _https_env_url("MCP_HUB_PIP_INDEX_URL", None)
    index_args = ["--index-url", index_url] if index_url else []
    if manager == "pip":
        argv = [
            "pip", "install", "--target", _EXTRAS_DIR,
            *index_args, *packages, *constraints,
        ]
    elif manager == "uv":
        argv = [
            "uv", "pip", "install", "--target", _EXTRAS_DIR,
            *index_args, *packages, *constraints,
        ]
    elif manager == "uv-tool":
        with_args = [a for c in constraints for a in ("--with", c)]
        argv = ["uv", "tool", "install", *with_args, *index_args, *packages]
    else:
        registry = _https_env_url("MCP_HUB_NPM_REGISTRY", None)
        registry_args = ["--registry", registry] if registry else []
        argv = ["npm", "install", *registry_args, *packages]

    # Ensure EXTRAS_DIR exists
    try:
        os.makedirs(_EXTRAS_DIR, exist_ok=True)
    except OSError:
        logger.exception("Failed to create %s", _EXTRAS_DIR)
        raise HTTPException(500, detail=f"Failed to create {_EXTRAS_DIR}") from None

    try:
        result = await _run_pkg_command(argv, "Install")
        if auto_constraints:
            result["auto_constraints"] = auto_constraints
            if auto_reason:
                result["auto_constraints_reason"] = auto_reason
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Install command failed")
        raise HTTPException(500, detail=str(e)) from e


@router.post("/tools/uninstall")
async def uninstall_dependency(body: InstallRequest):
    """Uninstall packages (same InstallRequest as /tools/install).

    npm / uv-tool: fixed argv (`npm uninstall` / `uv tool uninstall`,
    no shell). pip/uv: pip has no --target-aware uninstall, so matching
    directories under EXTRAS_DIR are removed instead (name validation
    plus realpath pinning make traversal impossible). Zero hits →
    {success: false, removed: []}.
    """
    if body.constraints:
        raise HTTPException(400, detail="uninstall は constraints 非対応です")
    manager, packages = _validate_manager_packages(body)

    try:
        if manager in ("pip", "uv"):
            removed = _remove_extras_dirs(packages)
            return {
                "success": bool(removed),
                "returncode": 0 if removed else 1,
                "removed": removed,
                "stdout": "\n".join(removed),
                "stderr": "" if removed else f"no matching packages under {_EXTRAS_DIR}",
            }

        if manager == "uv-tool":
            argv = ["uv", "tool", "uninstall", *packages]
        else:
            argv = ["npm", "uninstall", *packages]
        return await _run_pkg_command(argv, "Uninstall")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Uninstall command failed")
        raise HTTPException(500, detail=str(e)) from e


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
