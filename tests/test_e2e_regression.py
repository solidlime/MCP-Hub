"""E2E / 回帰テスト (kanban task #12).

HTTP 管理 API・normal/meta ルーティング・stdio バックエンド越しの
search → get_schema → execute_tool を「実物のアプリ／実プロセス」で拘束する。
個々の機能の単体テスト（test_tool_cache.py 等）は各ファイルの責務なので、
ここでは *経路が繋がっていること* と *契約の形* だけを見る。

このファイルは `TEST_FILE=tests/test_e2e_regression.py ./scripts/run-tests.sh`
で単独実行できる（他ファイルと同じ前提: conftest の bundled config 差し替え）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client, FastMCP

from mcp_hub.main import create_app
from mcp_hub.meta_provider import create_meta_app
from mcp_hub.proxy_manager import ProxyManager
from mcp_hub.state import app_state

STDIO_SERVER = os.path.join(
    os.path.dirname(__file__), "test_servers", "stdio_echo_server.py"
)
_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _rpc(
    client: TestClient, path: str, method: str, params: dict | None = None
) -> dict:
    """Streamable HTTP へ 1 回 JSON-RPC を投げ、SSE/JSON どちらでも結果を返す。

    ステートフルなセッションなので initialize → notifications/initialized で
    セッションを開いてから本リクエストを送る。
    """
    init = client.post(
        path,
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "e2e", "version": "0"},
            },
            "id": "init",
        },
        headers=_MCP_HEADERS,
    )
    assert init.status_code == 200, init.text
    headers = dict(_MCP_HEADERS)
    sid = init.headers.get("mcp-session-id")
    if sid:
        headers["mcp-session-id"] = sid
        client.post(
            path,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=headers,
        )
    r = client.post(
        path,
        json={"jsonrpc": "2.0", "method": method, "params": params or {}, "id": "e2e"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    if r.text.lstrip().startswith("{"):
        return r.json()
    data = "".join(line[6:] for line in r.text.split("\n") if line.startswith("data: "))
    import json as _json

    return _json.loads(data)


def _tool_names(result: dict) -> list[str]:
    return [t["name"] for t in result["result"]["tools"]]


# ── HTTP アプリ全体（管理 API / ルーティング） ────────────────────────────────


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
    app_state.clear_logs()
    app_state.clear_diffs()
    app_state.tool_cache.clear()
    with TestClient(create_app()) as client:
        yield client
    app_state.draining = False
    app_state.inflight = 0
    app_state.clear_logs()
    app_state.clear_diffs()


class TestAdminSurface:
    def test_health_and_metrics_shape(self, hub):
        health = hub.get("/admin/api/health").json()
        assert health["status"] == "ok"
        assert isinstance(health["servers"], int)
        assert "embedding_status" in health

        metrics = hub.get("/admin/api/metrics").json()
        assert metrics["servers_registered"] == 0
        assert metrics["servers_active"] == 0
        assert "tool_cache" in metrics
        assert metrics["uptime_seconds"] >= 0

    def test_settings_expose_embedding_rebuild_and_cache(self, hub):
        s = hub.get("/admin/api/settings").json()
        assert s["meta_mode"] is True  # bundled default (conftest)
        assert s["embedding_rebuild"]["status"] in {
            "idle",
            "running",
            "success",
            "error",
        }
        assert s["tool_cache"]["stats"]["hits"] == 0

    def test_embedding_model_validation_and_rebuild_snapshot(self, hub):
        bad = hub.patch(
            "/admin/api/settings/embedding-model", json={"embedding_model": "a/../b"}
        )
        assert bad.status_code == 422

        ok = hub.patch(
            "/admin/api/settings/embedding-model",
            json={"embedding_model": "intfloat/multilingual-e5-small"},
        )
        assert ok.status_code == 200
        assert ok.json()["rebuild"]["status"] in {"running", "success", "error"}
        # 再構築の進捗は GET /settings 側でも同じ形で見える
        s = hub.get("/admin/api/settings").json()
        assert "status" in s["embedding_rebuild"]

    def test_tool_cache_toggle_and_clear(self, hub):
        r = hub.patch(
            "/admin/api/settings",
            json={"tool_cache": {"enabled": True, "ttl_seconds": 30}},
        )
        assert r.status_code == 200
        assert r.json()["tool_cache"]["enabled"] is True

        cleared = hub.delete("/admin/api/cache").json()
        assert cleared["cleared"] == 0

    def test_config_reload_applies_and_rejects_missing_file(self, hub, tmp_path):
        ok = hub.post("/admin/api/reload")
        assert ok.status_code == 200
        body = ok.json()
        assert body["ok"] is True
        assert isinstance(body["servers"], dict)
        assert set(body["servers"]) == {"added", "removed", "updated", "unchanged"}
        assert "settings" in body

        # 設定ファイルが消えている → 全サーバー削除として適用せず 500
        (tmp_path / "hub.config.json").unlink()
        broken = hub.post("/admin/api/reload")
        assert broken.status_code == 500
        assert "見つかりません" in broken.json()["detail"]

    def test_schema_diff_endpoint_records_and_filters(self, hub):
        pm = app_state.proxy_manager

        def tool(name: str):
            return type("T", (), {"name": name, "description": "", "parameters": {}})

        # 1 回目はベースライン（差分なし）、2 回目で added が 1 件
        asyncio.run(
            pm._check_schema_diff("srv-a", MagicMock(), "refresh", [tool("t1")])
        )
        asyncio.run(
            pm._check_schema_diff(
                "srv-a", MagicMock(), "refresh", [tool("t1"), tool("t2")]
            )
        )
        asyncio.run(
            pm._check_schema_diff("srv-b", MagicMock(), "refresh", [tool("t1")])
        )
        asyncio.run(
            pm._check_schema_diff(
                "srv-b", MagicMock(), "refresh", [tool("t1"), tool("t2")]
            )
        )

        all_entries = hub.get("/admin/api/schema-diffs").json()
        assert all_entries["total"] == 2
        only_a = hub.get("/admin/api/schema-diffs?server=srv-a").json()
        assert [e["server"] for e in only_a["entries"]] == ["srv-a"]

    def test_discovery_search_returns_results_shape(self, hub):
        r = hub.get(
            "/admin/api/discovery/search", params={"query": "echo", "kind": "tool"}
        )
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["results"], list)
        assert "query" in body or "total" in body

    def test_per_server_limits_patch_and_connection_info(self, hub):
        reg = {
            "name": "lim",
            "config": {
                "url": "http://127.0.0.1:9/mcp",
                "disabled": True,
                "tags": ["dev", "herta"],
            },
        }
        assert hub.post("/admin/api/servers", json=reg).status_code == 201

        r = hub.patch("/admin/api/servers/lim/limits", json={"max_concurrent": 2})
        assert r.status_code == 200
        assert r.json()["limits"]["max_concurrent"] == 2

        bad = hub.patch("/admin/api/servers/lim/limits", json={"max_concurrent": 0})
        assert bad.status_code == 422
        assert hub.patch("/admin/api/servers/nope/limits", json={}).status_code == 404

        conn = hub.get("/admin/api/servers/lim/connection").json()
        assert conn["tags"] == ["dev", "herta"]
        assert conn["url"].endswith("?tags=dev,herta")
        assert conn["example_header"] == "X-MCP-Hub-Tags: dev,herta"

    def test_auth_gate_on_admin_api(self, hub, monkeypatch):
        monkeypatch.setenv("MCP_HUB_API_KEY", "e2e-secret")
        assert hub.get("/admin/api/servers").status_code == 401
        assert (
            hub.get(
                "/admin/api/servers", headers={"X-API-Key": "e2e-secret"}
            ).status_code
            == 200
        )
        # health は認証バイパス（監視用）
        assert hub.get("/admin/api/health").status_code == 200

    def test_admin_rejected_while_draining(self, hub):
        assert hub.get("/admin/api/health").status_code == 200
        app_state.draining = True
        try:
            r = hub.get("/admin/api/health")
            assert r.status_code == 503
            assert r.headers["Retry-After"] == "1"
        finally:
            app_state.draining = False
        assert hub.get("/admin/api/health").status_code == 200


class TestModeRouting:
    def test_meta_mode_serves_progressive_discovery(self, hub):
        names = _tool_names(_rpc(hub, "/mcp/", "tools/list"))
        assert {"search_tools", "get_schema", "execute_tool"} <= set(names)

    def test_switching_meta_mode_off_routes_to_normal_app(self, hub):
        assert (
            hub.patch("/admin/api/settings", json={"meta_mode": False}).status_code
            == 200
        )
        names = _tool_names(_rpc(hub, "/mcp/", "tools/list"))
        # normal 経路は upstream ツールを名前空間付きで出す（meta ツールは出ない）
        assert "search_tools" not in names

        assert (
            hub.patch("/admin/api/settings", json={"meta_mode": True}).status_code
            == 200
        )
        assert "search_tools" in _tool_names(_rpc(hub, "/mcp/", "tools/list"))


# ── プロトコル選択（HTTP / SSE / stdio） ────────────────────────


class TestBackendProtocols:
    """_create_proxy が URL 形状でトランスポートを選び分ける（実接続なし）。"""

    @staticmethod
    def _pm():
        mcp = type("MCP", (), {"mount": lambda self, p, namespace=None: None})()
        return ProxyManager(mcp, {})

    def _transport(self, config: dict):
        from unittest.mock import patch

        pm = self._pm()
        with patch("mcp_hub.proxy_manager.Client", autospec=True) as client_cls:
            asyncio.run(pm._create_proxy("x", config))
        return client_cls.call_args.kwargs["transport"]

    def test_sse_url_uses_sse_transport(self):
        from fastmcp.client.transports import SSETransport

        assert isinstance(self._transport({"url": "http://h/sse"}), SSETransport)

    def test_plain_url_uses_streamable_http(self):
        from fastmcp.client.transports import StreamableHttpTransport

        t = self._transport({"url": "http://h/mcp"})
        assert isinstance(t, StreamableHttpTransport)

    def test_command_uses_stdio_transport(self):
        from fastmcp.client.transports import StdioTransport

        t = self._transport({"command": sys.executable, "args": [STDIO_SERVER]})
        assert isinstance(t, StdioTransport)


# ── stdio バックエンド越しのメタ経路（search → get_schema → execute） ─────────


@pytest.fixture
async def stdio_pm():
    mcp = FastMCP("e2e-hub")
    mcp.mount = MagicMock(return_value=None)
    registry = AsyncMock()
    pm = ProxyManager(mcp, registry)
    cfg = {"command": sys.executable, "args": [STDIO_SERVER]}
    await pm.register_server("echo", cfg)
    deadline = time.monotonic() + 30
    while "echo" not in pm._proxies and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert "echo" in pm._proxies, "stdio backend did not connect"
    try:
        yield pm
    finally:
        await pm.close_all()


@pytest.mark.asyncio
class TestStdioMetaFlow:
    async def test_search_schema_execute_round_trip(self, stdio_pm):
        meta_app = await create_meta_app(stdio_pm)
        assert await meta_app.rebuild_index() == []

        async with Client(meta_app.mcp) as client:
            tools = {t.name for t in await client.list_tools()}
            assert {"search_tools", "get_schema", "execute_tool"} <= tools

            found = await client.call_tool("search_tools", {"query": "echo message"})
            text = found.content[0].text
            assert "echo" in text

            schema = await client.call_tool(
                "get_schema", {"server": "echo", "tool_name": "echo"}
            )
            assert "message" in schema.content[0].text

            out = await client.call_tool(
                "execute_tool",
                {"server": "echo", "tool_name": "echo", "arguments": {"message": "hi"}},
            )
            assert "ECHO: hi" in out.content[0].text

    async def test_get_schema_and_execute_reject_unknown_tool(self, stdio_pm):
        meta_app = await create_meta_app(stdio_pm)
        await meta_app.rebuild_index()
        async with Client(meta_app.mcp) as client:
            missing = await client.call_tool(
                "execute_tool",
                {"server": "echo", "tool_name": "nope", "arguments": {}},
            )
            assert "not found" in missing.content[0].text.lower()

    async def test_load_concurrent_calls_respect_limit(self, stdio_pm):
        """軽負荷シナリオ: 10 並列呼び出しが per-server 上限 2 で完走する。"""
        await stdio_pm.update_limits("echo", {"limits": {"max_concurrent": 2}})
        started = time.monotonic()
        results = await asyncio.gather(
            *[
                stdio_pm.call_tool_limited("echo", "echo", {"message": str(i)})
                for i in range(10)
            ]
        )
        # 2 同時 × 5 バッチ。各呼び出しは即時なので、ハングしないことだけ拘束する
        assert time.monotonic() - started < 20
        assert len(results) == 10

    async def test_connection_state_and_tool_counts(self, stdio_pm):
        state = stdio_pm.get_connection_state("echo")
        assert state["status"] == "connected"
        assert state["effective_state"] == "healthy"
        assert state["tool_count"] == 1
