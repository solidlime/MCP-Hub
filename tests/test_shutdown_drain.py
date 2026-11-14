"""グレースフルシャットダウン / ドレイン (task #7)。

- 新規受付停止 (HTTP 503 / ProxyManager.call_tool 拒否)
- 進行中リクエストのドレイン待機（タイムアウト付き）
- バックグラウンド接続タスク・upstream client の回収
"""

import asyncio
import json
import time
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from fastmcp import FastMCP

from mcp_hub.config import DEFAULT_DRAIN_TIMEOUT, load_config
from mcp_hub.main import _drain_timeout, _graceful_drain, create_app
from mcp_hub.proxy_manager import ProxyManager
from mcp_hub.state import _AppState, app_state
from mcp_hub.store import JsonStore


class _MockProxy:
    def __init__(self):
        self.call_tool = AsyncMock(return_value="ok")


class _MockStore:
    async def list_servers(self):
        return []

    async def add_server(self, name, config):
        pass

    async def get_server(self, name):
        return None

    async def remove_server(self, name):
        return True


@pytest.fixture
def manager():
    mcp = FastMCP("test")
    pm = ProxyManager(mcp, cast(JsonStore, _MockStore()))
    pm.mcp.mount = MagicMock(return_value=None)
    return pm


@pytest.fixture
def fresh_state():
    return _AppState()


class TestCallToolGate:
    @pytest.mark.asyncio
    async def test_call_tool_rejected_after_begin_drain(self, manager):
        proxy = _MockProxy()
        manager._proxies["srv"] = proxy
        manager.begin_drain()

        with pytest.raises(RuntimeError, match="shutting down"):
            await manager.call_tool("srv", "tool", {})
        proxy.call_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_call_tool_still_works_before_drain(self, manager):
        proxy = _MockProxy()
        manager._proxies["srv"] = proxy

        assert await manager.call_tool("srv", "tool", {"a": 1}) == "ok"
        proxy.call_tool.assert_awaited_once_with("tool", {"a": 1})


class TestCloseAll:
    @pytest.mark.asyncio
    async def test_cancels_background_tasks_and_closes_clients(self, manager):
        cancelled = asyncio.Event()

        async def _hanging_connect():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        manager._spawn_bg(_hanging_connect())
        client = MagicMock()
        client.close = AsyncMock()
        manager._clients["srv"] = client
        await asyncio.sleep(0)  # タスクを running にする

        await manager.close_all()

        assert cancelled.is_set()
        assert manager._bg_tasks == set()
        client.close.assert_awaited_once()
        assert manager._clients == {}
        assert manager._draining is True

    @pytest.mark.asyncio
    async def test_partial_client_failure_does_not_abort(self, manager):
        bad = MagicMock()
        bad.close = AsyncMock(side_effect=RuntimeError("boom"))
        good = MagicMock()
        good.close = AsyncMock()
        manager._clients["bad"] = bad
        manager._clients["good"] = good

        await manager.close_all()  # 例外を送出しない

        bad.close.assert_awaited_once()
        good.close.assert_awaited_once()
        assert manager._clients == {}


class TestStateDrain:
    @pytest.mark.asyncio
    async def test_wait_idle_returns_zero_after_inflight_completes(self, fresh_state):
        st = fresh_state
        st.enter_request()

        async def _finish_soon():
            await asyncio.sleep(0.05)
            st.leave_request()

        asyncio.create_task(_finish_soon())
        remaining = await st.wait_idle(timeout=2.0)

        assert remaining == 0

    @pytest.mark.asyncio
    async def test_wait_idle_times_out_with_remaining_count(self, fresh_state):
        st = fresh_state
        st.enter_request()

        remaining = await st.wait_idle(timeout=0.01)

        assert remaining == 1
        st.leave_request()
        assert st.inflight == 0

    def test_begin_drain_marks_state_and_stops_new_requests(self, fresh_state):
        st = fresh_state
        assert st.begin_drain() == 0
        assert st.draining is True


class TestDrainTimeoutConfig:
    def _write(self, tmp_path, **extra):
        p = tmp_path / "hub.config.json"
        p.write_text(json.dumps({"version": 1, "mcpServers": {}, **extra}))
        return str(p)

    def test_default_when_absent(self, tmp_path):
        assert load_config(self._write(tmp_path)).shutdown_drain_timeout == (
            DEFAULT_DRAIN_TIMEOUT
        )

    def test_parsed_from_config(self, tmp_path):
        cfg = load_config(self._write(tmp_path, shutdown_drain_timeout=2.5))
        assert cfg.shutdown_drain_timeout == 2.5

    def test_invalid_value_falls_back(self, tmp_path):
        cfg = load_config(self._write(tmp_path, shutdown_drain_timeout="soon"))
        assert cfg.shutdown_drain_timeout == DEFAULT_DRAIN_TIMEOUT

    def test_env_overrides_config(self, tmp_path, monkeypatch):
        cfg = load_config(self._write(tmp_path, shutdown_drain_timeout=2.5))
        monkeypatch.setenv("MCP_HUB_DRAIN_TIMEOUT", "1.5")
        assert _drain_timeout(cfg) == 1.5

    def test_env_invalid_falls_back_to_config(self, tmp_path, monkeypatch):
        cfg = load_config(self._write(tmp_path, shutdown_drain_timeout=2.5))
        monkeypatch.setenv("MCP_HUB_DRAIN_TIMEOUT", "nope")
        assert _drain_timeout(cfg) == 2.5


class TestGracefulDrain:
    @pytest.mark.asyncio
    async def test_times_out_then_stops_background_work(self, monkeypatch):
        monkeypatch.setenv("MCP_HUB_DRAIN_TIMEOUT", "0.05")
        pm = MagicMock()
        pm.stop_health_monitor = AsyncMock()
        pm.cancel_background_tasks = AsyncMock()
        watch = asyncio.create_task(asyncio.sleep(3600))
        rebuild = asyncio.create_task(asyncio.sleep(3600))

        app_state.draining = False
        app_state.enter_request()  # 帰ってこない進行中リクエストを模擬
        try:
            started = time.monotonic()
            await _graceful_drain(None, pm, watch, rebuild)
            elapsed = time.monotonic() - started

            assert app_state.draining is True
            assert 0.04 <= elapsed < 5  # タイムアウトで打ち切る（無限待機しない）
            assert watch.cancelled() and rebuild.cancelled()
            pm.begin_drain.assert_called_once()
            pm.stop_health_monitor.assert_awaited_once()
            pm.cancel_background_tasks.assert_awaited_once()
        finally:
            app_state.inflight = 0
            app_state.draining = False


class TestUvicornShutdownBound:
    def test_uvicorn_gets_drain_timeout(self, tmp_path, monkeypatch):
        """uvicorn 側に待機上限が無いと、SSE 等の長期接続が閉じるまで
        lifespan shutdown（ドレイン）に到達しない。"""
        import uvicorn

        import mcp_hub.main as main_mod

        monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MCP_HUB_DRAIN_TIMEOUT", "2.5")
        captured = {}
        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))

        main_mod.main()

        assert captured["timeout_graceful_shutdown"] == 3  # 2.5 を切り上げ（drain より短くしない）


class TestDrainHttpGate:
    def test_new_requests_rejected_while_draining(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
        app = create_app()
        with TestClient(app) as client:
            assert client.get("/admin/api/health").status_code == 200
            app_state.draining = True
            try:
                r = client.get("/admin/api/health")
            finally:
                app_state.draining = False
            assert r.status_code == 503
            assert client.get("/admin/api/health").status_code == 200
