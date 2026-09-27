"""tool_search_desc: ツール単位の日本語1文を索引テキストに注入する契約のテスト。

- rebuild_index が proxy_manager.server_tool_search_desc() を読み、index_text に
  前置すること（英語 docstring の語彙の壁を越える経路）。
- その1文が意味検索の順位に効くこと（スタブ埋め込みで再現）。
- index_text が変わった文書だけが再埋め込みされること（doc 単位の sha1 キャッシュ。
  TEXT_FMT_VERSION の bump は不要）。
- per-server PATCH が tool_search_desc を部分更新マージで受け、プロキシ再生成に
  落ちないこと（update_config_only 経由で即時反映）。
- POST /admin/api/llm/generate の正常系・api_key 未設定 400・上流失敗 502。
- 上流が HTTP 200 でも choices を持たない JSON（OpenAI 互換契約の外）は 502 に畳む。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from mcp_hub.main import create_app
from mcp_hub.meta_provider import ToolIndex, create_meta_app
from mcp_hub.state import app_state


# ── スタブ ──────────────────────────────────────────────────────────


class _KeywordEmbedder:
    """text にキーワードが含まれるかで決定的な2次元ベクトルを返す。

    埋め込みチャネルを再現可能に検証するためのスタブ（実モデル不要）。
    keyword を含む → [1,0]、含まない → [0,1]。
    """

    def __init__(self, keyword: str, dim: int = 2):
        self.keyword = keyword
        self.dim = dim
        self.embedded: list[str] = []
        self.calls = 0

    def embed(self, texts, batch_size=8):  # noqa: ANN001
        self.calls += 1
        out = []
        for t in texts:
            self.embedded.append(t)
            out.append(
                np.array(
                    [1.0, 0.0] if self.keyword in t else [0.0, 1.0], dtype=np.float32
                )
            )
        return out


def _tool(name: str, description: str):
    return SimpleNamespace(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {}},
    )


def _mock_proxy_manager(tools, desc_map, server="ha"):
    pm = MagicMock()
    proxy = MagicMock()
    proxy.list_tools = AsyncMock(return_value=tools)
    pm._proxies = {server: proxy}
    pm.get_connected_servers = MagicMock(side_effect=lambda: dict(pm._proxies))

    async def _list_tools_for_server(name, proxy):  # noqa: ANN001
        return tools

    pm.list_tools_for_server = AsyncMock(side_effect=_list_tools_for_server)
    pm.server_tags = MagicMock(return_value=[])
    pm.server_description = MagicMock(return_value="")
    pm.server_tool_search_desc = MagicMock(return_value=desc_map)
    return pm


async def _meta_app_with_embeddings(pm, embedder):
    meta = await create_meta_app(pm)
    meta.index._embedder = embedder  # type: ignore[assignment]
    meta.index._use_embeddings = True
    meta.index._embeddings_requested = True
    await meta.rebuild_index()
    return meta


# ── index_text への注入 ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_HUB_EMBED_CACHE_DIR", str(tmp_path / "embcache"))


async def test_rebuild_index_injects_tool_search_desc():
    """server_tool_search_desc の1文が index_text に前置される。"""
    tools = [_tool("ha_search", "Search entities in Home Assistant")]
    pm = _mock_proxy_manager(tools, {"ha_search": "照明や電球を操作し室温を確認する"})
    meta = await _meta_app_with_embeddings(pm, _KeywordEmbedder("電球"))

    doc = next(d for d in meta.index._documents if d["name"] == "ha_search")
    assert "照明や電球を操作し室温を確認する" in doc["index_text"]
    # 元の英語 docstring も残る（後置の切り詰めは従来どおり）
    assert "Search entities in Home Assistant" in doc["index_text"]


async def test_rebuild_index_without_tool_search_desc_unchanged():
    """未設定なら index_text は従来どおりサーバー説明 + docstring。"""
    tools = [_tool("ha_search", "Search entities in Home Assistant")]
    pm = _mock_proxy_manager(tools, {})
    meta = await _meta_app_with_embeddings(pm, _KeywordEmbedder("電球"))

    doc = next(d for d in meta.index._documents if d["name"] == "ha_search")
    assert doc["index_text"] == "Search entities in Home Assistant"


# ── 検索順位への効果 ─────────────────────────────────────────────────


async def test_tool_search_desc_makes_ignored_tool_rank_first():
    """英語 docstring に「電球」が無くても、tool_search_desc があれば1位に来る。

    無い場合は埋め込み語彙にも検索語彙にも届かず結果に出ない（語彙の壁）。
    """
    tools = [_tool("ha_search", "Search entities in Home Assistant")]

    # 1文あり → 電球 が index_text に入り、意味検索・BM25 両方で届く。
    with_desc = await _meta_app_with_embeddings(
        _mock_proxy_manager(tools, {"ha_search": "照明や電球を操作する"}),
        _KeywordEmbedder("電球"),
    )
    hit = with_desc.index.search("電球", top_k=5)
    assert [r["name"] for r in hit][:1] == ["ha_search"]

    # 1文なし → 届かない（空 or 対象外）。
    without = await _meta_app_with_embeddings(
        _mock_proxy_manager(tools, {}), _KeywordEmbedder("電球")
    )
    miss = [r["name"] for r in without.index.search("電球", top_k=5)]
    assert "ha_search" not in miss


# ── doc 単位の再埋め込みキャッシュ ──────────────────────────────────


def _doc(name: str, index_text: str) -> dict:
    return {
        "server": "s",
        "name": name,
        "description": index_text,
        "index_text": index_text,
        "full_description": index_text,
        "tags": [],
        "inputSchema": {"type": "object", "properties": {}},
    }


async def test_index_text_change_reembeds_only_that_doc():
    """index_text を変えた文書だけが再 embed される（sha1[:16] の doc 単位キャッシュ）。"""
    emb = _KeywordEmbedder("hit")  # 常に同じ次元を返すだけの計数用
    idx = ToolIndex()
    idx._embedder = emb  # type: ignore[assignment]
    idx._use_embeddings = True
    idx._embeddings_requested = True

    await idx.rebuild([_doc("a", "alpha"), _doc("b", "beta")])
    assert len(emb.embedded) == 2  # 初回は両文書

    emb.embedded.clear()
    # a の index_text だけ変更（tool_search_desc 追加の再現）。
    await idx.rebuild([_doc("a", "alpha 電球"), _doc("b", "beta")])
    assert len(emb.embedded) == 1  # 変わった a だけ再埋め込み
    assert "電球" in emb.embedded[0]

    emb.embedded.clear()
    # コーパス不変 → short-circuit で再 embed なし。
    await idx.rebuild([_doc("a", "alpha 電球"), _doc("b", "beta")])
    assert emb.embedded == []


# ── per-server PATCH（tool_search_desc の保存経路） ────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    """MCP_HUB_DATA_DIR を分離した実アプリ（TestClient）。"""
    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as c:
        yield c


def _add_disabled(client, name):
    r = client.post(
        "/admin/api/servers",
        json={"name": name, "config": {"url": "http://localhost:9999", "disabled": True}},
    )
    assert r.status_code == 201


def test_patch_sets_tool_search_desc(client):
    """PATCH /servers/{name} が tool_search_desc を config に乗せ、返す。"""
    _add_disabled(client, "ha")
    descs = {"ha_search": "照明や電球を操作し室温を確認する"}
    r = client.patch("/admin/api/servers/ha", json={"tool_search_desc": descs})
    assert r.status_code == 200
    assert r.json()["config"]["tool_search_desc"] == descs
    # カタログ参照元（server_tool_search_desc）にも即時反映される
    pm = app_state.proxy_manager
    assert pm is not None
    assert pm.server_tool_search_desc("ha") == descs
    # 恒久化されている（GET でも乗る）
    listed = client.get("/admin/api/servers").json()["servers"]
    server = next(s for s in listed if s["name"] == "ha")
    assert server["config"]["tool_search_desc"] == descs


def test_patch_tool_search_desc_skips_refresh(client, monkeypatch):
    """tool_search_desc のみの PATCH はプロキシ再生成を伴わない。"""
    _add_disabled(client, "ha2")
    pm = app_state.proxy_manager
    calls = []

    async def fake_refresh(name, config):
        calls.append((name, config))

    monkeypatch.setattr(pm, "refresh_server", fake_refresh)
    r = client.patch(
        "/admin/api/servers/ha2", json={"tool_search_desc": {"t": "説明"}}
    )
    assert r.status_code == 200
    assert calls == []


def test_patch_tool_search_desc_invalid_is_422(client):
    """{ツール名: 文字列} 以外は 422。"""
    _add_disabled(client, "ha3")
    r = client.patch("/admin/api/servers/ha3", json={"tool_search_desc": {"t": 1}})
    assert r.status_code == 422
    r2 = client.patch("/admin/api/servers/ha3", json={"tool_search_desc": "not-a-map"})
    assert r2.status_code == 422


# ── LLM 生成エンドポイント ──────────────────────────────────────────


class _FakeResp:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _install_fake_httpx(monkeypatch, handler):
    """httpx.AsyncClient を handler(url, headers, json) -> _FakeResp に差し替える。"""

    class _FakeClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, headers=None, json=None):
            return handler(url, headers, json)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)


def _llm_ready_client(client, monkeypatch, *, api_key="sk-test"):
    """接続済みサーバー1件と llm.api_key を用意した状態にする。"""
    pm = app_state.proxy_manager
    monkeypatch.setattr(pm, "get_proxy", lambda name: object())
    monkeypatch.setattr(pm, "server_description", lambda name: "Home Assistant 操作")

    async def _list(name, proxy):  # noqa: ANN001
        return [_tool("ha_search", "Search entities in Home Assistant")]

    monkeypatch.setattr(pm, "list_tools_for_server", _list)
    r = client.patch(
        "/admin/api/settings", json={"llm": {"api_key": api_key, "model": "gpt-test"}}
    )
    assert r.status_code == 200
    assert r.json()["llm"]["api_key_set"] is True
    return pm


def test_llm_generate_server_description(client, monkeypatch):
    """正常系: OpenAI 互換に POST し、content を text で返す。"""
    _llm_ready_client(client, monkeypatch)
    seen: list = []

    def handler(url, headers, payload):
        seen.append((url, headers, payload))
        return _FakeResp(200, {"choices": [{"message": {"content": " 照明を操作する "}}]})

    _install_fake_httpx(monkeypatch, handler)
    r = client.post(
        "/admin/api/llm/generate", json={"kind": "server_description", "server": "ha"}
    )
    assert r.status_code == 200
    assert r.json() == {"text": "照明を操作する"}
    url, headers, payload = seen[0]
    assert url == "https://api.openai.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-test"
    assert payload["model"] == "gpt-test"
    assert "ha" in payload["messages"][0]["content"]


def test_llm_generate_tool_search_desc(client, monkeypatch):
    """正常系: tool_search_desc は tool_name を要し、docstring を材料にする。"""
    _llm_ready_client(client, monkeypatch)
    seen: list = []

    def handler(url, headers, payload):
        seen.append(payload)
        return _FakeResp(200, {"choices": [{"message": {"content": "エンティティを検索する"}}]})

    _install_fake_httpx(monkeypatch, handler)
    r = client.post(
        "/admin/api/llm/generate",
        json={"kind": "tool_search_desc", "server": "ha", "tool_name": "ha_search"},
    )
    assert r.status_code == 200
    assert r.json()["text"] == "エンティティを検索する"
    assert "Search entities in Home Assistant" in seen[0]["messages"][0]["content"]


def test_llm_generate_without_api_key_is_400(client):
    """api_key 未設定は 400（フロントは ✨ を出さない）。"""
    r = client.post(
        "/admin/api/llm/generate", json={"kind": "server_description", "server": "ha"}
    )
    assert r.status_code == 400


def test_llm_generate_upstream_error_is_502(client, monkeypatch):
    """上流 HTTP エラーは 502 に畳む。"""
    _llm_ready_client(client, monkeypatch)
    _install_fake_httpx(monkeypatch, lambda *a: _FakeResp(500, {}))
    r = client.post(
        "/admin/api/llm/generate", json={"kind": "server_description", "server": "ha"}
    )
    assert r.status_code == 502


def test_llm_generate_200_without_choices_is_502(client, monkeypatch):
    """上流が 200 でも choices 無し（例: {"error": ...}）は 502 に畳む。

    OpenAI 互換契約ではエラーは 4xx/5xx なので、200 + エラー JSON は契約外。
    JSON 解析失敗として 502 に寄せ、text へ流さない現挙動を固定する。
    """
    _llm_ready_client(client, monkeypatch)
    _install_fake_httpx(
        monkeypatch, lambda *a: _FakeResp(200, {"error": "rate limited"})
    )
    r = client.post(
        "/admin/api/llm/generate", json={"kind": "server_description", "server": "ha"}
    )
    assert r.status_code == 502


def test_llm_generate_connection_failure_is_502(client, monkeypatch):
    """接続失敗（httpx.HTTPError）も 502。"""
    _llm_ready_client(client, monkeypatch)

    def handler(url, headers, payload):
        raise httpx.ConnectError("boom")

    _install_fake_httpx(monkeypatch, handler)
    r = client.post(
        "/admin/api/llm/generate", json={"kind": "server_description", "server": "ha"}
    )
    assert r.status_code == 502


def test_patch_settings_llm_partial_merge_and_off(client):
    """llm は部分更新マージ（api_key を毎回送らせない）。空 dict で機能オフ。"""
    client.patch("/admin/api/settings", json={"llm": {"api_key": "sk-1", "model": "m1"}})
    # model だけ更新しても api_key は残る（merge）
    r = client.patch("/admin/api/settings", json={"llm": {"model": "m2"}})
    assert r.json()["llm"] == {
        "provider": None,
        "model": "m2",
        "base_url": None,
        "api_key_set": True,
    }
    # 非オブジェクトは 422
    assert client.patch("/admin/api/settings", json={"llm": "x"}).status_code == 422
    # 空 dict でオフ
    r2 = client.patch("/admin/api/settings", json={"llm": {}})
    assert r2.json()["llm"]["api_key_set"] is False
