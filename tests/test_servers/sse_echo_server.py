"""SSE-based MCP test server for dogfood testing.

Exposes one tool: sse_echo(message: str) -> str returning "SSE_ECHO: {message}".
Uses FastMCP's HTTP app with SSE transport, served via uvicorn.

Port: configurable via SSE_ECHO_PORT env var (default: 18766).
"""

from __future__ import annotations

import asyncio
import logging
import os

import uvicorn
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

DEFAULT_PORT = 18766

mcp = FastMCP("sse-echo")


@mcp.tool
def sse_echo(message: str) -> str:
    """Echo the message back with an SSE prefix."""
    return f"SSE_ECHO: {message}"


app = mcp.http_app(transport="sse", path="/sse")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_server: uvicorn.Server | None = None
_port: int = DEFAULT_PORT
_server_task: asyncio.Task[None] | None = None

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def start_server() -> None:
    """Start the SSE Echo test server on localhost.

    Reads SSE_ECHO_PORT env var or defaults to 18766.
    """
    global _server, _port, _server_task
    _port = int(os.environ.get("SSE_ECHO_PORT", str(DEFAULT_PORT)))

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
    logger.info("SSE Echo test server started on localhost:%d", _port)


async def stop_server() -> None:
    """Stop the SSE Echo test server."""
    global _server, _server_task
    if _server is not None:
        _server.should_exit = True
        await _server.shutdown()
        _server = None
    if _server_task is not None:
        _server_task.cancel()
        _server_task = None
        logger.info("SSE Echo test server stopped")


def get_port() -> int:
    """Return the port the server is listening on."""
    return _port


# ---------------------------------------------------------------------------
# Manual testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("SSE_ECHO_PORT", str(DEFAULT_PORT)))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
