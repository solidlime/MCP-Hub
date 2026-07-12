"""
Admin REST API integration tests.
Tests CRUD endpoints, PATCH update, enable/disable, metrics, connection info.
"""
import pytest
from fastapi.testclient import TestClient
from mcp_hub.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Create a test app with temp DB and no config seeding."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("MCP_HUB_DB_PATH", db_path)
    monkeypatch.setenv("MCP_HUB_CONFIG", "/nonexistent/config.json")
    app = create_app()
    with TestClient(app) as c:
        yield c


class TestHealth:
    def test_returns_ok(self, client):
        r = client.get("/admin/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestServerCRUD:
    def test_list_empty(self, client):
        r = client.get("/admin/api/servers")
        assert r.status_code == 200
        assert "servers" in r.json()

    def test_add_url_server(self, client):
        r = client.post("/admin/api/servers", json={
            "name": "test-http",
            "config": {"url": "http://localhost:9999", "tags": ["api"]}
        })
        assert r.status_code == 201
        assert r.json()["name"] == "test-http"

    def test_add_duplicate_is_409(self, client):
        client.post("/admin/api/servers", json={
            "name": "dup", "config": {"url": "http://localhost:9999"}
        })
        r = client.post("/admin/api/servers", json={
            "name": "dup", "config": {"url": "http://localhost:9999"}
        })
        assert r.status_code == 409

    def test_add_no_url_or_command_is_422(self, client):
        r = client.post("/admin/api/servers", json={
            "name": "bad", "config": {}
        })
        assert r.status_code == 422

    def test_delete_existing(self, client):
        client.post("/admin/api/servers", json={
            "name": "to-delete", "config": {"url": "http://localhost:9999"}
        })
        r = client.delete("/admin/api/servers/to-delete")
        assert r.status_code == 204

    def test_delete_nonexistent(self, client):
        r = client.delete("/admin/api/servers/nonexistent")
        assert r.status_code == 404

    def test_list_includes_status_and_disabled(self, client):
        client.post("/admin/api/servers", json={
            "name": "s1", "config": {"url": "http://localhost:9999"}
        })
        r = client.get("/admin/api/servers")
        servers = r.json()["servers"]
        assert len(servers) >= 1
        for s in servers:
            assert "status" in s
            assert "disabled" in s


class TestPatchUpdate:
    def test_patch_updates_tags(self, client):
        client.post("/admin/api/servers", json={
            "name": "patch-me", "config": {"url": "http://localhost:9999"}
        })
        r = client.patch("/admin/api/servers/patch-me", json={
            "tags": ["web", "api"]
        })
        assert r.status_code == 200
        assert r.json()["config"]["tags"] == ["web", "api"]

    def test_patch_disables_server(self, client):
        client.post("/admin/api/servers", json={
            "name": "sleepy", "config": {"url": "http://localhost:9999"}
        })
        r = client.patch("/admin/api/servers/sleepy", json={"disabled": True})
        assert r.status_code == 200
        assert r.json()["config"]["disabled"] is True

    def test_patch_nonexistent_is_404(self, client):
        r = client.patch("/admin/api/servers/ghost", json={"tags": ["web"]})
        assert r.status_code == 404


class TestMetrics:
    def test_returns_metrics(self, client):
        r = client.get("/admin/api/metrics")
        assert r.status_code == 200
        data = r.json()
        for key in ["uptime_seconds", "servers_registered", "servers_active", "total_tools"]:
            assert key in data


class TestConnection:
    def test_returns_connection_info(self, client):
        client.post("/admin/api/servers", json={
            "name": "conn-test", "config": {"url": "http://localhost:9999", "tags": ["web"]}
        })
        r = client.get("/admin/api/servers/conn-test/connection")
        assert r.status_code == 200
        data = r.json()
        assert "url" in data
        assert "tags" in data
        assert "web" in data["tags"]

    def test_connection_nonexistent_is_404(self, client):
        r = client.get("/admin/api/servers/ghost/connection")
        assert r.status_code == 404

    def test_connection_url_uses_urljoin(self, client):
        """URL construction uses proper urljoin, not string concatenation."""
        client.post("/admin/api/servers", json={
            "name": "url-test", "config": {"url": "http://localhost:9999"}
        })
        r = client.get("/admin/api/servers/url-test/connection")
        assert r.status_code == 200
        url = r.json()["url"]
        # Must end with /mcp, not double-slash
        assert url.endswith("/mcp")
        assert "//mcp" not in url.replace("://", "  ")


class TestTagFilter:
    """Verify tag filtering behavior via the admin API."""

    def test_server_list_includes_tags(self, client):
        """Server list response includes tag information."""
        client.post("/admin/api/servers", json={
            "name": "tagged-srv",
            "config": {"url": "http://localhost:9999", "tags": ["web", "api"]}
        })
        r = client.get("/admin/api/servers")
        servers = r.json()["servers"]
        tagged = [s for s in servers if s["name"] == "tagged-srv"]
        assert len(tagged) == 1
        assert tagged[0]["config"].get("tags") == ["web", "api"]

    def test_server_without_tags_shows_empty_list(self, client):
        """Server with no tags shows empty tags list in response."""
        client.post("/admin/api/servers", json={
            "name": "no-tags",
            "config": {"url": "http://localhost:9999"}
        })
        r = client.get("/admin/api/servers")
        servers = r.json()["servers"]
        srv = next(s for s in servers if s["name"] == "no-tags")
        assert srv["config"].get("tags", None) in (None, [])

    def test_connection_info_with_multiple_tags(self, client):
        """Connection info includes all tags in URL and example header."""
        client.post("/admin/api/servers", json={
            "name": "multi-tag",
            "config": {"url": "http://localhost:9999", "tags": ["web", "api", "db"]}
        })
        r = client.get("/admin/api/servers/multi-tag/connection")
        data = r.json()
        assert data["tags"] == ["web", "api", "db"]
        assert "tags=web,api,db" in data["url"]
        assert data["example_header"] == "X-MCP-Hub-Tags: web,api,db"
