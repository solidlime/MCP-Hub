"""Ring-buffer log tests — LogEntry + _AppState log buffer."""
import time

import pytest

from mcp_hub.state import LogEntry, app_state


@pytest.fixture(autouse=True)
def clean_buffer():
    app_state.clear_logs()
    yield
    app_state.clear_logs()


def test_log_entry_to_dict_roundtrip():
    entry = LogEntry(
        ts=time.time(), type="tool_call", server="fetch", tool="fetch",
        status="success", duration_ms=12.3, args='{"url": "https://example.com"}',
    )
    d = entry.to_dict()
    assert d["type"] == "tool_call"
    assert d["server"] == "fetch"
    assert d["tool"] == "fetch"
    assert d["status"] == "success"
    assert d["duration_ms"] == 12.3
    assert d["args"] == '{"url": "https://example.com"}'
    assert d["error"] is None
    assert d["traceback"] is None


def test_append_log_assigns_monotonic_ids():
    app_state.append_log(LogEntry(ts=1.0, type="tool_call", server="-", tool="t", status="success"))
    app_state.append_log(LogEntry(ts=2.0, type="server_event", server="fetch", tool="-", status="connected"))
    entries = app_state.snapshot_logs()
    assert len(entries) == 2
    assert entries[0].id == 1
    assert entries[1].id == 2


def test_ring_buffer_drops_oldest():
    for i in range(520):
        app_state.append_log(LogEntry(ts=float(i), type="tool_call", server="-", tool="t", status="success"))
    entries = app_state.snapshot_logs()
    assert len(entries) == 500
    assert entries[0].id == 21  # ids 1..20 dropped
    assert entries[-1].id == 520
