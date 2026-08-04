"""FullInfoMiddleware / split_qualified_name のテスト。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fastmcp.tools.base import Tool, ToolResult
from mcp.types import TextContent

from mcp_hub.full_info import FullInfoMiddleware
from mcp_hub.middleware import split_qualified_name
from mcp_hub.state import app_state


@pytest.fixture(autouse=True)
def fake_registry(monkeypatch):
    """app_state.registry を _data 付きのフェイクに差し替える。"""
    fake = SimpleNamespace(
        _data={"full_info_tools": ["fetch_fetch", "fetch_tools_get", "fetch_missing", "exa_thing"]}
    )
    monkeypatch.setattr(app_state, "registry", fake)


def _pm(connected):
    return SimpleNamespace(get_connected_servers=lambda: connected)


def _schema_fn(server, tool_name):
    # 実サーバーに存在するツールだけを模擬する（ToolIndex.get_schema 相当）。
    # 接続サーバー集合により ("fetch_tools_get") が ("fetch", "tools_get") と
    # 分解されるケースでも、fetch に tools_get が無ければ None が返る。
    exists = {("fetch", "fetch"), ("fetch_tools", "get")}
    if (server, tool_name) not in exists:
        return None
    return {
        "name": tool_name,
        "description": f"{server}/{tool_name} の説明",
        "server": server,
        "inputSchema": {"type": "object", "properties": {}},
    }


class TestSplitQualifiedName:
    def test_longest_prefix_wins(self):
        connected = {"fetch": object(), "fetch_tools": object()}
        assert split_qualified_name("fetch_tools_get", connected) == ("fetch_tools", "get")
        assert split_qualified_name("fetch_fetch", connected) == ("fetch", "fetch")

    def test_no_match_returns_dash(self):
        connected = {"fetch": object()}
        assert split_qualified_name("other_thing", connected) == ("-", "other_thing")

    def test_server_with_underscore(self):
        connected = {"my_server": object()}
        assert split_qualified_name("my_server_do_stuff", connected) == ("my_server", "do_stuff")


class TestFullInfoMiddlewareList:
    def test_appends_full_info_tools(self):
        mw = FullInfoMiddleware(_pm({"fetch": object(), "fetch_tools": object()}), _schema_fn)

        async def call_next(ctx):
            return [Tool(name="search_tools", description="meta", parameters={"type": "object"})]

        import asyncio
        tools = asyncio.run(mw.on_list_tools(SimpleNamespace(message=None), call_next))
        names = [t.name for t in tools]
        assert "search_tools" in names            # 既存ツールは保持
        assert "fetch_fetch" in names             # フル公開ツール追加
        assert "fetch_tools_get" in names         # 接頭辞最長一致で分解
        assert "fetch_missing" not in names       # schema なしは除外
        assert "exa_thing" not in names           # 未接続サーバーは除外

    def test_appends_only_connected(self):
        mw = FullInfoMiddleware(_pm({"fetch": object()}), _schema_fn)

        async def call_next(ctx):
            return []

        import asyncio
        tools = asyncio.run(mw.on_list_tools(SimpleNamespace(message=None), call_next))
        names = [t.name for t in tools]
        assert "fetch_fetch" in names
        assert "fetch_tools_get" not in names     # fetch_tools は未接続
        assert "fetch_missing" not in names


class TestFullInfoMiddlewareCall:
    def test_direct_transfer_skips_call_next(self):
        pm = _pm({"fetch": object()})
        pm.call_tool = AsyncMock(return_value=ToolResult(content=[TextContent(type="text", text="ok")]))
        mw = FullInfoMiddleware(pm, _schema_fn)
        call_next = AsyncMock(return_value="SHOULD_NOT_BE_USED")
        ctx = SimpleNamespace(message=SimpleNamespace(name="fetch_fetch", arguments={"url": "https://x.com"}))

        import asyncio
        result = asyncio.run(mw.on_call_tool(ctx, call_next))

        pm.call_tool.assert_awaited_once_with("fetch", "fetch", {"url": "https://x.com"})
        call_next.assert_not_awaited()
        assert result.content[0].text == "ok"

    def test_passthrough_non_full_info(self):
        pm = _pm({"fetch": object()})
        mw = FullInfoMiddleware(pm, _schema_fn)
        call_next = AsyncMock(return_value="PASSED")
        ctx = SimpleNamespace(message=SimpleNamespace(name="search_tools", arguments={}))

        import asyncio
        result = asyncio.run(mw.on_call_tool(ctx, call_next))

        assert result == "PASSED"
        call_next.assert_awaited_once()

    def test_error_wrapped_in_tool_result(self):
        pm = _pm({"fetch": object()})

        async def boom(*args, **kwargs):
            raise RuntimeError("downstream down")

        pm.call_tool = boom
        mw = FullInfoMiddleware(pm, _schema_fn)
        call_next = AsyncMock(return_value="SHOULD_NOT_BE_USED")
        ctx = SimpleNamespace(message=SimpleNamespace(name="fetch_fetch", arguments={}))

        import asyncio
        result = asyncio.run(mw.on_call_tool(ctx, call_next))

        assert result.is_error is True
        assert "downstream down" in result.content[0].text

    def test_unknown_server_returns_error(self):
        pm = _pm({"fetch": object()})
        mw = FullInfoMiddleware(pm, _schema_fn)
        ctx = SimpleNamespace(message=SimpleNamespace(name="exa_thing", arguments={}))

        import asyncio
        result = asyncio.run(mw.on_call_tool(ctx, AsyncMock()))

        assert result.is_error is True
        assert "not found" in result.content[0].text


class TestIntegration:
    def test_middleware_registration_order(self, tmp_path, monkeypatch):
        """meta_app の middleware チェーンで ToolLog が FullInfo より外側（先に実行）
        であること。FullInfo が on_call_tool で直接転送（call_next スキップ）しても
        ToolLog が必ず実行され、ログ・metrics が保証される。

        NOTE: FastMCP.__init__ が DereferenceRefsMiddleware を先頭に追加するため、
        絶対インデックス（chain[0]/chain[1]）ではなく相対順序で検証する。
        """
        from fastapi.testclient import TestClient

        from mcp_hub.full_info import FullInfoMiddleware
        from mcp_hub.main import create_app
        from mcp_hub.middleware import ToolLogMiddleware
        from mcp_hub.state import app_state

        monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
        app = create_app()
        with TestClient(app):
            meta_app = app_state.meta_app
            assert meta_app is not None
            chain = meta_app.mcp.middleware
            tool_log_idx = next(
                i for i, mw in enumerate(chain) if isinstance(mw, ToolLogMiddleware)
            )
            full_info_idx = next(
                i for i, mw in enumerate(chain) if isinstance(mw, FullInfoMiddleware)
            )
            assert tool_log_idx < full_info_idx  # ToolLog が外側 = 先に実行される

    def test_patch_updates_registry_data(self, tmp_path, monkeypatch):
        """PATCH /settings 後、middleware が参照する registry._data に反映される。"""
        from fastapi.testclient import TestClient

        from mcp_hub.main import create_app
        from mcp_hub.state import app_state

        monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
        app = create_app()
        with TestClient(app) as client:
            r = client.patch("/admin/api/settings", json={"full_info_tools": ["fetch_fetch"]})
            assert r.status_code == 200
            assert app_state.registry._data["full_info_tools"] == ["fetch_fetch"]
