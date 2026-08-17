"""Lenient session manager: tolerate unknown session IDs on POST.

After an MCP-Hub restart, clients that keep their old session ID would
otherwise receive 404 "Session not found" forever (opencode does not
re-establish sessions on reconnect). Instead, route unknown-session POSTs
through the SDK's stateless request path so tool calls still work.
"""

from __future__ import annotations

from fastmcp.server.http import FastMCPStreamableHTTPSessionManager
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER


class LenientSessionManager(FastMCPStreamableHTTPSessionManager):
    """Session manager that tolerates unknown session IDs on POST.

    GET/DELETE with unknown session IDs still return 404 (a stateless GET
    would just hang an SSE stream). Only POST is made lenient.
    """

    async def handle_request(self, scope, receive, send):
        if scope["method"] == "POST" and self._is_unknown_session(scope):
            await self._handle_stateless_request(scope, receive, send)
            return
        await super().handle_request(scope, receive, send)

    def _is_unknown_session(self, scope) -> bool:
        headers = dict(scope.get("headers") or [])
        session_id = headers.get(MCP_SESSION_ID_HEADER.encode())
        if not session_id:
            return False  # 新規 initialize → 従来の stateful パス
        return session_id.decode() not in self._server_instances