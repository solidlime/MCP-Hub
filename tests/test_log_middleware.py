"""ToolLogMiddleware tests — server resolution + call recording."""

import pytest

from mcp_hub.middleware import ToolLogMiddleware, resolve_server
from mcp_hub.state import app_state


@pytest.fixture(autouse=True)
def clean_buffer():
    app_state.clear_logs()
    yield
    app_state.clear_logs()


class TestResolveServer:
    def test_namespaced_normal_mode(self):
        # サーバー名 fetch / fetch_tools が存在するとき最長一致
        connected = {"fetch": object(), "fetch_tools": object()}
        server, tool = resolve_server("fetch_tools_fetch", {"x": 1}, connected)
        assert server == "fetch_tools"
        assert tool == "fetch_tools_fetch"

    def test_plain_tool(self):
        connected = {"fetch": object()}
        server, tool = resolve_server("fetch", {"x": 1}, connected)
        assert server == "fetch"
        assert tool == "fetch"

    def test_meta_execute_tool_uses_arguments(self):
        connected = {"fetch": object()}
        server, tool = resolve_server(
            "execute_tool",
            {"server": "fetch", "tool_name": "fetch", "arguments": {"url": "https://x.com"}},
            connected,
        )
        assert server == "fetch"
        assert tool == "fetch"

    def test_unknown_tool_returns_dash(self):
        connected = {"fetch": object()}
        server, tool = resolve_server("some_unknown_tool", {}, connected)
        assert server == "-"
        assert tool == "some_unknown_tool"


class _DummyContext:
    """MiddlewareContext の代わり。message 属性のみ使用。"""

    def __init__(self, name, arguments=None):
        self.message = type("Msg", (), {"name": name, "arguments": arguments or {}})()


