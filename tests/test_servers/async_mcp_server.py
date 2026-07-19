"""Test MCP server implementing Streamable HTTP 202 async pattern (simulating EDINET DB behavior).

Listens on localhost:18765 (configurable via ASYNC_MCP_PORT env var).
Uses FastAPI/uvicorn — intentionally NOT FastMCP — to test against a
server that doesn't use FastMCP's patterns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

DEFAULT_PORT = 18765

# Track how many times each request ID has been polled at the status endpoint.
# Key: request ID (str), Value: poll count (int)
_poll_state: dict[str, int] = {}

_server: uvicorn.Server | None = None
_port: int = DEFAULT_PORT
_server_task: asyncio.Task[None] | None = None


def _reset_state() -> None:
    _poll_state.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_request_id(body: dict) -> str | None:
    rid = body.get("id")
    if rid is None:
        return None
    return str(rid)


def _make_error(rid: str | int | None, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "error": {"code": code, "message": message},
    }


def _json_response(data: dict, status: int = 200, extra_headers: dict | None = None) -> Response:
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    return Response(
        content=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        status_code=status,
        headers=headers,
    )


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/mcp")
async def handle_mcp(request: Request) -> Response:
    """Main MCP endpoint — POST /mcp"""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_response(_make_error(None, -32700, "Parse error"), status=400)

    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return _json_response(_make_error(None, -32600, "Invalid Request"), status=400)

    method: str = body.get("method", "")

    if method == "initialize":
        return _handle_initialize(body)
    elif method == "tools/list":
        return _handle_tools_list(body)
    elif method == "tools/call":
        return _handle_tools_call(body)
    else:
        rid = _get_request_id(body)
        return _json_response(_make_error(rid, -32601, f"Method not found: {method}"))


@app.post("/mcp/status/tools-list-result")
async def handle_status(request: Request) -> Response:
    """Polling endpoint — POST /mcp/status/tools-list-result.
    
    Accepts empty POST bodies (MCP-Hub patch sends no JSON-RPC payload on poll,
    to avoid re-executing the original request). When no request ID is found in
    the body, uses any pending poll request.
    """
    rid = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            rid = _get_request_id(body)
    except (json.JSONDecodeError, ValueError):
        pass

    # If no rid from body, use any pending poll request
    if rid is None:
        for key in _poll_state:
            rid = key
            break

    if rid is None:
        return _json_response(
            _make_error(None, -32600, "Invalid Request: no pending poll"), status=400
        )

    count = _poll_state.get(rid, 0)
    if count == 0:
        # First poll — still processing
        _poll_state[rid] = 1
        return Response(
            content=json.dumps({"jsonrpc": "2.0", "id": rid}, ensure_ascii=False).encode("utf-8"),
            status_code=202,
            headers={
                "Content-Type": "application/json",
                "Location": "/mcp/status/tools-list-result",
            },
        )
    else:
        # Second (or later) poll — return result
        _poll_state.pop(rid, None)
        return _json_response({
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "tools": [
                    {
                        "name": "search_async",
                        "description": "Search asynchronously",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                            },
                        },
                    },
                ],
            },
        })


# ---------------------------------------------------------------------------
# OPTIONS handlers
# ---------------------------------------------------------------------------

@app.options("/mcp")
@app.options("/mcp/status/tools-list-result")
async def handle_options() -> Response:
    """CORS preflight."""
    return Response(status_code=200, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    })


# ---------------------------------------------------------------------------
# JSON-RPC method handlers
# ---------------------------------------------------------------------------

def _handle_initialize(body: dict) -> Response:
    rid = _get_request_id(body)
    return _json_response({
        "jsonrpc": "2.0",
        "id": rid,
        "result": {
            "protocolVersion": "2025-06-18",
            "serverInfo": {"name": "async-test", "version": "1.0"},
            "capabilities": {"tools": {}},
        },
    })


def _handle_tools_list(body: dict) -> Response:
    """Return 202 Accepted — the result must be polled."""
    rid = _get_request_id(body)
    if rid:
        _poll_state[rid] = 0  # initialise poll counter
    return Response(
        content=json.dumps({"jsonrpc": "2.0", "id": rid}, ensure_ascii=False).encode("utf-8"),
        status_code=202,
        headers={
            "Content-Type": "application/json",
            "Location": "/mcp/status/tools-list-result",
        },
    )


def _handle_tools_call(body: dict) -> Response:
    rid = _get_request_id(body)
    params = body.get("params", {})
    name = params.get("name", "")
    args = params.get("arguments", {})

    if name == "search_async":
        query = args.get("query", "")
        return _json_response({
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": f'Search results for "{query}": [async result]',
                    }
                ],
            },
        })

    return _json_response(_make_error(rid, -32601, f"Method not found: tools/call for {name}"))


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def start_server() -> None:
    """Start the async MCP test server on localhost.

    Reads ASYNC_MCP_PORT env var or defaults to 18765.
    """
    global _server, _port, _server_task
    _reset_state()
    _port = int(os.environ.get("ASYNC_MCP_PORT", str(DEFAULT_PORT)))

    config = uvicorn.Config(
        app,
        host="localhost",
        port=_port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    _server = server
    _server_task = asyncio.create_task(server.serve())
    # Give the server a moment to bind the socket
    await asyncio.sleep(0.1)
    logger.info("Async MCP test server started on localhost:%d", _port)


async def stop_server() -> None:
    """Stop the async MCP test server."""
    global _server, _server_task
    if _server is not None:
        _server.should_exit = True
        await _server.shutdown()
        _server = None
    if _server_task is not None:
        _server_task.cancel()
        _server_task = None
        logger.info("Async MCP test server stopped")


def get_port() -> int:
    """Return the port the server is listening on."""
    return _port


# ---------------------------------------------------------------------------
# Manual testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("ASYNC_MCP_PORT", str(DEFAULT_PORT)))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
