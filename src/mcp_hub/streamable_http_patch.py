"""Monkey-patch MCP SDK's StreamableHTTPTransport to handle 202 Accepted.

NOTE: Per the MCP Streamable HTTP spec, 202 Accepted is used ONLY for
notifications/responses — never for requests. The spec defines no
Location-header-based polling pattern.

Some non-standard servers (historically EDINET DB) may return 202 with a
Location header for long-running requests. This patch intercepts such
responses, polls the Location URL until a final result is received, then
feeds it through the normal response handling pipeline (JSON or SSE).

The polling sends an empty POST (no re-execution of the original request)
to the Location URL, matching common async-pattern semantics.

Usage:
    from mcp_hub.streamable_http_patch import apply_patch, restore_patch
    apply_patch()   # call once at startup, before any connections
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from urllib.parse import urljoin

import httpx2

from mcp.client.streamable_http import (
    StreamableHTTPTransport,
)
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCRequest, JSONRPCMessage
from mcp_types import ErrorData, INVALID_REQUEST, JSONRPCError

if TYPE_CHECKING:
    from mcp.client.streamable_http import RequestContext

logger = logging.getLogger(__name__)

# Maximum poll attempts and delay between polls
MAX_POLL_ATTEMPTS = 20
POLL_DELAY_SECONDS = 1.0

# Reference to original method for restore
_original_handle_post_request = StreamableHTTPTransport._handle_post_request
_patch_applied = False


async def _patched_handle_post_request(
    self: StreamableHTTPTransport, ctx: "RequestContext"
) -> None:
    """Patched _handle_post_request: handles 202 Accepted with polling."""
    headers = self._prepare_headers()
    message = ctx.session_message.message
    is_initialization = self._is_initialization_request(message)

    async with ctx.client.stream(
        "POST",
        self.url,
        json=message.model_dump(by_alias=True, mode="json", exclude_none=True),
        headers=headers,
    ) as response:
        # ── PATCHED: Handle 202 Accepted with Location polling ──
        if response.status_code == 202:
            location = response.headers.get("Location")
            if not location:
                logger.debug("Received 202 Accepted (no Location header) — giving up")
                return

            # Resolve relative Location URLs against the server base URL
            poll_url = urljoin(self.url, location)
            logger.debug("Received 202 Accepted, polling %s", poll_url)

            for attempt in range(MAX_POLL_ATTEMPTS):
                await asyncio.sleep(POLL_DELAY_SECONDS)
                try:
                    # Poll with empty POST — never re-send the original request body
                    # (which would re-execute tools/call, etc.)
                    poll_resp = await ctx.client.post(
                        poll_url,
                        headers=headers,
                    )
                except Exception:
                    logger.warning(
                        "Poll attempt %d/%d failed for %s",
                        attempt + 1, MAX_POLL_ATTEMPTS, poll_url,
                        exc_info=True,
                    )
                    continue

                if poll_resp.status_code == 200:
                    # Got the final result — process it like a normal response
                    logger.debug("Poll successful after %d attempts", attempt + 1)
                    # Extract session ID from final response if initialization
                    if is_initialization:
                        self._maybe_extract_session_id_from_response(poll_resp)
                    # Process the response like the original code does
                    await _process_response(
                        self, poll_resp, ctx, message, is_initialization,
                    )
                    return
                elif poll_resp.status_code == 202:
                    logger.debug(
                        "Still processing (attempt %d/%d)",
                        attempt + 1, MAX_POLL_ATTEMPTS,
                    )
                    continue
                else:
                    logger.warning(
                        "Poll returned unexpected status %d for %s",
                        poll_resp.status_code, poll_url,
                    )
                    poll_resp.raise_for_status()
                    return
            else:
                logger.warning(
                    "Polling exhausted (%d attempts) for %s",
                    MAX_POLL_ATTEMPTS, poll_url,
                )
            return
        # ── END PATCH ──

        # Original logic for non-202 responses (unchanged)
        if response.status_code == 404:
            if isinstance(message, JSONRPCRequest):
                # 2.x: _send_session_terminated_error is gone — inline the
                # Session-terminated error send (SessionMessage + JSONRPCError).
                error_data = ErrorData(code=INVALID_REQUEST, message="Session terminated")
                session_message = SessionMessage(
                    JSONRPCError(jsonrpc="2.0", id=message.id, error=error_data)
                )
                await ctx.read_stream_writer.send(session_message)
            return

        response.raise_for_status()
        await _process_response(self, response, ctx, message, is_initialization)


async def _process_response(
    self: StreamableHTTPTransport,
    response: "httpx2.Response",
    ctx: "RequestContext",
    message: JSONRPCMessage,
    is_initialization: bool,
) -> None:
    """Process a successful (200) response — JSON or SSE.

    Extracted from the original _handle_post_request to avoid code duplication
    between the direct 200 path and the 202→poll→200 path.
    """
    if is_initialization:
        self._maybe_extract_session_id_from_response(response)

    if isinstance(message, JSONRPCRequest):
        content_type = response.headers.get("content-type", "").lower()
        content_encoding = response.headers.get("content-encoding", "")
        content_length = response.headers.get("content-length", "")
        logger.debug(
            "Response from %s: Content-Type=%s Content-Encoding=%s Content-Length=%s",
            self.url, content_type, content_encoding or "none", content_length or "unknown",
        )

        if content_type.startswith("application/json"):
            # Pre-read body for diagnostics (aread() caches; _handle_json_response reuses cached bytes)
            body = await response.aread()
            if body and body[:1] != b"{":
                logger.warning(
                    "Non-JSON response body from %s (Content-Type: %s, Content-Encoding: %s, "
                    "first 128 bytes hex: %s)",
                    self.url, content_type, content_encoding or "none",
                    body[:128].hex(),
                )
            # 2.x: _handle_json_response(response, writer, *, request_id=...)
            await self._handle_json_response(
                response, ctx.read_stream_writer, request_id=message.id,
            )
        elif content_type.startswith("text/event-stream"):
            # 2.x: _handle_sse_response(response, ctx)
            await self._handle_sse_response(response, ctx)
        else:
            # 2.x: no _handle_unexpected* helper — inline the error send.
            logger.error("Unexpected content type: %s", content_type)
            error_data = ErrorData(
                code=INVALID_REQUEST, message=f"Unexpected content type: {content_type}"
            )
            error_msg = SessionMessage(
                JSONRPCError(jsonrpc="2.0", id=message.id, error=error_data)
            )
            await ctx.read_stream_writer.send(error_msg)


def apply_patch() -> None:
    """Apply the 202-handling monkey-patch globally."""
    global _patch_applied
    if _patch_applied:
        return
    StreamableHTTPTransport._handle_post_request = _patched_handle_post_request  # type: ignore[method-assign]
    _patch_applied = True
    logger.info("Streamable HTTP 202 polling patch applied")


def restore_patch() -> None:
    """Restore the original _handle_post_request (for cleanup in tests)."""
    global _patch_applied
    if not _patch_applied:
        return
    StreamableHTTPTransport._handle_post_request = _original_handle_post_request  # type: ignore[method-assign]
    _patch_applied = False
    logger.info("Streamable HTTP 202 polling patch restored")
