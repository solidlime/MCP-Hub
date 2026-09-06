"""Regression tests: HTTP 4xx JSON-RPC error handling in streamable_http_patch.

Mirrors the Exa/Mapbox failure mode: remote servers reject SDK 2.x's
`server/discover` preflight with HTTP 400 + a JSON-RPC error body. The patched
POST handler must surface that error as an MCPError on the session's read
stream (so the connect-time probe can fall back to `initialize`) instead of
raising httpx2.HTTPStatusError, which would kill the whole connection.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from mcp import MCPError
from mcp_hub.streamable_http_patch import apply_patch

# Apply patch before any tests run (required for Streamable HTTP 4xx handling)
apply_patch()


DISCOVER_ERROR_MESSAGE = (
    "Bad Request: Unsupported protocol version: 2026-07-28 "
    "(supported versions: 2025-11-25, 2025-06-18, 2025-03-26)"
)


def _rpc_error(message: str, *, code: int = -32000) -> dict:
    return {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}


def _build_app(state: dict[str, bool]) -> FastAPI:
    app = FastAPI()

    @app.post("/mcp")
    async def post_mcp(request: Request):
        body = await request.json()
        method = body.get("method")
        msg_id = body.get("id")

        if method == "server/discover":
            # Exa/Mapbox style rejection: HTTP 400 + JSON-RPC error, id=null.
            return JSONResponse(status_code=400, content=_rpc_error(DISCOVER_ERROR_MESSAGE))

        if method == "initialize":
            if state["fail_initialize"]:
                return JSONResponse(status_code=400, content=_rpc_error(DISCOVER_ERROR_MESSAGE))
            return JSONResponse(
                content={
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "serverInfo": {"name": "fake-4xx-server", "version": "0.0.0"},
                    },
                }
            )

        if method == "notifications/initialized":
            return Response(status_code=202)

        if method == "tools/list":
            return JSONResponse(
                content={
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "Echo back the input text.",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                    "required": ["text"],
                                },
                            }
                        ]
                    },
                }
            )

        return JSONResponse(content=_rpc_error("Method not found", code=-32601))

    return app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def fake_server():
    """Start the fake streamable-http server on a background event loop."""
    state = {"fail_initialize": False}
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(_build_app(state), host="127.0.0.1", port=port, log_level="warning")
    )

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    asyncio.run_coroutine_threadsafe(server.serve(), loop)

    # Wait for the port to accept connections.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("fake test server failed to start")

    yield f"http://127.0.0.1:{port}/mcp", state

    server.should_exit = True
    thread.join(timeout=5)
    loop.call_soon_threadsafe(loop.stop)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHttp4xxFallback:
    @pytest.mark.asyncio
    async def test_discover_400_falls_back_to_initialize(self, fake_server):
        """server/discover 400 + JSON-RPC error -> probe falls back to initialize."""
        from fastmcp import Client
        from fastmcp.client.transports.http import StreamableHttpTransport

        url, _state = fake_server
        transport = StreamableHttpTransport(url=url)
        async with Client(transport=transport, timeout=10) as client:
            tools = await client.list_tools()
        tool_names = [t.name for t in tools]
        assert "echo" in tool_names, f"Tools: {tool_names}"

    @pytest.mark.asyncio
    async def test_initialize_400_surfaces_mcp_error(self, fake_server):
        """initialize 400 + JSON-RPC error -> MCPError (not HTTPStatusError)."""
        from fastmcp import Client
        from fastmcp.client.transports.http import StreamableHttpTransport

        url, state = fake_server
        state["fail_initialize"] = True
        try:
            transport = StreamableHttpTransport(url=url)
            with pytest.raises(MCPError):
                async with Client(transport=transport, timeout=10):
                    pass
        finally:
            state["fail_initialize"] = False
