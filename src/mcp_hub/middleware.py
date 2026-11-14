"""ツールログ記録用 middleware。

fastmcp の Middleware.on_call_tool をオーバーライドし、tools/call の
前後を包んで呼び出し記録・所要時間・エラー詳細を _AppState の
リングバッファに追記する。normal_app と meta_app の両方に登録する。
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool, ToolResult
from mcp.types import TextContent

from .masking import _TRACEBACK_MAX_LEN, mask_args, mask_text
from .state import LogEntry, app_state

if TYPE_CHECKING:
    import mcp.types as mt

# meta モードのローカルツール名
_META_TOOL_EXECUTE = "execute_tool"


def resolve_server(tool_name: str, arguments: dict, connected: dict[str, Any]) -> tuple[str, str]:
    """tool name と arguments から (server, tool) を解決する。

    通常モード: マウントは {namespace}_{tool} 形式。name が f"{server}_" で
    始まる最長一致で get_connected_servers() から逆引きする。
    完全一致（namespace なしで直接マウントされたツール）もフォールバックで拾う。
    meta モード: tool name が execute_tool の場合、arguments の
    {"server", "tool_name"} を実サーバー・実ツールとして使う。
    不明なら ("-", tool_name) を返す。
    """
    if tool_name == _META_TOOL_EXECUTE:
        server = arguments.get("server") or "-"
        return str(server), str(arguments.get("tool_name") or tool_name)

    best: str | None = None
    for name in connected:
        if tool_name.startswith(f"{name}_"):
            if best is None or len(name) > len(best):
                best = name
    if best is not None:
        return best, tool_name
    if tool_name in connected:
        return tool_name, tool_name
    return "-", tool_name


def split_qualified_name(name: str, connected: dict[str, Any]) -> tuple[str, str]:
    """'{server}_{tool}' 形式の名前を (server, tool) に分解する。

    connected のサーバー名のうち、name が f"{server}_" で始まる接頭辞の
    最長一致で server を特定し、残りを tool として返す。
    マッチしなければ ("-", name) を返す。
    full_info_tools のエントリ照合と on_call_tool の名前分解で共用する。
    """
    best: str | None = None
    for server in connected:
        if name.startswith(f"{server}_"):
            if best is None or len(server) > len(best):
                best = server
    if best is None:
        return "-", name
    return best, name[len(best) + 1:]


class ToolLogMiddleware(Middleware):
    """tools/call を包んでツール呼び出しを記録する。"""

    def __init__(self, proxy_manager) -> None:
        super().__init__()
        self._pm = proxy_manager

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """wire 直前: wrap-result マーカー付き outputSchema のみ落とす。

        FastMCP は `-> str` 等の非オブジェクト返しを wrap-result で包み、
        content と structuredContent.result に同一内容を二重で載せる。
        その wrap ツール（x-fastmcp-wrap-result）だけ outputSchema を外して
        on_call_tool の structured_content 除去と対を取る。マーカー無しの
        schema（Proxy 経由の上流 schema 等）は保持し、
        mcp/client/session.py:validate_tool_result の RuntimeError を防ぐ。
        """
        tools = await call_next(context)
        out: list[Tool] = []
        for t in tools:
            if isinstance(t.output_schema, dict) and t.output_schema.get("x-fastmcp-wrap-result"):
                t = t.model_copy(update={"output_schema": None})
            out.append(t)
        return out

    def _cache_key(
        self, name: str, server: str, tool: str, arguments: dict
    ) -> str | None:
        """キャッシュ対象なら結果キャッシュのキーを返す（対象外は None）。"""
        cache = app_state.tool_cache
        if not cache.is_cacheable(server, tool):
            return None
        # meta モードの execute_tool は内側の arguments で判定する。
        payload = arguments.get("arguments") if name == _META_TOOL_EXECUTE else arguments
        return cache.make_key(server, tool, payload if isinstance(payload, dict) else {})

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, Any],
    ) -> Any:
        params = context.message
        name = params.name
        arguments = params.arguments or {}
        server, tool = resolve_server(name, arguments, self._pm.get_connected_servers())

        # 受信フェーズを記録。call_next が例外を投げても started は残る
        # （try の外に置く）。started のみで完了ログがない = 進行中/ハング。
        app_state.append_log(LogEntry(
            ts=time.time(),
            type="tool_call",
            server=server,
            tool=tool,
            status="started",
            args=mask_args(arguments),
        ))

        # 任意機能: 冪等/参照系ツールの結果キャッシュ（既定 OFF）。
        cache = app_state.tool_cache
        cache_key = self._cache_key(name, server, tool, arguments)
        if cache_key is not None:
            cached = cache.get(cache_key)
            if cached is not None:
                app_state.metrics.record_tool_call(server, tool, "cache_hit", 0.0)
                app_state.append_log(LogEntry(
                    ts=time.time(),
                    type="tool_call",
                    server=server,
                    tool=tool,
                    status="cache_hit",
                    duration_ms=0.0,
                    args=mask_args(arguments),
                ))
                return cached

        start = time.monotonic()
        try:
            result = await call_next(context)
        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            await app_state.inc_tool_call_errors()
            app_state.metrics.record_tool_call(server, tool, "error", duration_ms / 1000)
            app_state.append_log(LogEntry(
                ts=time.time(),
                type="tool_call",
                server=server,
                tool=tool,
                status="error",
                duration_ms=round(duration_ms, 1),
                args=mask_args(arguments),
                error=mask_text(str(e)),
                traceback=mask_text(traceback.format_exc(), _TRACEBACK_MAX_LEN),
            ))
            raise

        duration_ms = (time.monotonic() - start) * 1000
        status = "success"
        error_text: str | None = None

        # meta モード: execute_tool はタグ拒否・ツール不在を JSON 文字列で
        # 200 返すだけ（is_error=False）。content の JSON に error キーが
        # ある場合は error として記録する。
        if name == _META_TOOL_EXECUTE:
            error_text = _extract_json_error(result)
            if error_text:
                status = "error"

        if status == "success":
            await app_state.inc_tool_calls()
        else:
            await app_state.inc_tool_call_errors()
        app_state.metrics.record_tool_call(server, tool, status, duration_ms / 1000)

        app_state.append_log(LogEntry(
            ts=time.time(),
            type="tool_call",
            server=server,
            tool=tool,
            status=status,
            duration_ms=round(duration_ms, 1),
            args=mask_args(arguments),
            error=mask_text(error_text) if error_text else None,
        ))

        # wire 直前: 完全二重化した structured_content のみ除く。
        # wrap-result ツールは FastMCP が content と structuredContent.result
        # に同一内容を載せるため応答が2倍になる。判定は
        # 「単一 TextContent と structured_content が完全一致」の形状条件に
        # 加え、fastmcp/tools/base.py:430 が wrap 時に必ず付与する真正印
        # meta={"fastmcp": {"wrap_result": True}} を AND で要求する
        # （on_list_tools の x-fastmcp-wrap-result 判定と同じ判定源）。
        # 形状だけの上流 schema（マーカー無し）は素通しし、下流 SDK
        # クライアントの outputSchema 検証 RuntimeError を防ぐ。
        # content は _extract_json_error の JSON 判定入力なので一切触らない。
        if (
            isinstance(result, ToolResult)
            and isinstance(result.meta, dict)
            and isinstance(result.meta.get("fastmcp"), dict)
            and result.meta["fastmcp"].get("wrap_result") is True
            and len(result.content) == 1
            and isinstance(result.content[0], TextContent)
            and result.structured_content == {"result": result.content[0].text}
        ):
            result.structured_content = None
            result._raw_mcp_result = None  # 原生 CallToolResult 直通の復元を無効化

        # 成功結果のみキャッシュ。エラー/例外（上の raise）は入れない。
        if cache_key is not None and status == "success" and not getattr(result, "is_error", False):
            cache.put(cache_key, result)
        return result


def _extract_json_error(result: Any) -> str | None:
    """ToolResult の content から JSON の error キーを探す。"""
    content = getattr(result, "content", None)
    if not content:
        return None
    for block in content:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            data = json.loads(text)
        except Exception:
            continue
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])
    return None


class LimitsMiddleware(Middleware):
    """サーバー単位の timeout / 同時実行 / レート制限を適用する（最外殻）。

    normal / meta / full_info の全 tools/call がこの層を通るため、ここが
    単一の適用点になる。制限未設定のサーバーは従来と同じ挙動（timeout は
    グローバル既定 MCP_HUB_CALL_TOOL_TIMEOUT）。
    """

    def __init__(self, proxy_manager) -> None:
        super().__init__()
        self._pm = proxy_manager

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, Any],
    ) -> Any:
        name = context.message.name
        arguments = context.message.arguments or {}
        server, tool = resolve_server(name, arguments, self._pm.known_servers())
        timeout = self._pm.server_call_timeout(server)

        async with self._pm.call_slot(server, tool):
            if timeout is None:
                return await call_next(context)
            try:
                return await asyncio.wait_for(call_next(context), timeout)
            except (asyncio.TimeoutError, TimeoutError):
                raise ToolError(
                    f"Call to server {server!r} tool {tool!r} timed out after "
                    f"{timeout:g}s (limits.call_timeout)"
                ) from None
