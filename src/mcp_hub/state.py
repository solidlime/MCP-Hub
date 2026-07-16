"""共有状態。lifespan で初期化され、admin_router から参照される。"""

import asyncio
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
    _tool_calls_total: int = 0
    _tool_call_errors: int = 0
    mcp_dispatcher: object | None = None
    _stats_lock: asyncio.Lock | None = None

    def _ensure_lock(self) -> asyncio.Lock:
        if self._stats_lock is None:
            self._stats_lock = asyncio.Lock()
        return self._stats_lock

    @property
    def tool_calls_total(self) -> int:
        return self._tool_calls_total

    @property
    def tool_call_errors(self) -> int:
        return self._tool_call_errors

    async def inc_tool_calls(self) -> None:
        async with self._ensure_lock():
            self._tool_calls_total += 1

    async def inc_tool_call_errors(self) -> None:
        async with self._ensure_lock():
            self._tool_call_errors += 1


app_state = _AppState()
