"""フル公開（メタOFF）機能用 middleware。

meta モードで hub.config.json の full_info_tools に指定されたツール
（"{server}_{tool}" 形式）を、tools/list に通常ツールとして追加し、
tools/call では execute_tool 経由をスキップして直接下流サーバーへ転送する。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool, ToolResult
from mcp.types import TextContent

from .middleware import split_qualified_name
from .state import app_state

if TYPE_CHECKING:
    import mcp.types as mt

logger = logging.getLogger(__name__)

# (server, tool_name) -> ツール定義 dict（{name, description, server, inputSchema}）| None
GetSchemaFn = Callable[[str, str], dict | None]


class FullInfoMiddleware(Middleware):
    """full_info_tools に指定されたツールをフル公開する。"""

    def __init__(self, proxy_manager, get_schema_fn: GetSchemaFn) -> None:
        super().__init__()
        self._pm = proxy_manager
        self._get_schema = get_schema_fn

    def _full_info_entries(self) -> set[str]:
        """hub.config.json の full_info_tools を同期参照する（キャッシュ不要）。"""
        registry = app_state.registry
        if registry is None:
            return set()
        data = getattr(registry, "_data", None) or {}
        entries = data.get("full_info_tools") or []
        return {e for e in entries if isinstance(e, str)}

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = list(await call_next(context))
        connected = self._pm.get_connected_servers()
        for entry in sorted(self._full_info_entries()):
            server, tool_name = split_qualified_name(entry, connected)
            if server == "-":
                continue  # 未接続サーバーのエントリは除外
            schema = self._get_schema(server, tool_name)
            if schema is None:
                continue  # ツール不在は除外
            try:
                tools.append(
                    Tool(
                        name=entry,
                        description=schema.get("description") or "",
                        parameters=schema.get("inputSchema") or {},
                    )
                )
            except Exception:
                # SEP-986 名規則違反などはスキップ
                logger.warning("full_info_tools のツール追加をスキップ: %s", entry)
        return tools

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        name = context.message.name
        if name not in self._full_info_entries():
            return await call_next(context)
        connected = self._pm.get_connected_servers()
        server, tool_name = split_qualified_name(name, connected)
        if server == "-":
            return ToolResult(is_error=True, content=[TextContent(type="text", text=f"Server for {name!r} not found")])
        arguments = context.message.arguments or {}
        try:
            return await self._pm.call_tool(server, tool_name, arguments)
        except Exception as e:
            return ToolResult(is_error=True, content=[TextContent(type="text", text=str(e))])
