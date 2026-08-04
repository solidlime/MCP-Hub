"""共有状態。lifespan で初期化され、admin_router から参照される。"""

from __future__ import annotations

import asyncio
from collections import deque
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .proxy_manager import ProxyManager
    from .store import JsonStore

request_tags: ContextVar[list[str] | None] = ContextVar("request_tags", default=None)


def tags_match(requested: list[str] | None, server_tags: list[str]) -> bool:
    """Check whether *requested* tags intersect with *server_tags* (OR logic).

    Returns True when:
    - *requested* is None or empty (no filter → pass)
    - Any requested tag is present in server_tags

    This is the single source of truth for tag-matching logic.
    """
    if not requested:
        return True
    return any(t in server_tags for t in requested)


# === ツールログ（リングバッファ） ===

@dataclass
class LogEntry:
    """一つのログエントリ（ツール呼び出し or サーバーイベント）。"""

    ts: float            # epoch 秒
    type: str            # "tool_call" | "server_event"
    server: str          # サーバー名（該当なしは "-"）
    tool: str            # ツール名（server_event では "-"）
    status: str          # success|error|timeout|connected|disconnected|spawn_failed|recovered|removed|updated
    duration_ms: float | None = None
    args: str | None = None        # マスク済み・最大500字（tool_call のみ）
    error: str | None = None       # エラー概要・最大500字（マスク適用済み）
    traceback: str | None = None   # 例外トレースバック・最大4000字（マスク適用済み）
    id: int = 0                    # 単調増加シーケンス（append_log が付与）

    def to_dict(self) -> dict:
        return asdict(self)


class _AppState:
    registry: JsonStore | None = None
    proxy_manager: ProxyManager | None = None
    start_time: float = 0.0
    _tool_calls_total: int = 0
    _tool_call_errors: int = 0
    mcp_dispatcher: object | None = None
    meta_app: object | None = None
    _stats_lock: asyncio.Lock | None = None

    _log_buffer: deque | None = None
    _log_seq: int = 0

    def _ensure_log_buffer(self) -> deque:
        if self._log_buffer is None:
            self._log_buffer = deque(maxlen=500)
        return self._log_buffer

    def append_log(self, entry: LogEntry) -> None:
        """リングバッファに追記。await を含まないため単一イベントループ内で原子的。

        id 割り当てと deque.append を同期的に連続実行し、並行タスクによる
        id 重複を防ぐ（既存 inc_tool_calls の asyncio.Lock 方式とは独立）。
        """
        self._log_seq += 1
        entry.id = self._log_seq
        self._ensure_log_buffer().append(entry)

    def snapshot_logs(self) -> list[LogEntry]:
        """全ログのコピー（新しい順は呼び出し側で反転）。"""
        return list(self._ensure_log_buffer())

    def clear_logs(self) -> None:
        """テスト・デバッグ用: バッファを空にする。"""
        self._log_buffer = deque(maxlen=500)
        self._log_seq = 0

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
