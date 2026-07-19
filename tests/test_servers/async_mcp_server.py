"""Test MCP server implementing Streamable HTTP 202 async pattern (simulating EDINET DB behavior).

Listens on localhost:18765 (configurable via ASYNC_MCP_PORT env var).
Uses aiohttp.web for HTTP, NOT FastMCP — this is intentionally a plain
implementation to test against a server that doesn't use FastMCP's patterns.
"""

from __future__ import annotations

import json
import logging
import os


import aiohttp.web

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

DEFAULT_PORT = 18765

# Track how many times each request ID has been polled at the status endpoint.
# Key: request ID (str), Value: poll count (int)
_poll_state: dict[str, int] = {}

_server: aiohttp.web.AppRunner | None = None
_port: int = DEFAULT_PORT


def _reset_state() -> None:
    _poll_state.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cors_headers() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    }


def _json_response(status: int, data: object) -> aiohttp.web.Response:
    return aiohttp.web.Response(
        status=status,
        body=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
        headers=_cors_headers(),
    )


def _get_request_id(body: dict) -> str | None:
    rid = body.get("id")
    if rid is None:
        return None
    return str(rid)


def _make_error(
    rid: str | int | None, code: int, message: str
) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "error": {"code": code, "message": message},
    }


# ---------------------------------------------------------------------------
# JSON-RPC method handlers
# ---------------------------------------------------------------------------

def _handle_initialize(body: dict) -> aiohttp.web.Response:
    rid = _get_request_id(body)
    return _json_response(200, {
        "jsonrpc": "2.0",
        "id": rid,
        "result": {
            "protocolVersion": "2025-06-18",
            "serverInfo": {"name": "async-test", "version": "1.0"},
            "capabilities": {"tools": {}},
        },
    })


def _handle_tools_list(body: dict) -> aiohttp.web.Response:
    """Return 202 Accepted — the result must be polled."""
    rid = _get_request_id(body)
    if rid:
        _poll_state[rid] = 0  # initialise poll counter
    return aiohttp.web.Response(
        status=202,
        body=json.dumps({
            "jsonrpc": "2.0",
            "id": rid,
        }, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
        headers={
            **_cors_headers(),
            "Location": "/mcp/status/tools-list-result",
        },
    )


def _handle_tools_call(body: dict) -> aiohttp.web.Response:
    rid = _get_request_id(body)
    params = body.get("params", {})
    name = params.get("name", "")
    args = params.get("arguments", {})

    if name == "search_async":
        query = args.get("query", "")
        return _json_response(200, {
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

    return _json_response(200, _make_error(rid, -32601, f"Method not found: tools/call for {name}"))


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

async def handle_mcp(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Main MCP endpoint — POST /mcp"""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_response(400, _make_error(None, -32700, "Parse error"))

    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return _json_response(400, _make_error(None, -32600, "Invalid Request"))

    method: str = body.get("method", "")

    if method == "initialize":
        return _handle_initialize(body)
    elif method == "tools/list":
        return _handle_tools_list(body)
    elif method == "tools/call":
        return _handle_tools_call(body)
    else:
        rid = _get_request_id(body)
        return _json_response(200, _make_error(rid, -32601, f"Method not found: {method}"))


async def handle_status(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Polling endpoint — POST /mcp/status/tools-list-result"""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_response(400, _make_error(None, -32700, "Parse error"))

    rid = _get_request_id(body)
    if rid is None:
        return _json_response(400, _make_error(None, -32600, "Invalid Request: missing id"))

    count = _poll_state.get(rid, 0)
    if count == 0:
        # First poll — still processing
        _poll_state[rid] = 1
        return aiohttp.web.Response(
            status=202,
            body=json.dumps({
                "jsonrpc": "2.0",
                "id": rid,
            }, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
            headers={
                **_cors_headers(),
                "Location": "/mcp/status/tools-list-result",
            },
        )
    else:
        # Second (or later) poll — return result
        _poll_state.pop(rid, None)
        return _json_response(200, {
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


async def handle_options(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """CORS preflight."""
    return aiohttp.web.Response(status=204, headers=_cors_headers())


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def _build_app() -> aiohttp.web.Application:
    app = aiohttp.web.Application()
    app.router.add_route("POST", "/mcp", handle_mcp)
    app.router.add_route("POST", "/mcp/status/tools-list-result", handle_status)
    app.router.add_route("OPTIONS", "/mcp", handle_options)
    app.router.add_route("OPTIONS", "/mcp/status/tools-list-result", handle_options)
    return app


async def start_server() -> None:
    """Start the async MCP test server on localhost.

    Reads ASYNC_MCP_PORT env var or defaults to 18765.
    """
    global _server, _port
    _reset_state()
    _port = int(os.environ.get("ASYNC_MCP_PORT", str(DEFAULT_PORT)))
    app = _build_app()
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "localhost", _port)
    await site.start()
    _server = runner
    logger.info("Async MCP test server started on localhost:%d", _port)


async def stop_server() -> None:
    """Stop the async MCP test server."""
    global _server
    if _server is not None:
        await _server.cleanup()
        _server = None
        logger.info("Async MCP test server stopped")


async def get_port() -> int:
    """Return the port the server is listening on."""
    return _port


# ---------------------------------------------------------------------------
# Manual testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def _main() -> None:
        await start_server()
        logger.info("Server running. Press Ctrl+C to stop.")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await stop_server()

    import asyncio

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