class TestToolLogMiddleware:
    def test_records_success_call(self):
        pm = type("PM", (), {"get_connected_servers": lambda self: {"fetch": object()}})()
        mw = ToolLogMiddleware(pm)

        async def call_next(ctx):
            return type("R", (), {"is_error": False, "content": []})()

        import asyncio
        asyncio.run(mw.on_call_tool(_DummyContext("fetch_fetch", {"url": "https://example.com"}), call_next))

        logs = app_state.snapshot_logs()
        assert len(logs) == 2
        assert logs[0].status == "started"
        assert logs[1].type == "tool_call"
        assert logs[1].server == "fetch"
        assert logs[1].tool == "fetch_fetch"
        assert logs[1].status == "success"
        assert logs[1].duration_ms is not None
        assert app_state.tool_calls_total == 1
        assert app_state.tool_call_errors == 0

    def test_records_error_call(self):
        pm = type("PM", (), {"get_connected_servers": lambda self: {"fetch": object()}})()
        mw = ToolLogMiddleware(pm)

        async def call_next(ctx):
            raise RuntimeError("boom")

        import asyncio
        with pytest.raises(RuntimeError):
            asyncio.run(mw.on_call_tool(_DummyContext("fetch_fetch", {}), call_next))

        logs = app_state.snapshot_logs()
        assert logs[0].status == "started"
        assert logs[1].status == "error"
        assert "boom" in logs[1].error
        assert logs[1].traceback is not None
        assert app_state.tool_call_errors == 1

    def test_records_meta_execute_tool_error_json(self):
        # meta モード: タグ拒否・ツール不在は JSON 文字列を 200 で返すだけ
        # （is_error=False）。content の JSON に error キーがあれば error 扱い。
        pm = type("PM", (), {"get_connected_servers": lambda self: {"fetch": object()}})()
        mw = ToolLogMiddleware(pm)

        async def call_next(ctx):
            return type("R", (), {
                "is_error": False,
                "content": [type("C", (), {"type": "text", "text": '{"error": "Tool not found"}'})],
            })()

        import asyncio
        asyncio.run(mw.on_call_tool(
            _DummyContext("execute_tool", {"server": "fetch", "tool_name": "missing", "arguments": {}}),
            call_next,
        ))

        logs = app_state.snapshot_logs()
        assert logs[0].status == "started"
        assert logs[1].status == "error"
        assert "Tool not found" in logs[1].error

    def test_list_tools_drops_wrap_marker_output_schema(self):
        """案D 対で: wrap-result マーカー付き outputSchema のみ tools/list で落とす。

        outputSchema を宣言したまま structuredContent を抜くと公式 SDK
        クライアントが RuntimeError で拒否するため、wrap ツール（＝常に
        content と完全二重化される対象）は list 側と対で外す。
        """
        from fastmcp.tools.base import Tool

        pm = type("PM", (), {"get_connected_servers": lambda self: {}})()
        mw = ToolLogMiddleware(pm)

        async def call_next(ctx):
            return [Tool(name="t", description="", parameters={},
                         output_schema={"type": "object",
                                        "properties": {"result": {"type": "string"}},
                                        "x-fastmcp-wrap-result": True})]

        import asyncio
        tools = asyncio.run(mw.on_list_tools(_DummyContext("list_tools"), call_next))
        assert tools[0].output_schema is None
        assert tools[0].name == "t"  # それ以外は不変

    def test_list_tools_keeps_non_wrap_output_schema(self):
        """マーカー無しの outputSchema（Proxy 経由の上流 schema 等）は保持する。

        落とすと上流ツールの正当な structuredContent がクライアント検証に
        反して RuntimeError になるため。
        """
        from fastmcp.tools.base import Tool

        pm = type("PM", (), {"get_connected_servers": lambda self: {}})()
        mw = ToolLogMiddleware(pm)
        upstream = {"type": "object", "properties": {"foo": {"type": "integer"}}}

        async def call_next(ctx):
            return [Tool(name="t", description="", parameters={},
                         output_schema=upstream)]

        import asyncio
        tools = asyncio.run(mw.on_list_tools(_DummyContext("list_tools"), call_next))
        assert tools[0].output_schema == upstream

    def test_strips_structured_content_keeps_content(self):
        """案D: 完全二重化（単一 TextContent == structured.result）のときだけ
        wire 直前に structured_content が除かれ、content は無傷で残る。

        FastMCP は str 返しツールの同一内容を content と structuredContent に
        二重で載せるため。_extract_json_error は content 内の JSON を読むので、
        content が不変であること（error 判定が生きる）も同時に検証する。
        meta には fastmcp/tools/base.py:430 が wrap 時に付与する真正印を正しく付ける。
        """
        from fastmcp.tools.base import ToolResult
        from mcp.types import TextContent

        pm = type("PM", (), {"get_connected_servers": lambda self: {"fetch": object()}})()
        mw = ToolLogMiddleware(pm)
        body = '{"error": "Tool not found"}'

        async def call_next(ctx):
            return ToolResult(
                content=[TextContent(type="text", text=body)],
                structured_content={"result": body},
                meta={"fastmcp": {"wrap_result": True}},  # wrap 真正印
            )

        import asyncio
        result = asyncio.run(mw.on_call_tool(
            _DummyContext("execute_tool", {"server": "fetch", "tool_name": "missing", "arguments": {}}),
            call_next,
        ))

        # wire に出す structured_content は無い
        assert result.structured_content is None
        # content は無傷（_extract_json_error の入力源）
        assert [b.text for b in result.content] == [body]
        # content の JSON error 判定が従来どおり機能する
        logs = app_state.snapshot_logs()
        assert logs[1].status == "error"
        assert "Tool not found" in logs[1].error

    def test_keeps_structured_content_without_wrap_marker(self):
        """反例: 形状は二重化と同一でも wrap 真正印（meta）が無ければ剥がさない。

        マーカー無しの正当な上流 schema が content ["ok"] + structured
        {"result": "ok"} の形状で返す場合、strip すると on_list_tools は
        schema を保持したまま structuredContent だけ失い、下流 SDK
        クライアント（mcp/client/session.py validate_tool_result）が
        RuntimeError になる。判定源は on_list_tools と同じ
        x-fastmcp-wrap-result の真正印でなければならない。
        """
        from fastmcp.tools.base import ToolResult
        from mcp.types import TextContent

        pm = type("PM", (), {"get_connected_servers": lambda self: {"fetch": object()}})()
        mw = ToolLogMiddleware(pm)

        async def call_next(ctx):
            return ToolResult(
                content=[TextContent(type="text", text="ok")],
                structured_content={"result": "ok"},
                meta=None,  # wrap 真正印なし
            )

        import asyncio
        result = asyncio.run(mw.on_call_tool(_DummyContext("fetch"), call_next))

        # 形状一致でもマーカー無し＝非 wrap → structured は生き残る
        assert result.structured_content == {"result": "ok"}
        assert [b.text for b in result.content] == ["ok"]

    def test_keeps_non_duplicate_structured_content(self):
        """非二重化 structured（{foo:1} + 異なる content）は素通しして生き残る。

        上流 Proxy が返す正当な structuredContent まで剥がすとデータ喪失に
        なるため、除去条件は「完全一致の二重化」1条件に限定する。
        """
        from fastmcp.tools.base import ToolResult
        from mcp.types import TextContent

        pm = type("PM", (), {"get_connected_servers": lambda self: {"fetch": object()}})()
        mw = ToolLogMiddleware(pm)

        async def call_next(ctx):
            return ToolResult(
                content=[TextContent(type="text", text='{"foo": 1}')],
                structured_content={"foo": 1},
            )

        import asyncio
        result = asyncio.run(mw.on_call_tool(_DummyContext("fetch"), call_next))

        # 非二重化 structured は無傷で残る
        assert result.structured_content == {"foo": 1}
        assert [b.text for b in result.content] == ['{"foo": 1}']

    def test_records_started_and_success(self):
        """started ログが success ログより先に、同じ server/tool で記録される。"""
        pm = type("PM", (), {"get_connected_servers": lambda self: {"fetch": object()}})()
        mw = ToolLogMiddleware(pm)

        async def call_next(ctx):
            return type("R", (), {"is_error": False, "content": []})()

        import asyncio
        asyncio.run(mw.on_call_tool(_DummyContext("fetch_fetch", {"url": "https://example.com"}), call_next))

        logs = app_state.snapshot_logs()
        assert len(logs) == 2
        assert logs[0].status == "started"
        assert logs[0].server == "fetch"
        assert logs[0].tool == "fetch_fetch"
        assert logs[0].duration_ms is None  # 完了前なので duration なし
        assert logs[1].status == "success"
        assert logs[1].server == "fetch"
        assert logs[1].tool == "fetch_fetch"

    def test_records_started_and_error(self):
        """例外時も started ログが記録され、error ログが続く。"""
        pm = type("PM", (), {"get_connected_servers": lambda self: {"fetch": object()}})()
        mw = ToolLogMiddleware(pm)

        async def call_next(ctx):
            raise RuntimeError("boom")

        import asyncio
        with pytest.raises(RuntimeError):
            asyncio.run(mw.on_call_tool(_DummyContext("fetch_fetch", {}), call_next))

        logs = app_state.snapshot_logs()
        assert len(logs) == 2
        assert logs[0].status == "started"
        assert logs[0].server == "fetch"
        assert logs[0].tool == "fetch_fetch"
        assert logs[1].status == "error"
        assert "boom" in logs[1].error


class TestIntegration:
    def test_server_event_recorded_via_on_change(self, tmp_path, monkeypatch):
        """on_change 経由でサーバーイベントがログに記録される（統合）。"""
        from fastapi.testclient import TestClient
        from mcp_hub.main import create_app

        monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
        app = create_app()
        with TestClient(app) as client:
            # create_app の lifespan で on_change が登録済み。
            # サーバーを追加（失敗するコマンド）→ spawn_failed が記録されるはず
            r = client.post("/admin/api/servers", json={
                "name": "broken",
                "config": {"command": "definitely-not-a-real-command-xyz", "args": []},
            })
            assert r.status_code == 201

            import time as _time
            _time.sleep(1.0)  # background connect 完了待ち

            logs = app_state.snapshot_logs()
            server_events = [e for e in logs if e.type == "server_event" and e.server == "broken"]
            # 接続は background task。spawn 失敗の場合は spawn_failed だが、
            # 環境によっては create_proxy が proxy を生成し list_tools の
            # 接続エラーが握りつぶされて connected (tool_count=0) になる。
            # ステータスは環境依存のため、on_change → _on_log_event の配線が
            # 働いて server_event が記録されたことのみを検証する。
            assert len(server_events) >= 1
