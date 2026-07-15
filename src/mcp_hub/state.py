"""共有状態。lifespan で初期化され、admin_router から参照される。"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .proxy_manager import ProxyManager
    from .store import JsonStore

request_tags: ContextVar[list[str] | None] = ContextVar("request_tags", default=None)


class _AppState:
    registry: JsonStore | None = None
    proxy_manager: ProxyManager | None = None
    start_time: float = 0.0
    tool_calls_total: int = 0
    tool_call_errors: int = 0
    mcp_dispatcher: object | None = None


app_state = _AppState()
