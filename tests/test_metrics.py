"""`/metrics` (Prometheus テキスト) と MetricsCollector のテスト。"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from mcp_hub.metrics import MetricsCollector
from mcp_hub.middleware import ToolLogMiddleware
from mcp_hub.state import app_state


# === 収集・描画 ===


def test_counter_renders_labels_and_type():
    m = MetricsCollector()
    m.record_tool_call("srv", "tool_a", "success", 0.012)
    text = m.render()
    assert "# TYPE mcp_hub_tool_calls_total counter" in text
    assert 'mcp_hub_tool_calls_total{server="srv",status="success",tool="tool_a"} 1' in text


def test_counter_accumulates_same_labels():
    m = MetricsCollector()
    m.record_tool_call("srv", "tool_a", "error", 0.0)
    m.record_tool_call("srv", "tool_a", "error", 0.0)
    assert 'mcp_hub_tool_calls_total{server="srv",status="error",tool="tool_a"} 2' in m.render()


def test_histogram_renders_cumulative_buckets():
    m = MetricsCollector()
    m.record_histogram("mcp_hub_tool_call_duration_seconds", 0.02)
    text = m.render()
    assert "# TYPE mcp_hub_tool_call_duration_seconds histogram" in text
    assert 'mcp_hub_tool_call_duration_seconds_bucket{le="0.005"} 0' in text
    assert 'mcp_hub_tool_call_duration_seconds_bucket{le="0.025"} 1' in text
    assert 'mcp_hub_tool_call_duration_seconds_bucket{le="+Inf"} 1' in text
    assert "mcp_hub_tool_call_duration_seconds_sum 0.02" in text
    assert "mcp_hub_tool_call_duration_seconds_count 1" in text


def test_label_values_are_escaped():
    m = MetricsCollector()
    m.record_counter("mcp_hub_health_check_failures_total", {"server": 'a"b\\c', "reason": "timeout"})
    assert 'server="a\\"b\\\\c"' in m.render()


def test_search_hit_and_miss_are_counted():
    m = MetricsCollector()
    m.record_search(3)
    m.record_search(0)
    text = m.render()
    assert "mcp_hub_search_queries_total 2" in text
    assert "mcp_hub_search_hits_total 1" in text
    assert "mcp_hub_search_misses_total 1" in text


def test_index_rebuild_failure_counter():
    m = MetricsCollector()
    m.record_index_rebuild(1.5, failed_servers=2)
    m.record_index_rebuild(0.5, failed_servers=0)
    text = m.render()
    assert "mcp_hub_index_rebuilds_total 2" in text
    assert "mcp_hub_index_rebuild_failures_total 1" in text
    assert "mcp_hub_index_build_duration_seconds_count 2" in text


def test_gauges_render_grouped():
    m = MetricsCollector()
    text = m.render(
        [
            ("mcp_hub_servers_active", {}, 2.0),
            ("mcp_hub_server_up", {"server": "a"}, 1.0),
            ("mcp_hub_server_up", {"server": "b"}, 0.0),
        ]
    )
    assert "# TYPE mcp_hub_servers_active gauge" in text
    assert "mcp_hub_servers_active 2" in text
    assert 'mcp_hub_server_up{server="a"} 1' in text
    assert 'mcp_hub_server_up{server="b"} 0' in text
    # HELP/TYPE はゲージ名ごとに 1 回だけ
    assert text.count("# TYPE mcp_hub_server_up gauge") == 1


# === middleware からの記録 ===


class _DummyContext:
    def __init__(self, name, arguments=None):
        self.message = type("Msg", (), {"name": name, "arguments": arguments or {}})()


def _count(metric_line_prefix: str) -> int:
    """現在の exposition から指定シリーズの値を読む（テスト間の累積に依存しない）。"""
    for line in app_state.metrics.render().splitlines():
        if line.startswith(metric_line_prefix):
            return int(float(line.rsplit(" ", 1)[1]))
    return 0


def test_middleware_records_success_metric():
    pm = type("PM", (), {"get_connected_servers": lambda self: {"msrv": object()}})()
    mw = ToolLogMiddleware(pm)
    prefix = 'mcp_hub_tool_calls_total{server="msrv",status="success",tool="msrv_ok"}'

    async def call_next(ctx):
        return type("R", (), {"is_error": False, "content": []})()

    before = _count(prefix)
    asyncio.run(mw.on_call_tool(_DummyContext("msrv_ok", {}), call_next))
    assert _count(prefix) == before + 1


def test_middleware_records_error_metric():
    pm = type("PM", (), {"get_connected_servers": lambda self: {"msrv": object()}})()
    mw = ToolLogMiddleware(pm)
    prefix = 'mcp_hub_tool_calls_total{server="msrv",status="error",tool="msrv_boom"}'

    async def call_next(ctx):
        raise RuntimeError("boom")

    before = _count(prefix)
    with pytest.raises(RuntimeError):
        asyncio.run(mw.on_call_tool(_DummyContext("msrv_boom", {}), call_next))
    assert _count(prefix) == before + 1


# === /metrics エンドポイント ===


@pytest.fixture
def client(tmp_path, monkeypatch):
    # main は他の作業と独立にロードできるよう遅延 import（収集時の相互依存を避ける）
    from mcp_hub.main import create_app

    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_metrics_endpoint_returns_prometheus_text(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "# TYPE mcp_hub_tool_calls_total counter" in body
    assert "# TYPE mcp_hub_uptime_seconds gauge" in body
    assert "# TYPE mcp_hub_servers_active gauge" in body
    assert "mcp_hub_tools_indexed" in body


def test_metrics_endpoint_reports_tool_calls(client):
    app_state.metrics.record_tool_call("esrv", "esrv_x", "success", 0.01)
    body = client.get("/metrics").text
    assert 'mcp_hub_tool_calls_total{server="esrv",status="success",tool="esrv_x"} ' in body


def test_admin_json_metrics_still_works(client):
    r = client.get("/admin/api/metrics")
    assert r.status_code == 200
    assert "tool_calls_total" in r.json()
