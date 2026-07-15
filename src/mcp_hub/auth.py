"""X-API-Key authentication middleware for admin API."""
import logging
import os
from secrets import compare_digest

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Used for path matching (handles /admin/api/health and /admin/api/health/)
EXEMPT_PATHS = {"/admin/api/health", "/admin/api/health/"}
EXEMPT_PATH_PREFIXES = ("/admin/api/health/",)


def get_api_key() -> str | None:
    """Return configured API key, or None if auth is disabled."""
    key = os.environ.get("MCP_HUB_API_KEY", "").strip()
    return key if key else None


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Require X-API-Key header on /admin/api/* when MCP_HUB_API_KEY is set.

    If no key is configured, all requests pass through.
    """

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/admin/api/"):
            return await call_next(request)

        # Exact match for /admin/api/health (and /admin/api/health/)
        # Prefix match for sub-paths like /admin/api/health/status
        path = request.url.path
        if path in EXEMPT_PATHS:
            return await call_next(request)
        for prefix in EXEMPT_PATH_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        expected = get_api_key()
        if expected is None:
            return await call_next(request)

        provided = request.headers.get("X-API-Key", "")
        if not compare_digest(expected, provided):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key"},
            )

        return await call_next(request)
