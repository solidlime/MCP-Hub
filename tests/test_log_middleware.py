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
        assert len(logs) == 1
        assert logs[0].type == "tool_call"
        assert logs[0].server == "fetch"
        assert logs[0].tool == "fetch_fetch"
        assert logs[0].status == "success"
        assert logs[0].duration_ms is not None
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
        assert logs[0].status == "error"
        assert "boom" in logs[0].error
        assert logs[0].traceback is not None
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
        assert logs[0].status == "error"
        assert "Tool not found" in logs[0].error


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
