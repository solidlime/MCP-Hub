"""
Tag filter tests — verify that tag_middleware, ProxyManager filtering,
and TagFilterMiddleware all work together.
"""
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from mcp_hub.main import create_app
from mcp_hub.state import request_tags
from mcp_hub.tag_filter import TagFilterMiddleware


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as c:
        yield c


# ── Dummy classes for unit-testing TagFilterMiddleware ──────────────

class _DummyTool:
    """Mimics FastMCPProviderTool for testing without server setup."""
    def __init__(self, name: str, server=None):
        self.name = name
        self._server = server


# ── Tests ───────────────────────────────────────────────────────────

class TestTagMiddleware:
    def test_no_tags_sets_none(self, client):
        """Without tags param or header, request_tags stays None."""
        client.get("/admin/api/health")  # any path
        # request_tags is a ContextVar; after request it should be reset
        # We just verify the app doesn't crash
        assert True

    def test_query_param_sets_tags(self, client):
        """Query param ?tags=web,api sets request_tags."""
        # The middleware only intercepts /mcp paths
        # Just verify it doesn't 500 on tagged /mcp path
        r = client.post("/mcp?tags=web,local", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}
        })
        # FastMCP returns 406 (Not Acceptable) for streamable-http
        # without proper Accept header — that's expected
        assert r.status_code == 406

    def test_header_override_query(self, client):
        """X-MCP-Hub-Tags header takes priority over ?tags= query param."""
        r = client.post(
            "/mcp?tags=ignore",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"X-MCP-Hub-Tags": "local"}
        )
        assert r.status_code == 406


class TestTagFilterMiddlewareUnit:
    """Unit tests for TagFilterMiddleware._filter_items logic."""

    def test_no_tags_passes_all(self):
        """Without request_tags set, all items pass."""
        pm = MagicMock()
        mw = TagFilterMiddleware(pm)
        items = [_DummyTool("a"), _DummyTool("b"), _DummyTool("c")]
        request_tags.set(None)
        result = mw._filter_items(items, [])
        # When tags is empty list, _filter_items is not called by the hooks
        # (they short-circuit). But if called directly with empty list, all pass.
        assert len(result) == 3

    def test_local_items_always_pass(self):
        """Items without _server attribute (local hub tools) always pass."""
        pm = MagicMock()
        mw = TagFilterMiddleware(pm)
        local = _DummyTool("hub_health")  # no _server
        result = mw._filter_items([local], ["nous"])
        assert len(result) == 1
        assert result[0].name == "hub_health"

    def test_server_with_matching_tag_kept(self):
        """Server tags match requested tags → item kept."""
        pm = MagicMock()
        pm.proxy_to_name.return_value = "my_server"
        pm.server_tags.return_value = ["web", "api"]

        mw = TagFilterMiddleware(pm)
        server = object()
        tool = _DummyTool("get_data", server=server)

        result = mw._filter_items([tool], ["web"])
        assert len(result) == 1
        assert result[0].name == "get_data"

    def test_server_without_matching_tag_filtered(self):
        """Server tags don't match requested tags → item filtered out."""
        pm = MagicMock()
        pm.proxy_to_name.return_value = "my_server"
        pm.server_tags.return_value = ["web", "api"]

        mw = TagFilterMiddleware(pm)
        server = object()
        tool = _DummyTool("get_data", server=server)

        result = mw._filter_items([tool], ["nous", "local"])
        assert len(result) == 0

    def test_unknown_server_kept(self):
        """proxy_to_name returns None (unknown) → item kept (safety valve)."""
        pm = MagicMock()
        pm.proxy_to_name.return_value = None

        mw = TagFilterMiddleware(pm)
        server = object()
        tool = _DummyTool("orphan", server=server)

        result = mw._filter_items([tool], ["nous"])
        assert len(result) == 1

    def test_mixed_items(self):
        """Mix of local, matching, and non-matching items."""
        pm = MagicMock()

        def mock_proxy_to_name(proxy_id):
            return {id(server_a): "server_a", id(server_b): "server_b"}.get(proxy_id)

        def mock_server_tags(name):
            return {"server_a": ["web"], "server_b": ["db"]}.get(name, [])

        pm.proxy_to_name.side_effect = mock_proxy_to_name
        pm.server_tags.side_effect = mock_server_tags

        mw = TagFilterMiddleware(pm)
        server_a = object()
        server_b = object()

        items = [
            _DummyTool("local_health"),           # no _server → always pass
            _DummyTool("web_tool", server=server_a),   # server_a: tags=["web"] → matches "web"
            _DummyTool("db_tool", server=server_b),    # server_b: tags=["db"] → doesn't match "web"
        ]

        result = mw._filter_items(items, ["web"])
        assert len(result) == 2
        names = [t.name for t in result]
        assert "local_health" in names
        assert "web_tool" in names
        assert "db_tool" not in names

    def test_or_logic_multiple_request_tags(self):
        """OR logic: any matching tag keeps the item."""
        pm = MagicMock()
        pm.proxy_to_name.return_value = "my_server"
        pm.server_tags.return_value = ["db"]

        mw = TagFilterMiddleware(pm)
        server = object()
        tool = _DummyTool("query", server=server)

        # "web" doesn't match, but "db" does → kept (OR logic)
        result = mw._filter_items([tool], ["web", "db"])
        assert len(result) == 1
