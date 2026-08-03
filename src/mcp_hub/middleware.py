"""ツールログ記録用 middleware。

fastmcp の Middleware.on_call_tool をオーバーライドし、tools/call の
前後を包んで呼び出し記録・所要時間・エラー詳細を _AppState の
リングバッファに追記する。normal_app と meta_app の両方に登録する。
"""

from __future__ import annotations

import json
import logging
import time
import traceback
from typing import TYPE_CHECKING, Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from .masking import _TRACEBACK_MAX_LEN, mask_args, mask_text
from .state import LogEntry, app_state

if TYPE_CHECKING:
    import mcp.types as mt

logger = logging.getLogger(__name__)

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


class ToolLogMiddleware(Middleware):
    """tools/call を包んでツール呼び出しを記録する。"""

    def __init__(self, proxy_manager) -> None:
        super().__init__()
        self._pm = proxy_manager

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, Any],
    ) -> Any:
        params = context.message
        name = params.name
        arguments = params.arguments or {}
        server, tool = resolve_server(name, arguments, self._pm.get_connected_servers())

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
