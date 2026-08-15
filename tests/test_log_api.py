"""GET /admin/api/logs tests — server-side filtering."""

import pytest
from fastapi.testclient import TestClient

from mcp_hub.main import create_app
from mcp_hub.state import LogEntry, app_state


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_buffer():
    app_state.clear_logs()
    yield
    app_state.clear_logs()


def _seed():
    app_state.append_log(LogEntry(ts=1.0, type="tool_call", server="fetch", tool="fetch_fetch", status="success", duration_ms=5.0, args="{}"))
    app_state.append_log(LogEntry(ts=2.0, type="tool_call", server="fetch", tool="fetch_fetch", status="error", error="boom"))
    app_state.append_log(LogEntry(ts=3.0, type="server_event", server="broken", tool="-", status="spawn_failed", error="no such command"))
    app_state.append_log(LogEntry(ts=4.0, type="tool_call", server="other", tool="other_x", status="success"))


class TestGetLogs:
    def test_returns_newest_first(self, client):
        _seed()
        r = client.get("/admin/api/logs")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 4
        assert [e["id"] for e in data["entries"]] == [4, 3, 2, 1]

    def test_filter_by_type(self, client):
        _seed()
        r = client.get("/admin/api/logs", params={"type": "server_event"})
        data = r.json()
        assert data["total"] == 1
        assert data["entries"][0]["status"] == "spawn_failed"

    def test_filter_by_server(self, client):
        _seed()
        r = client.get("/admin/api/logs", params={"server": "fetch"})
        data = r.json()
        assert data["total"] == 2
        assert all(e["server"] == "fetch" for e in data["entries"])

    def test_filter_by_status(self, client):
        _seed()
        r = client.get("/admin/api/logs", params={"status": "success"})
        data = r.json()
        assert data["total"] == 2

    def test_filter_by_q(self, client):
        _seed()
        r = client.get("/admin/api/logs", params={"q": "boom"})
        data = r.json()
        assert data["total"] == 1
        assert data["entries"][0]["error"] == "boom"

    def test_limit_defaults_to_100_and_caps_at_500(self, client):
        for i in range(600):
            app_state.append_log(LogEntry(ts=float(i), type="tool_call", server="-", tool="t", status="success"))
        r = client.get("/admin/api/logs", params={"limit": 999})
        data = r.json()
        assert len(data["entries"]) <= 500
        r2 = client.get("/admin/api/logs")
        assert len(r2.json()["entries"]) <= 100
