"""プロセス内メトリクス。`GET /metrics` で Prometheus テキスト形式として公開する。

イベント発生時に record_* で加算し、スクレイプ時に render() で exposition を
組み立てる。ゲージ（起動時間・サーバー数・接続状態など）はスクレイプ時点の
スナップショットなので render() の引数で渡す（collector 側では保持しない）。

インクリメントは同期のみ。asyncio の単一イベントループ内で原子的に完了するため
ロックは持たない（state.append_log と同じ前提）。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping

# ツール呼び出し・索引構築のレイテンシ用バケット（秒）。
# ツール呼び出しは通常数十 ms〜数秒、索引構築は数秒〜数十秒に収まる。
_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0,
)

_HELP: dict[str, str] = {
    "mcp_hub_tool_calls_total": "Tool calls by server, tool and status.",
    "mcp_hub_tool_call_duration_seconds": "Tool call latency in seconds.",
    "mcp_hub_search_queries_total": "search_tools queries.",
    "mcp_hub_search_hits_total": "search_tools queries that returned a result.",
    "mcp_hub_search_misses_total": "search_tools queries that returned nothing.",
    "mcp_hub_index_rebuilds_total": "Tool index rebuilds.",
    "mcp_hub_index_rebuild_failures_total": (
        "Tool index rebuilds with at least one server whose tools could not be fetched."
    ),
    "mcp_hub_index_build_duration_seconds": "Tool index rebuild duration in seconds.",
    "mcp_hub_embedding_cache_hits_total": "Document embeddings reused from the on-disk cache.",
    "mcp_hub_embedding_cache_misses_total": "Document embeddings computed (not cached).",
    "mcp_hub_health_check_failures_total": "Health-check failures by server and reason.",
    "mcp_hub_reconnects_total": "Successful recoveries of disconnected servers.",
    "mcp_hub_uptime_seconds": "Seconds since the hub process started.",
    "mcp_hub_servers_registered": "Servers in the registry.",
    "mcp_hub_servers_active": "Currently connected servers.",
    "mcp_hub_tools_total": "Cached tool count across all servers.",
    "mcp_hub_tools_indexed": "Tools in the meta-mode search index.",
    "mcp_hub_server_up": "1 when the server is connected, else 0.",
}


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels) + "}"


def _fmt(value: float) -> str:
    # 整数値は "5.0" ではなく "5" で出す（exposition を読みやすく保つ）
    return str(int(value)) if float(value).is_integer() else repr(round(value, 6))


class _Histogram:
    __slots__ = ("buckets", "count", "total")

    def __init__(self) -> None:
        self.buckets = [0] * len(_BUCKETS)
        self.total = 0.0
        self.count = 0

    def observe(self, value: float) -> None:
        self.total += value
        self.count += 1
        for i, bound in enumerate(_BUCKETS):
            if value <= bound:
                self.buckets[i] += 1


class MetricsCollector:
    """ツール呼び出し・検索・索引再構築・接続状態の集計。"""

    def __init__(self) -> None:
        self._counters: dict[str, dict[tuple[tuple[str, str], ...], float]] = defaultdict(dict)
        self._histograms: dict[str, _Histogram] = {}

    def record_counter(
        self,
        name: str,
        labels: Mapping[str, str] | None = None,
        amount: float = 1.0,
    ) -> None:
        key = tuple(sorted((labels or {}).items()))
        series = self._counters[name]
        series[key] = series.get(key, 0.0) + amount

    def record_histogram(self, name: str, value: float) -> None:
        hist = self._histograms.get(name)
        if hist is None:
            hist = self._histograms[name] = _Histogram()
        hist.observe(value)

    # ── 主要イベント（呼び出し側の意図が読めるよう専用メソッドで包む） ──

    def record_tool_call(
        self, server: str, tool: str, status: str, duration_s: float
    ) -> None:
        self.record_counter(
            "mcp_hub_tool_calls_total",
            {"server": server, "tool": tool, "status": status},
        )
        self.record_histogram(
            "mcp_hub_tool_call_duration_seconds", max(0.0, duration_s)
        )

    def record_search(self, result_count: int) -> None:
        self.record_counter("mcp_hub_search_queries_total")
        if result_count:
            self.record_counter("mcp_hub_search_hits_total")
        else:
            self.record_counter("mcp_hub_search_misses_total")

    def record_index_rebuild(self, duration_s: float, failed_servers: int) -> None:
        self.record_counter("mcp_hub_index_rebuilds_total")
        if failed_servers:
            self.record_counter("mcp_hub_index_rebuild_failures_total")
        self.record_histogram(
            "mcp_hub_index_build_duration_seconds", max(0.0, duration_s)
        )

    def record_health_failure(self, server: str, reason: str) -> None:
        self.record_counter(
            "mcp_hub_health_check_failures_total",
            {"server": server, "reason": reason},
        )

    def record_reconnect(self) -> None:
        self.record_counter("mcp_hub_reconnects_total")

    def record_embedding_cache(self, hits: int, misses: int) -> None:
        self.record_counter("mcp_hub_embedding_cache_hits_total", amount=hits)
        self.record_counter("mcp_hub_embedding_cache_misses_total", amount=misses)

    def render(
        self, gauges: Iterable[tuple[str, Mapping[str, str], float]] = ()
    ) -> str:
        """Prometheus text exposition format (version 0.0.4) を返す。"""
        out: list[str] = []

        def header(name: str, kind: str) -> None:
            out.append(f"# HELP {name} {_HELP.get(name, name)}")
            out.append(f"# TYPE {name} {kind}")

        for name in sorted(self._counters):
            header(name, "counter")
            for series_labels, series_value in sorted(self._counters[name].items()):
                out.append(f"{name}{_labels(series_labels)} {_fmt(series_value)}")

        for name in sorted(self._histograms):
            hist = self._histograms[name]
            header(name, "histogram")
            for bound, count in zip(_BUCKETS, hist.buckets):
                out.append(f'{name}_bucket{{le="{bound}"}} {count}')
            out.append(f'{name}_bucket{{le="+Inf"}} {hist.count}')
            out.append(f"{name}_sum {_fmt(hist.total)}")
            out.append(f"{name}_count {hist.count}")

        grouped: dict[str, list[tuple[tuple[tuple[str, str], ...], float]]] = (
            defaultdict(list)
        )
        for gauge_name, gauge_labels, gauge_value in gauges:
            grouped[gauge_name].append(
                (tuple(sorted(gauge_labels.items())), gauge_value)
            )
        for gauge_name in sorted(grouped):
            header(gauge_name, "gauge")
            for g_labels, g_value in sorted(grouped[gauge_name]):
                out.append(
                    f"{gauge_name}{_labels(g_labels)} {_fmt(g_value)}"
                )

        return "\n".join(out) + "\n"
