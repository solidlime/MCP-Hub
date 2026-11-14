"""Schema diff detection (task #10).

Baseline on first snapshot, add/remove/change detection for
tools/resources/prompts, bounded history, admin API surface.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from mcp_hub.proxy_manager import ProxyManager
from mcp_hub.state import SchemaDiffEntry, app_state


def _tool(name, desc="", params=None):
    return type("T", (), {"name": name, "description": desc, "parameters": params or {}})()


def _resource(uri, name="", desc=""):
    return type("R", (), {"uri": uri, "name": name, "description": desc, "mime_type": None})()


def _prompt(name, desc="", arguments=None):
    return type("P", (), {"name": name, "description": desc, "arguments": arguments or []})()


def _proxy(tools=(), resources=(), prompts=()):
    p = AsyncMock()
    p.list_tools = AsyncMock(return_value=list(tools))
    p.list_resources = AsyncMock(return_value=list(resources))
    p.list_prompts = AsyncMock(return_value=list(prompts))
    return p


@pytest.fixture(autouse=True)
def clean_state():
    app_state.clear_diffs()
    app_state.clear_logs()
    yield
    app_state.clear_diffs()
    app_state.clear_logs()


@pytest.fixture
def pm():
    mcp = type("MCP", (), {"mount": lambda self, p, namespace=None: None})()
    registry = AsyncMock()
    registry.list_servers = AsyncMock(return_value=[])
    return ProxyManager(mcp, registry)


class TestBaseline:
    def test_first_snapshot_records_no_event(self, pm):
        proxy = _proxy(tools=[_tool("a")])
        changes = asyncio.run(pm._check_schema_diff("srv", proxy, "connect"))
        assert changes is None
        assert app_state.snapshot_diffs() == []
        assert "a" in pm._schema_snapshots["srv"]["tools"]

    def test_connect_flow_records_baseline(self, pm):
        pm._server_configs["srv"] = {"command": "x", "args": []}
        fake_proxy = _proxy(tools=[_tool("a")])
        with patch.object(pm, "_create_proxy", AsyncMock(return_value=(fake_proxy, AsyncMock()))):
            asyncio.run(pm._connect_and_mount("srv", {"command": "x", "args": []}))
        assert app_state.snapshot_diffs() == []
        assert "a" in pm._schema_snapshots["srv"]["tools"]


class TestToolDiff:
    def test_added_removed_changed(self, pm):
        asyncio.run(pm._check_schema_diff("srv", _proxy(tools=[_tool("a"), _tool("b")]), "connect"))
        changes = asyncio.run(pm._check_schema_diff(
            "srv",
            _proxy(tools=[_tool("a", desc="new"), _tool("c", params={"type": "object"})]),
            "refresh",
        ))
        assert changes["tools"] == {"added": ["c"], "removed": ["b"], "changed": ["a"]}
        entries = app_state.snapshot_diffs()
        assert len(entries) == 1
        assert entries[0].server == "srv"
        assert entries[0].trigger == "refresh"
        assert entries[0].changes["tools"]["changed"] == ["a"]

    def test_noop_refresh_records_nothing(self, pm):
        asyncio.run(pm._check_schema_diff("srv", _proxy(tools=[_tool("a")]), "connect"))
        assert asyncio.run(pm._check_schema_diff("srv", _proxy(tools=[_tool("a")]), "refresh")) is None
        assert app_state.snapshot_diffs() == []

    def test_input_schema_change_is_detected(self, pm):
        asyncio.run(pm._check_schema_diff(
            "srv", _proxy(tools=[_tool("a", params={"properties": {"q": {"type": "string"}}})]), "connect",
        ))
        changes = asyncio.run(pm._check_schema_diff(
            "srv", _proxy(tools=[_tool("a", params={"properties": {"q": {"type": "number"}}})]), "refresh",
        ))
        assert changes["tools"]["changed"] == ["a"]

    def test_empty_tool_list_does_not_mass_remove(self, pm):
        asyncio.run(pm._check_schema_diff("srv", _proxy(tools=[_tool("a")]), "connect"))
        assert asyncio.run(pm._check_schema_diff("srv", _proxy(tools=[]), "refresh")) is None
        assert app_state.snapshot_diffs() == []


class TestResourcePromptDiff:
    def test_resources_and_prompts_detected(self, pm):
        asyncio.run(pm._check_schema_diff(
            "srv",
            _proxy(resources=[_resource("file:///a")], prompts=[_prompt("p")]),
            "connect",
        ))
        changes = asyncio.run(pm._check_schema_diff(
            "srv",
            _proxy(resources=[_resource("file:///b")], prompts=[_prompt("p", arguments=[{"name": "x"}])]),
            "refresh",
        ))
        assert changes["resources"] == {"added": ["file:///b"], "removed": ["file:///a"], "changed": []}
        assert changes["prompts"] == {"added": [], "removed": [], "changed": ["p"]}

    def test_unsupported_resources_do_not_raise(self, pm):
        proxy = AsyncMock()
        proxy.list_tools = AsyncMock(return_value=[_tool("a")])
        proxy.list_resources = AsyncMock(side_effect=RuntimeError("not supported"))
        proxy.list_prompts = AsyncMock(side_effect=RuntimeError("not supported"))
        changes = asyncio.run(pm._check_schema_diff("srv", proxy, "connect"))
        assert changes is None
        assert "a" in pm._schema_snapshots["srv"]["tools"]


class TestPeriodicRefresh:
    def test_list_tools_cache_miss_detects_change_and_notifies(self, pm):
        events = []
        pm.on_change(lambda name, event, detail=None: events.append((name, event, detail)))
        asyncio.run(pm._check_schema_diff("srv", _proxy(tools=[_tool("a")]), "connect"))

        # cache miss → upstream re-list → diff against snapshot
        asyncio.run(pm.list_tools_for_server("srv", _proxy(tools=[_tool("a"), _tool("b")])))
        schema_events = [e for e in events if e[1] == "schema_changed"]
        assert len(schema_events) == 1
        assert schema_events[0][2]["changes"]["tools"]["added"] == ["b"]

    def test_list_tools_cache_hit_does_not_diff(self, pm):
        asyncio.run(pm._check_schema_diff("srv", _proxy(tools=[_tool("a")]), "connect"))
        proxy = _proxy(tools=[_tool("a")])
        asyncio.run(pm.list_tools_for_server("srv", proxy))
        proxy.list_tools = AsyncMock(return_value=[_tool("a"), _tool("b")])
        asyncio.run(pm.list_tools_for_server("srv", proxy))  # cache hit
        assert app_state.snapshot_diffs() == []


class TestBoundedHistory:
    def test_history_is_bounded(self, pm):
        from mcp_hub.state import _DIFF_MAX
        for i in range(_DIFF_MAX + 20):
            app_state.append_diff(SchemaDiffEntry(
                ts=float(i), server="s", trigger="refresh",
                changes={"tools": {"added": [f"t{i}"], "removed": [], "changed": []}},
            ))
        entries = app_state.snapshot_diffs()
        assert len(entries) == _DIFF_MAX
        assert entries[-1].changes["tools"]["added"] == [f"t{_DIFF_MAX + 19}"]


class TestAdminApi:
    def test_route_registered(self):
        from mcp_hub import admin_router
        paths = {getattr(r, "path", "") for r in admin_router.router.routes}
        assert "/admin/api/schema-diffs" in paths

    def test_endpoint_returns_entries_newest_first(self):
        from mcp_hub import admin_router
        app_state.append_diff(SchemaDiffEntry(
            ts=1.0, server="a", trigger="connect",
            changes={"tools": {"added": ["x"], "removed": [], "changed": []}},
        ))
        app_state.append_diff(SchemaDiffEntry(
            ts=2.0, server="b", trigger="refresh",
            changes={"prompts": {"added": [], "removed": ["p"], "changed": []}},
        ))
        data = asyncio.run(admin_router.schema_diffs())
        assert data["total"] == 2
        assert [e["server"] for e in data["entries"]] == ["b", "a"]

        filtered = asyncio.run(admin_router.schema_diffs(server="a"))
        assert filtered["total"] == 1
        assert filtered["entries"][0]["changes"]["tools"]["added"] == ["x"]
