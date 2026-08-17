"""Tests for LenientSessionManager: unknown session POSTs are handled statelessly.

After an MCP-Hub restart, clients keep sending their old session ID. The
lenient manager routes those POSTs through the SDK's stateless path instead
of returning 404 forever. GET/DELETE with unknown session IDs still 404.
"""

from __future__ import annotations

import json

import pytest

from fastmcp import FastMCP
from mcp_hub.lenient_session_manager import LenientSessionManager


def _make_server():
    mcp = FastMCP("lenient-test")

    @mcp.tool
    def add(a: int, b: int) -> int:
        return a + b

    return mcp._mcp_server


class _Client:
    """Minimal ASGI test client for a single request."""

    def __init__(self, method: str, body: bytes, headers: list[tuple[bytes, bytes]]):
        self.scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 26263),
        }
        self._body = body
        self._sent = False
        self.status: int | None = None
        self.headers: list[tuple[bytes, bytes]] = []
        self.body = b""

    async def receive(self):
        if self._sent:
            return {"type": "http.disconnect"}
        self._sent = True
        return {"type": "http.request", "body": self._body, "more_body": False}

    async def send(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = message.get("headers", [])
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")


def _headers(session_id: str | None = None) -> list[tuple[bytes, bytes]]:
    headers = [
        (b"content-type", b"application/json"),
        (b"accept", b"application/json, text/event-stream"),
    ]
    if session_id is not None:
        headers.append((b"mcp-session-id", session_id.encode()))
    return headers


def _tools_call(name: str, arguments: dict) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    ).encode()


def _initialize() -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0"},
            },
        }
    ).encode()


def _session_id_from(client: _Client) -> str:
    for k, v in client.headers:
        if k == b"mcp-session-id":
            return v.decode()
    raise AssertionError(f"no mcp-session-id header in {client.headers}")


@pytest.mark.asyncio
async def test_unknown_session_post_is_stateless():
    """POST with unknown session ID → 200 + tool result, not 404."""
    sm = LenientSessionManager(app=_make_server(), json_response=True)
    async with sm.run():
        client = _Client(
            "POST", _tools_call("add", {"a": 1, "b": 2}), _headers("unknown-session-123")
        )
        await sm.handle_request(client.scope, client.receive, client.send)
        assert client.status == 200, client.body
        result = json.loads(client.body)
        assert result["result"]["content"][0]["text"] == "3"


@pytest.mark.asyncio
async def test_known_session_post_is_stateful():
    """POST with a real session ID → stateful path (session created by initialize)."""
    sm = LenientSessionManager(app=_make_server(), json_response=True)
    async with sm.run():
        init = _Client("POST", _initialize(), _headers())
        await sm.handle_request(init.scope, init.receive, init.send)
        assert init.status == 200, init.body
        session_id = _session_id_from(init)
        assert session_id in sm._server_instances

        call = _Client(
            "POST", _tools_call("add", {"a": 2, "b": 3}), _headers(session_id)
        )
        await sm.handle_request(call.scope, call.receive, call.send)
        assert call.status == 200, call.body
        result = json.loads(call.body)
        assert result["result"]["content"][0]["text"] == "5"


@pytest.mark.asyncio
async def test_post_without_session_id_creates_new_session():
    """POST without session ID (initialize) → new session, stateful path."""
    sm = LenientSessionManager(app=_make_server(), json_response=True)
    async with sm.run():
        client = _Client("POST", _initialize(), _headers())
        await sm.handle_request(client.scope, client.receive, client.send)
        assert client.status == 200, client.body
        session_id = _session_id_from(client)
        assert session_id in sm._server_instances


@pytest.mark.asyncio
async def test_unknown_session_get_still_404():
    """GET with unknown session ID → 404 (stateless GET would hang an SSE stream)."""
    sm = LenientSessionManager(app=_make_server(), json_response=True)
    async with sm.run():
        client = _Client("GET", b"", _headers("unknown-session-123"))
        await sm.handle_request(client.scope, client.receive, client.send)
        assert client.status == 404


@pytest.mark.asyncio
async def test_unknown_session_delete_still_404():
    """DELETE with unknown session ID → 404."""
    sm = LenientSessionManager(app=_make_server(), json_response=True)
    async with sm.run():
        client = _Client("DELETE", b"", _headers("unknown-session-123"))
        await sm.handle_request(client.scope, client.receive, client.send)
        assert client.status == 404