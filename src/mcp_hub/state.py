"""共有状態。lifespan で初期化され、admin_router から参照される。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from .metrics import MetricsCollector
from .schema_diff import DIFF_MAX as _DIFF_MAX
from .schema_diff import SchemaDiffEntry
from .tool_cache import ToolResultCache

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
    status: str          # success|error|timeout|started|connected|disconnected|spawn_failed|recovered|removed|updated
    duration_ms: float | None = None
    args: str | None = None        # マスク済み・最大2000字（tool_call のみ）
    error: str | None = None       # エラー概要・最大500字（マスク適用済み）
    traceback: str | None = None   # 例外トレースバック・最大4000字（マスク適用済み）
    id: int = 0                    # 単調増加シーケンス（append_log が付与）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EmbeddingRebuildState:
    """埋め込み設定変更に伴う索引再構築の状態（admin API / WebUI 用）。

    設定値 (requested_model) と索引に実際に効いている値 (ToolIndex.embedding_model)
    を分けて持つ。再構築中 status="running"、検索は BM25 のみで動く（フォールバック）。
    """

    status: str = "idle"  # idle | running | success | error
    trigger: str | None = None  # embedding_model | use_embeddings
    requested_model: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class _AppState:
    registry: JsonStore | None = None
    proxy_manager: ProxyManager | None = None
    start_time: float = 0.0
    _tool_calls_total: int = 0
    _tool_call_errors: int = 0
    # Prometheus 用の集計（/metrics が参照する）
    metrics: MetricsCollector = MetricsCollector()
    mcp_dispatcher: object | None = None
    meta_app: object | None = None
    # 任意機能: ツール結果キャッシュ（既定は無効。起動時に hub.config.json から構成）
    tool_cache: ToolResultCache = ToolResultCache()
    # 埋め込み設定変更に伴う索引再構築の状態（admin API / WebUI 用）
    embedding_rebuild: EmbeddingRebuildState = EmbeddingRebuildState()
    _stats_lock: asyncio.Lock | None = None

    # === グレースフルシャットダウン（ドレイン） ===
    # draining=True になったら新規リクエストは 503 で拒否し、inflight が 0 に
    # なるまで（または drain タイムアウトまで）待つ。
    draining: bool = False
    inflight: int = 0

    def enter_request(self) -> None:
        self.inflight += 1

    def leave_request(self) -> None:
        self.inflight = max(0, self.inflight - 1)

    def begin_drain(self) -> int:
        """新規受付を止め、残っている進行中リクエスト数を返す（何度呼んでも安全）。"""
        self.draining = True
        return self.inflight

    async def wait_idle(self, timeout: float) -> int:
        """inflight が 0 になるまで待つ。残件数を返す（0 以外＝タイムアウト）。"""
        deadline = time.monotonic() + max(0.0, timeout)
        while self.inflight > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        return self.inflight

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

    # === サーバー接続のヘルス履歴（API/UI 用の短期ウィンドウ） ===

    _health_history: dict[str, deque] = {}
    _health_history_max: int = 20

    def record_health(self, server: str, event: str, detail: str | None = None) -> None:
        """接続イベントを記録（サーバーごとに直近 20 件だけ保持）。"""
        hist = self._health_history.setdefault(server, deque(maxlen=self._health_history_max))
        hist.append({
            "at": time.time(),
            "event": event,
            "detail": detail[:200] if detail else None,
        })

    def health_history(self, server: str) -> list[dict]:
        """古い順のヘルス履歴（最大 20 件）。"""
        return list(self._health_history.get(server, ()))

    def clear_health_history(self, server: str | None = None) -> None:
        """履歴を消す。server=None で全サーバー分。"""
        if server is None:
            self._health_history.clear()
        else:
            self._health_history.pop(server, None)

    # === スキーマ差分履歴（task #10） ===

    _diff_buffer: deque | None = None
    _diff_seq: int = 0

    def _ensure_diff_buffer(self) -> deque:
        if self._diff_buffer is None:
            self._diff_buffer = deque(maxlen=_DIFF_MAX)
        return self._diff_buffer

    def append_diff(self, entry: SchemaDiffEntry) -> SchemaDiffEntry:
        """差分イベントを追記（bounded）。await を含まないため原子的。"""
        self._diff_seq += 1
        entry.id = self._diff_seq
        self._ensure_diff_buffer().append(entry)
        return entry

    def snapshot_diffs(self) -> list[SchemaDiffEntry]:
        """古い順のコピー（新しい順は呼び出し側で反転）。"""
        return list(self._ensure_diff_buffer())

    def clear_diffs(self) -> None:
        """テスト・デバッグ用。"""
        self._diff_buffer = deque(maxlen=_DIFF_MAX)
        self._diff_seq = 0

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
