"""
Admin REST API integration tests.
Tests CRUD endpoints, PATCH update, enable/disable, metrics, connection info.
"""
import pytest
from fastapi.testclient import TestClient
from mcp_hub.main import create_app
from mcp_hub.state import app_state


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Create a test app with temp data dir and no config seeding."""
    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
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


class TestResourcesPrompts:
    """Tests for GET /servers/{name}/resources, /prompts, /resource-templates."""

    def _inject_mock_proxy(self, name: str):
        """Inject a mock FastMCPProxy into the proxy manager for testing."""
        from unittest.mock import AsyncMock
        mock = AsyncMock()
        mock.list_resources.return_value = []
        mock.list_prompts.return_value = []
        mock.list_resource_templates.return_value = []
        if app_state.proxy_manager is not None:
            app_state.proxy_manager._proxies[name] = mock
        return mock

    # --- 404: nonexistent server ---

    def test_resources_nonexistent_is_404(self, client):
        r = client.get("/admin/api/servers/nonexistent/resources")
        assert r.status_code == 404

    def test_prompts_nonexistent_is_404(self, client):
        r = client.get("/admin/api/servers/nonexistent/prompts")
        assert r.status_code == 404

    def test_resource_templates_nonexistent_is_404(self, client):
        r = client.get("/admin/api/servers/nonexistent/resource-templates")
        assert r.status_code == 404

    # --- 200: connected server returns data ---

    def test_resources_connected_server_returns_list(self, client):
        mock = self._inject_mock_proxy("res-srv")
        from mcp.types import Resource
        mock.list_resources.return_value = [
            Resource(uri="file:///test.txt", name="test.txt", description="A test file"),
        ]
        r = client.get("/admin/api/servers/res-srv/resources")
        assert r.status_code == 200
        data = r.json()
        assert data == {
            "resources": [
                {"uri": "file:///test.txt", "name": "test.txt", "description": "A test file"},
            ]
        }

    def test_resources_connected_server_empty(self, client):
        self._inject_mock_proxy("empty-srv")
        r = client.get("/admin/api/servers/empty-srv/resources")
        assert r.status_code == 200
        assert r.json() == {"resources": []}

    def test_prompts_connected_server_returns_list(self, client):
        mock = self._inject_mock_proxy("prompt-srv")
        from mcp.types import Prompt
        mock.list_prompts.return_value = [
            Prompt(name="greet", description="A greeting prompt"),
        ]
        r = client.get("/admin/api/servers/prompt-srv/prompts")
        assert r.status_code == 200
        data = r.json()
        assert data == {
            "prompts": [
                {"name": "greet", "description": "A greeting prompt"},
            ]
        }

    def test_resource_templates_connected_server_returns_list(self, client):
        mock = self._inject_mock_proxy("tmpl-srv")
        from mcp.types import ResourceTemplate
        mock.list_resource_templates.return_value = [
            ResourceTemplate(uriTemplate="file:///{path}", name="file", description="File access"),
        ]
        r = client.get("/admin/api/servers/tmpl-srv/resource-templates")
        assert r.status_code == 200
        data = r.json()
        assert data == {
            "resource_templates": [
                {"uriTemplate": "file:///{path}", "name": "file", "description": "File access"},
            ]
        }


class TestSettings:
    """Settings API (meta_mode toggle)."""

    def test_get_settings_default(self, client):
        """GET /admin/api/settings returns meta_mode=false by default."""
        r = client.get("/admin/api/settings")
        assert r.status_code == 200
        assert r.json() == {"meta_mode": False}

    def test_patch_settings_enable(self, client):
        """PATCH meta_mode=true persists and reflects in subsequent GET."""
        r = client.patch("/admin/api/settings", json={"meta_mode": True})
        assert r.status_code == 200
        assert r.json() == {"meta_mode": True}

        r2 = client.get("/admin/api/settings")
        assert r2.json() == {"meta_mode": True}

    def test_patch_settings_disable(self, client):
        """PATCH meta_mode=false persists."""
        # Enable first
        client.patch("/admin/api/settings", json={"meta_mode": True})
        # Then disable
        r = client.patch("/admin/api/settings", json={"meta_mode": False})
        assert r.status_code == 200
        assert r.json() == {"meta_mode": False}

        r2 = client.get("/admin/api/settings")
        assert r2.json() == {"meta_mode": False}

    def test_patch_settings_invalid_value(self, client):
        """PATCH with non-bool value like int 1 converts via bool()."""
        r = client.patch("/admin/api/settings", json={"meta_mode": 1})
        assert r.status_code == 200
        # bool(1) is True
        assert r.json() == {"meta_mode": True}

    def test_patch_settings_empty_body(self, client):
        """PATCH with empty body doesn't crash, returns current settings."""
        # get current first
        r0 = client.get("/admin/api/settings")
        current = r0.json()

        r = client.patch("/admin/api/settings", json={})
        assert r.status_code == 200
        assert r.json() == current
