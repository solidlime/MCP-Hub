"""埋め込み設定変更の即時再構築（タスク #2）のテスト。

契約:
- `PATCH /admin/api/settings/embedding-model` は保存後に背景で索引再構築を起動し、
  再起動を待たない。応答とその後は `embedding_rebuild` で進捗/成功/失敗が見える。
- `PATCH /admin/api/settings` の `use_embeddings` 変更も同じ経路で再構築する。
- 再構築中は埋め込みが捨てられ、検索は BM25 のみで継続する（フォールバック）。
"""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from mcp_hub import admin_router
from mcp_hub.config import DEFAULT_EMBEDDING_MODEL
from mcp_hub.main import create_app
from mcp_hub.state import EmbeddingRebuildState, app_state

NEW_MODEL = "intfloat/multilingual-e5-small"


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02):
    """背景タスクの完了をポーリングで待つ（イベントループは別スレッドで回る）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("条件が timeout 内に満たされなかった")


# === 単体: 再構築タスクの状態遷移 ===


class _FakeIndex:
    def __init__(self):
        self.embedding_model = DEFAULT_EMBEDDING_MODEL
        self.document_count = 0
        self.embedding_error = None
        self.embedding_status = "inactive:setting"


class _FakeMetaApp:
    """rebuild_index の結果だけを差し替えた最小の MetaApp 代用。"""

    def __init__(self, *, delay=0.0, raises=None, failed=None, embed_error=None):
        self.index = _FakeIndex()
        self.delay = delay
        self.raises = raises
        self.failed = failed or []
        self.embed_error = embed_error
        self.calls = 0

    async def rebuild_index(self):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        if self.embed_error:
            self.index.embedding_error = self.embed_error
        return list(self.failed)


def _current_task() -> asyncio.Task:
    """直近に起動した再構築タスク（未起動なら None なので明示的に落とす）。"""
    task = admin_router._embedding_rebuild_task
    assert task is not None
    return task


def _reset_rebuild_state(monkeypatch=None):
    app_state.embedding_rebuild = EmbeddingRebuildState()
    admin_router._embedding_rebuild_task = None


class TestRebuildTaskStates:
    def test_schedule_marks_running_then_success(self, monkeypatch):
        _reset_rebuild_state()
        fake = _FakeMetaApp()
        monkeypatch.setattr(app_state, "meta_app", fake)

        async def go():
            snap = admin_router._schedule_embedding_rebuild(
                "embedding_model", fake, NEW_MODEL
            )
            assert snap["status"] == "running"
            assert snap["in_progress"] is True
            assert snap["trigger"] == "embedding_model"
            assert snap["requested_model"] == NEW_MODEL
            assert snap["started_at"] is not None
            assert snap["finished_at"] is None
            await _current_task()
            return admin_router._embedding_rebuild_snapshot()

        snap = asyncio.run(go())
        assert fake.calls == 1
        assert snap["status"] == "success"
        assert snap["in_progress"] is False
        assert snap["error"] is None
        assert snap["finished_at"] is not None

    def test_rebuild_exception_is_recorded_as_error(self, monkeypatch):
        _reset_rebuild_state()
        fake = _FakeMetaApp(raises=RuntimeError("onnx broken"))
        monkeypatch.setattr(app_state, "meta_app", fake)

        async def go():
            admin_router._schedule_embedding_rebuild("embedding_model", fake, NEW_MODEL)
            await _current_task()

        asyncio.run(go())
        state = app_state.embedding_rebuild
        assert state.status == "error"
        assert "onnx broken" in (state.error or "")
        assert state.finished_at is not None

    def test_failed_servers_are_recorded_as_error(self, monkeypatch):
        _reset_rebuild_state()
        fake = _FakeMetaApp(failed=["warmup-server"])
        monkeypatch.setattr(app_state, "meta_app", fake)

        async def go():
            admin_router._schedule_embedding_rebuild("use_embeddings", fake)
            await _current_task()

        asyncio.run(go())
        state = app_state.embedding_rebuild
        assert state.status == "error"
        assert "warmup-server" in (state.error or "")

    def test_embedding_error_is_recorded_as_error(self, monkeypatch):
        _reset_rebuild_state()
        fake = _FakeMetaApp(embed_error="OSError: model download failed")
        monkeypatch.setattr(app_state, "meta_app", fake)

        async def go():
            admin_router._schedule_embedding_rebuild("embedding_model", fake, NEW_MODEL)
            await _current_task()

        asyncio.run(go())
        state = app_state.embedding_rebuild
        assert state.status == "error"
        assert "model download failed" in (state.error or "")

    def test_new_rebuild_replaces_running_one(self, monkeypatch):
        """設定は最終値だけが意味を持つ: 実行中の再構築は cancel され状態を壊さない。"""
        _reset_rebuild_state()
        slow = _FakeMetaApp(delay=10.0)
        fast = _FakeMetaApp()
        monkeypatch.setattr(app_state, "meta_app", fast)

        async def go():
            admin_router._schedule_embedding_rebuild("embedding_model", slow, "a/model")
            first = _current_task()
            admin_router._schedule_embedding_rebuild("embedding_model", fast, NEW_MODEL)
            await asyncio.sleep(0)
            assert first.cancelled() or first.cancelling()
            await _current_task()

        asyncio.run(go())
        state = app_state.embedding_rebuild
        assert state.status == "success"
        assert state.requested_model == NEW_MODEL
        assert fast.calls == 1


# === 単体: 再構築中の検索フォールバック（BM25 のみ） ===


class TestRebuildFallback:
    def test_model_swap_keeps_bm25_search_working(self):
        """モデル差し替え後（再構築前）も検索は BM25 で継続する。"""
        from mcp_hub.meta_provider import ToolIndex

        index = ToolIndex(embedding_model=DEFAULT_EMBEDDING_MODEL, use_embeddings=True)
        documents = [
            {
                "server": "web",
                "name": "fetch_url",
                "description": "Fetch a URL",
                "index_text": "web fetch url page content",
                "full_description": "Fetch a URL and return the page content",
                "tags": ["web"],
                "inputSchema": {},
            },
            {
                "server": "db",
                "name": "query_rows",
                "description": "Query database rows",
                "index_text": "db query rows sql table",
                "full_description": "Run a SQL query and return rows",
                "tags": ["db"],
                "inputSchema": {},
            },
        ]
        asyncio.run(index.rebuild(documents))
        assert index.document_count == 2
        assert index.search("fetch url page")

        index.set_embedding_model(NEW_MODEL)
        assert index.embedding_model == NEW_MODEL
        # 旧ベクトル/embedder は捨てられ、埋め込みは実効無効（BM25 のみ）
        assert index._embeddings is None
        assert index.embedding_status.startswith("inactive")

        hits = index.search("fetch url page")
        assert hits and hits[0]["name"] == "fetch_url"


# === API: PATCH が再構築を起動し、状態が API に出る ===


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
    _reset_rebuild_state()
    app = create_app()
    with TestClient(app) as c:
        yield c
    admin_router._embedding_rebuild_task = None


class TestEmbeddingModelEndpoint:
    def test_get_settings_exposes_rebuild_state(self, client):
        r = client.get("/admin/api/settings")
        assert r.status_code == 200
        rebuild = r.json()["embedding_rebuild"]
        for key in (
            "status",
            "in_progress",
            "trigger",
            "requested_model",
            "active_model",
            "documents",
            "embedding_status",
            "error",
            "started_at",
            "finished_at",
        ):
            assert key in rebuild

    def test_patch_schedules_rebuild_and_persists(self, client, monkeypatch):
        meta_app = app_state.meta_app
        assert meta_app is not None
        fake = _FakeMetaApp()
        monkeypatch.setattr(meta_app, "rebuild_index", fake.rebuild_index)

        r = client.patch(
            "/admin/api/settings/embedding-model",
            json={"embedding_model": NEW_MODEL},
        )
        assert r.status_code == 200
        body = r.json()
        # 応答はブロックしない（受け付け状態が返る: running か、即終わっていれば success）
        assert body["embedding_model"] == NEW_MODEL
        assert body["rebuild"]["requested_model"] == NEW_MODEL
        assert body["rebuild"]["status"] in ("running", "success")

        # 再起動を待たずに背景で 1 回再構築が走る
        _wait_until(lambda: fake.calls == 1)
        settings = _wait_until(
            lambda: (
                s
                if (s := client.get("/admin/api/settings").json())["embedding_rebuild"][
                    "status"
                ]
                != "running"
                else None
            )
        )
        assert settings["embedding_rebuild"]["status"] == "success"
        assert settings["embedding_rebuild"]["active_model"] == NEW_MODEL

        # store への永続化
        assert app_state.registry is not None
        assert app_state.registry._do_read()["embedding_model"] == NEW_MODEL

    def test_patch_reports_running_while_rebuild_in_flight(self, client, monkeypatch):
        meta_app = app_state.meta_app
        assert meta_app is not None
        fake = _FakeMetaApp(delay=0.5)
        monkeypatch.setattr(meta_app, "rebuild_index", fake.rebuild_index)

        r = client.patch(
            "/admin/api/settings/embedding-model",
            json={"embedding_model": NEW_MODEL},
        )
        assert r.status_code == 200
        assert r.json()["rebuild"]["status"] == "running"
        assert r.json()["rebuild"]["in_progress"] is True

        settings = client.get("/admin/api/settings").json()
        assert settings["embedding_rebuild"]["in_progress"] is True
        assert settings["embedding_rebuild"]["requested_model"] == NEW_MODEL

        _wait_until(
            lambda: app_state.embedding_rebuild.status != "running", timeout=10.0
        )
        assert app_state.embedding_rebuild.status == "success"

    def test_missing_key_is_400(self, client):
        r = client.patch("/admin/api/settings/embedding-model", json={})
        assert r.status_code == 400

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "http://evil/model", "/abs/path", "../up", "a" * 300, "a b"],
    )
    def test_invalid_model_is_422(self, client, bad):
        r = client.patch(
            "/admin/api/settings/embedding-model", json={"embedding_model": bad}
        )
        assert r.status_code == 422

    def test_use_embeddings_toggle_schedules_rebuild(self, client, monkeypatch):
        meta_app = app_state.meta_app
        assert meta_app is not None
        fake = _FakeMetaApp()
        monkeypatch.setattr(meta_app, "rebuild_index", fake.rebuild_index)

        r = client.patch("/admin/api/settings", json={"use_embeddings": False})
        assert r.status_code == 200
        assert r.json()["embedding_rebuild"]["trigger"] == "use_embeddings"
        _wait_until(lambda: fake.calls == 1)
