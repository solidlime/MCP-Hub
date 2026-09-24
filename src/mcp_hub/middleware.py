"""ツールログ記録用 middleware。

fastmcp の Middleware.on_call_tool をオーバーライドし、tools/call の
前後を包んで呼び出し記録・所要時間・エラー詳細を _AppState の
リングバッファに追記する。normal_app と meta_app の両方に登録する。
"""

from __future__ import annotations

import json
import time
import traceback
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

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

        start = time.monotonic()
        try:
            result = await call_next(context)
        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            await app_state.inc_tool_call_errors()
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
