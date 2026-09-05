"""Lenient session manager: tolerate unknown session IDs on POST.

After an MCP-Hub restart, clients that keep their old session ID would
otherwise receive 404 "Session not found" forever (opencode does not
re-establish sessions on reconnect). Instead, route unknown-session POSTs
through the SDK's stateless request path so tool calls still work.
"""

from __future__ import annotations

import logging

from fastmcp.server.http import FastMCPStreamableHTTPSessionManager
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER

logger = logging.getLogger(__name__)


class LenientSessionManager(FastMCPStreamableHTTPSessionManager):
    """Session manager that tolerates unknown session IDs on POST.

    GET/DELETE with unknown session IDs still return 404 (a stateless GET
    would just hang an SSE stream). Only POST is made lenient.
    """

    async def handle_request(self, scope, receive, send):
        if scope["method"] == "POST" and self._is_unknown_session(scope):
            # mcp 2.x: _handle_stateless_request takes protocol_version_hint
            # first (from the mcp-protocol-version header, None if absent).
            header = MCP_PROTOCOL_VERSION_HEADER.encode("ascii")
            pv = next(
                (v.decode("latin-1") for k, v in scope.get("headers", []) if k == header),
                None,
            )
            await self._handle_stateless_request(pv, scope, receive, send)
            return
        await super().handle_request(scope, receive, send)

    def _is_unknown_session(self, scope) -> bool:
        headers = dict(scope.get("headers") or [])
        session_id = headers.get(MCP_SESSION_ID_HEADER.encode())
        if not session_id:
            return False  # 新規 initialize → 従来の stateful パス
        try:
            text = session_id.decode()
        except UnicodeDecodeError:
            # 不正バイトは 500 にせず置換デコードで不明扱いに落とす
            logger.warning("不正バイトの session id を受信: %r", session_id)
            text = session_id.decode(errors="replace")
        return text not in self._server_instances