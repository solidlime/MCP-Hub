"""設定ホットリロード（POST /admin/api/reload）のテスト。

対象:
- ProxyManager.apply_server_configs の差分適用（追加/削除/変更/据え置き）
- reload_runtime の設定再適用（meta_mode / use_embeddings / embedding_model）
- 不正 JSON は 500 で弾き、ランタイムを変更前のまま残すこと
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from mcp_hub.main import create_app
from mcp_hub.proxy_manager import ProxyManager
from mcp_hub.reload import ReloadError, reload_runtime
from mcp_hub.state import app_state

_E5 = "intfloat/multilingual-e5-small"


class _MCP:
    """FastMCP の mount/providers だけを持つスタンドイン。"""

    def __init__(self):
        self.local_provider = object()
        self.providers: list = []
        self.mounted: list[str | None] = []

    def mount(self, proxy, namespace=None):
        self.mounted.append(namespace)
        self.providers.append(proxy)


class _Client:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.fixture
def manager(monkeypatch):
    """接続とリフレッシュを記録するだけの ProxyManager。"""
    pm = ProxyManager(_MCP(), {})
    calls: dict[str, list] = {"connect": [], "refresh": []}

    async def fake_connect(name, config):
        calls["connect"].append((name, dict(config)))

    async def fake_refresh(name, config):
        calls["refresh"].append((name, dict(config)))
        pm._server_configs[name] = config

    monkeypatch.setattr(pm, "_connect_and_mount", fake_connect)
    monkeypatch.setattr(pm, "refresh_server", fake_refresh)
    return pm, calls


async def _drain(pm):
    """バックグラウンド接続タスクを待つ。"""
    if pm._bg_tasks:
        await asyncio.gather(*list(pm._bg_tasks), return_exceptions=True)


class TestApplyServerConfigs:
    def test_adds_removes_updates_and_keeps(self, manager):
        pm, calls = manager
        pm._server_configs = {
            "keep": {"url": "http://a"},
            "change": {"url": "http://b"},
            "gone": {"url": "http://c"},
        }
        pm._proxies = {"keep": object(), "change": object(), "gone": object()}
        gone_client = _Client()
        pm._clients = {"gone": gone_client}

        async def scenario():
            report = await pm.apply_server_configs({
                "keep": {"url": "http://a"},
                "change": {"url": "http://b2"},
                "new": {"url": "http://d"},
            })
            await _drain(pm)
            return report

        report = asyncio.run(scenario())

        assert report["added"] == ["new"]
        assert report["removed"] == ["gone"]
        assert report["updated"] == ["change"]
        assert report["unchanged"] == ["keep"]
        assert [c[0] for c in calls["connect"]] == ["new"]
        assert [c[0] for c in calls["refresh"]] == ["change"]
        assert "gone" not in pm._server_configs
        assert "gone" not in pm._proxies
        assert gone_client.closed is True

    def test_disabled_added_is_not_connected(self, manager):
        pm, calls = manager

        async def scenario():
            report = await pm.apply_server_configs({"off": {"url": "http://x", "disabled": True}})
            await _drain(pm)
            return report

        report = asyncio.run(scenario())

        assert report["added"] == ["off"]
        assert calls["connect"] == []
        assert pm._status["off"] == "disabled"


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """一時データディレクトリ + 設定ファイルで実アプリを起動する。"""
    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
    cfg_path = tmp_path / "hub.config.json"
    cfg_path.write_text(json.dumps({
        "version": 1,
        "log_level": "info",
        "mcpServers": {"a": {"url": "http://127.0.0.1:9", "disabled": True}},
    }), encoding="utf-8")
    app = create_app()
    with TestClient(app) as c:
        yield c, cfg_path


def _edit(path, **changes):
    data = json.loads(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        data[key] = value
    path.write_text(json.dumps(data), encoding="utf-8")


class TestReloadEndpoint:
    def test_new_and_removed_servers_are_applied(self, app_client):
        client, path = app_client
        data = json.loads(path.read_text(encoding="utf-8"))
        data["mcpServers"]["b"] = {"url": "http://127.0.0.1:9", "disabled": True}
        del data["mcpServers"]["a"]
        path.write_text(json.dumps(data), encoding="utf-8")

        r = client.post("/admin/api/reload")

        assert r.status_code == 200
        body = r.json()
        assert body["servers"]["added"] == ["b"]
        assert body["servers"]["removed"] == ["a"]
        assert "b" in app_state.proxy_manager._server_configs
        assert "a" not in app_state.proxy_manager._server_configs

    def test_unchanged_reload_is_noop(self, app_client):
        client, _path = app_client

        body = client.post("/admin/api/reload").json()

        assert body["servers"]["added"] == []
        assert body["servers"]["removed"] == []
        assert body["servers"]["unchanged"] == ["a"]

    def test_settings_are_reapplied(self, app_client):
        client, path = app_client
        _edit(path, embedding_model=_E5, use_embeddings=False)
        app_state.mcp_dispatcher._cached_meta_mode = False

        body = client.post("/admin/api/reload").json()

        assert body["settings"]["embedding_model"] == _E5
        assert body["settings"]["embedding_model_changed"] is True
        assert body["settings"]["use_embeddings"] is False
        assert app_state.meta_app.index.embedding_model == _E5
        # meta_mode は毎リクエストで store から読むため、キャッシュを落とせば足りる
        assert app_state.mcp_dispatcher._cached_meta_mode is None

    def test_corrupt_config_is_rejected_without_touching_runtime(self, app_client):
        client, path = app_client
        before = dict(app_state.proxy_manager._server_configs)
        path.write_text("{ not json", encoding="utf-8")

        r = client.post("/admin/api/reload")

        assert r.status_code == 500
        assert "設定ファイル" in r.json()["detail"]
        assert dict(app_state.proxy_manager._server_configs) == before


class TestConfigValidation:
    """適用前検証: 壊れたエントリは 500 で弾き、ランタイムは無傷のまま。"""

    def test_broken_server_entry_is_rejected(self, app_client):
        client, path = app_client
        before = dict(app_state.proxy_manager._server_configs)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["mcpServers"]["broken"] = {"disabled": False}  # url も command も無い
        path.write_text(json.dumps(data), encoding="utf-8")

        r = client.post("/admin/api/reload")

        assert r.status_code == 500
        assert "broken" in r.json()["detail"]
        assert dict(app_state.proxy_manager._server_configs) == before

    def test_disabled_stub_without_transport_is_allowed(self, app_client):
        client, path = app_client
        data = json.loads(path.read_text(encoding="utf-8"))
        data["mcpServers"]["stub"] = {"disabled": True}  # 接続対象ではないので通す
        path.write_text(json.dumps(data), encoding="utf-8")

        r = client.post("/admin/api/reload")

        assert r.status_code == 200
        assert r.json()["servers"]["added"] == ["stub"]


class TestReloadRuntime:
    def test_corrupt_config_raises_reload_error(self, tmp_path):
        path = tmp_path / "hub.config.json"
        path.write_text("[]", encoding="utf-8")  # JSON だが dict でない

        with pytest.raises(ReloadError):
            asyncio.run(reload_runtime(
                registry=object(), proxy_manager=object(), meta_app=None,
                dispatcher=None, config_path=str(path),
            ))

    def test_missing_config_is_not_treated_as_remove_all(self, tmp_path):
        path = tmp_path / "hub.config.json"  # 存在しない

        with pytest.raises(ReloadError):
            asyncio.run(reload_runtime(
                registry=object(), proxy_manager=object(), meta_app=None,
                dispatcher=None, config_path=str(path),
            ))


class _Registry:
    """watch_config が参照する path だけを持つスタンドイン。"""

    def __init__(self, path):
        self.path = path


class TestWatchConfig:
    """MCP_HUB_WATCH_CONFIG の監視ループは失敗しても止まらないこと。"""

    def test_reloads_when_mtime_changes(self, tmp_path, monkeypatch):
        from mcp_hub import reload as reload_mod

        path = tmp_path / "hub.config.json"
        path.write_text("{}", encoding="utf-8")
        calls: list = []

        async def fake_reload(**kwargs):
            calls.append(kwargs)
            return {"servers": {"added": [], "removed": [], "updated": [], "unchanged": []}}

        monkeypatch.setattr(reload_mod, "reload_runtime", fake_reload)

        async def scenario():
            task = asyncio.create_task(reload_mod.watch_config(0.01, registry=_Registry(path)))
            await asyncio.sleep(0.05)  # 初回はベースライン取得のみ
            first = len(calls)
            path.write_text('{"version": 1}', encoding="utf-8")
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return first

        assert asyncio.run(scenario()) == 0
        assert len(calls) >= 1

    def test_survives_reload_errors(self, tmp_path, monkeypatch):
        from mcp_hub import reload as reload_mod

        path = tmp_path / "hub.config.json"
        path.write_text("{}", encoding="utf-8")
        attempts: list[int] = []

        async def flaky_reload(**kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise ReloadError("壊れた設定")
            return {"servers": {"added": [], "removed": [], "updated": [], "unchanged": []}}

        monkeypatch.setattr(reload_mod, "reload_runtime", flaky_reload)

        async def scenario():
            task = asyncio.create_task(reload_mod.watch_config(0.01, registry=_Registry(path)))
            await asyncio.sleep(0.05)
            path.write_text('{"version": 1}', encoding="utf-8")  # 1回目: ReloadError
            await asyncio.sleep(0.1)
            path.write_text('{"version": 2}', encoding="utf-8")  # 2回目: 成功
            await asyncio.sleep(0.1)
            alive = not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return alive

        assert asyncio.run(scenario()) is True
        assert len(attempts) >= 2

    def test_survives_missing_file(self, tmp_path, monkeypatch):
        """atomic replace の途中などで stat が OSError でも監視は続く。"""
        from mcp_hub import reload as reload_mod

        path = tmp_path / "hub.config.json"  # 存在しない
        calls: list = []

        async def fake_reload(**kwargs):
            calls.append(kwargs)
            return {"servers": {"added": [], "removed": [], "updated": [], "unchanged": []}}

        monkeypatch.setattr(reload_mod, "reload_runtime", fake_reload)

        async def scenario():
            task = asyncio.create_task(reload_mod.watch_config(0.01, registry=_Registry(path)))
            await asyncio.sleep(0.05)
            alive = not task.done()
            path.write_text("{}", encoding="utf-8")
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return alive

        assert asyncio.run(scenario()) is True
        assert calls == []  # 初回はベースラインのみ
