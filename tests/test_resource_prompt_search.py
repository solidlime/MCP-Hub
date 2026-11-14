"""リソース/リソーステンプレート/プロンプトの検索・発見（task #11）。

ツール索引と同じ ToolIndex に kind 付きで載せ、`kinds` で経路を分ける契約を
検証する。既存の search_tools がリソースを混ぜないこと（後方互換）も見る。
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from mcp_hub.main import create_app
from mcp_hub.meta_provider import MetaTools, ToolIndex, create_meta_app
from mcp_hub.state import app_state, request_tags


def _doc(kind, server, name, description, **extra):
    doc = {
        "server": server,
        "name": name,
        "description": description,
        "index_text": description,
        "tags": [],
        "inputSchema": {},
        "kind": kind,
    }
    doc.update(extra)
    return doc


RESOURCE_DOCS = [
    _doc("tool", "files", "file_read", "Read file contents from disk"),
    _doc(
        "resource",
        "files",
        "config://app",
        "App configuration file",
        uri="config://app",
    ),
    _doc(
        "resource_template",
        "docs",
        "note://{id}",
        "Fetch a note by id",
        uri="note://{id}",
        arguments=[{"name": "id", "description": "note id", "required": True}],
    ),
    _doc(
        "prompt",
        "docs",
        "summarize_note",
        "Summarize a note",
        arguments=[{"name": "uri", "description": "note uri", "required": True}],
    ),
]


@pytest.fixture
def tag_map():
    return {"files": ["local"], "docs": ["public"]}


@pytest.fixture
def meta(tag_map):
    return MetaTools(
        tool_index=ToolIndex(use_embeddings=False),
        execute_tool_fn=AsyncMock(return_value="ok"),
        get_server_tags=lambda s: tag_map.get(s, []),
        list_servers_fn=lambda: ["files", "docs"],
    )


# ── ToolIndex: kind 分離 ─────────────────────────────────────────────


async def test_kind_filter_keeps_tools_and_resources_apart(meta):
    await meta._index.rebuild(RESOURCE_DOCS)

    tools = meta._index.search("file", 10, kinds=("tool",))
    assert [r["name"] for r in tools] == ["file_read"]
    # ツール結果の形状は不変（kind キーを足さない）
    assert "kind" not in tools[0]

    resources = meta._index.search("file", 10, kinds=("resource",))
    assert [r["name"] for r in resources] == ["config://app"]
    assert resources[0]["kind"] == "resource"
    assert resources[0]["uri"] == "config://app"


async def test_resource_template_and_prompt_expose_arguments(meta):
    await meta._index.rebuild(RESOURCE_DOCS)

    hits = meta._index.search("note", 5, kinds=("resource_template",))
    assert len(hits) == 1
    assert hits[0]["kind"] == "resource_template"
    assert hits[0]["uri"] == "note://{id}"
    assert hits[0]["arguments"][0]["name"] == "id"

    prompts = meta._index.search("summarize note", 5, kinds=("prompt",))
    assert [r["name"] for r in prompts] == ["summarize_note"]
    assert prompts[0]["arguments"][0]["required"] is True


async def test_unknown_kind_documents_count_as_tools(meta):
    """kind 無し doc（既存のツール index）は tool として扱う（後方互換）。"""
    await meta._index.rebuild(
        [{"server": "s", "name": "t", "description": "thing", "inputSchema": {}}]
    )
    assert [r["name"] for r in meta._index.search("thing", 5, kinds=("tool",))] == ["t"]
    assert meta._index.search("thing", 5, kinds=("prompt",)) == []


# ── MetaTools.search_resources ───────────────────────────────────────


async def test_search_resources_returns_resources_and_prompts_only(meta):
    await meta._index.rebuild(RESOURCE_DOCS)

    data = json.loads(await meta.search_resources("note", top_k=5))
    kinds = {r["kind"] for r in data["results"]}
    assert kinds == {"resource_template", "prompt"}
    assert data["note"]
    assert data["servers"] == {"docs": ""}


async def test_search_resources_kind_filter_accepts_aliases(meta):
    await meta._index.rebuild(RESOURCE_DOCS)

    data = json.loads(await meta.search_resources("note", kind="prompts"))
    assert [r["name"] for r in data["results"]] == ["summarize_note"]

    data = json.loads(await meta.search_resources("note", kind="resource_templates"))
    assert [r["kind"] for r in data["results"]] == ["resource_template"]


async def test_search_resources_rejects_unknown_kind(meta):
    data = json.loads(await meta.search_resources("note", kind="bogus"))
    assert "error" in data
    assert data["kinds"]


async def test_search_resources_applies_tag_filter(meta):
    await meta._index.rebuild(RESOURCE_DOCS)
    token = request_tags.set(["public"])
    try:
        data = json.loads(await meta.search_resources("note file", top_k=10))
    finally:
        request_tags.reset(token)
    assert {r["server"] for r in data["results"]} == {"docs"}
    assert all(r["kind"] != "tool" for r in data["results"])


async def test_search_resources_tag_filter_reports_exclusion(meta):
    await meta._index.rebuild(RESOURCE_DOCS)
    token = request_tags.set(["nothing"])
    try:
        data = json.loads(await meta.search_resources("note"))
    finally:
        request_tags.reset(token)
    assert data["results"] == []
    assert "タグ" in data["hint"]


async def test_search_resources_scopes_to_server(meta):
    await meta._index.rebuild(RESOURCE_DOCS)

    data = json.loads(await meta.search_resources("note file", top_k=10, server="Docs"))
    assert {r["server"] for r in data["results"]} == {"docs"}

    empty = json.loads(await meta.search_resources("note", server="files"))
    assert empty["results"] == []
    assert "files" in empty["hint"]


async def test_search_resources_server_outside_tags_is_forbidden(meta):
    await meta._index.rebuild(RESOURCE_DOCS)
    token = request_tags.set(["public"])
    try:
        data = json.loads(await meta.search_resources("file", server="files"))
    finally:
        request_tags.reset(token)
    assert data["results"] == []
    assert "利用できません" in data["hint"]


async def test_search_resources_unknown_server_is_not_found(meta):
    await meta._index.rebuild(RESOURCE_DOCS)
    data = json.loads(await meta.search_resources("note", server="nope"))
    assert data["results"] == []
    assert "not found" in data["message"].lower()


# ── 既存 search_tools の後方互換 ─────────────────────────────────────


async def test_search_tools_does_not_leak_resources(meta):
    await meta._index.rebuild(RESOURCE_DOCS)
    data = json.loads(await meta.search_tools("note file", top_k=10))
    assert [r["name"] for r in data["results"]] == ["file_read"]


# ── rebuild_index がリソース/プロンプトを索引化する ───────────────────


def _proxy(tools=(), resources=(), templates=(), prompts=()):
    proxy = MagicMock()
    proxy.list_tools = AsyncMock(return_value=list(tools))
    proxy.list_resources = AsyncMock(return_value=list(resources))
    proxy.list_resource_templates = AsyncMock(return_value=list(templates))
    proxy.list_prompts = AsyncMock(return_value=list(prompts))
    return proxy


def _mock_pm(proxies):
    pm = MagicMock()
    pm._proxies = dict(proxies)

    async def _list_tools_for_server(name, proxy):
        return await proxy.list_tools()

    pm.list_tools_for_server = AsyncMock(side_effect=_list_tools_for_server)
    pm.get_connected_servers = MagicMock(side_effect=lambda: dict(pm._proxies))
    pm.server_tags = MagicMock(return_value=["demo"])
    pm.server_description = MagicMock(return_value="")
    pm.server_tool_search_desc = MagicMock(return_value={})
    pm.call_tool = AsyncMock(return_value="ok")
    return pm


async def test_rebuild_index_includes_resources_templates_and_prompts():
    proxy = _proxy(
        tools=[SimpleNamespace(name="demo_tool", description="Do a thing", parameters={})],
        resources=[
            SimpleNamespace(name="cfg", uri="cfg://a", description="Config resource")
        ],
        templates=[
            SimpleNamespace(
                name="note",
                uri_template="note://{id}",
                description="Note template",
                parameters={
                    "type": "object",
                    "properties": {"id": {"type": "string", "description": "note id"}},
                    "required": ["id"],
                },
            )
        ],
        prompts=[
            SimpleNamespace(
                name="greet",
                description="Greeting prompt",
                arguments=[
                    SimpleNamespace(name="who", description="target", required=True)
                ],
            )
        ],
    )
    app = await create_meta_app(_mock_pm({"demo": proxy}), use_embeddings=False)
    await app.rebuild_index()

    res = app.index.search("config resource", 5, kinds=("resource",))
    assert [r["uri"] for r in res] == ["cfg://a"]
    templates = app.index.search("note", 5, kinds=("resource_template",))
    assert [r["uri"] for r in templates] == ["note://{id}"]
    assert templates[0]["arguments"] == [
        {"name": "id", "description": "note id", "required": True}
    ]
    prompts = app.index.search("greeting", 5, kinds=("prompt",))
    assert prompts[0]["arguments"] == [
        {"name": "who", "description": "target", "required": True}
    ]
    # 既存のツール経路は影響を受けない
    assert [r["name"] for r in app.index.search("thing", 5, kinds=("tool",))] == [
        "demo_tool"
    ]


async def test_rebuild_index_derives_template_arguments_from_uri_template():
    """実 SDK の ResourceTemplate は parameters を持たないので、
    uri_template のプレースホルダから引数名を拾う。"""
    proxy = _proxy(
        templates=[
            SimpleNamespace(
                name="file",
                uri_template="file://{+path}",
                description="A file by absolute URI",
            )
        ]
    )
    app = await create_meta_app(_mock_pm({"demo": proxy}), use_embeddings=False)
    await app.rebuild_index()

    templates = app.index.search("file", 5, kinds=("resource_template",))
    assert templates[0]["arguments"] == [
        {"name": "path", "description": "", "required": True}
    ]


async def test_rrf_fuse_preserves_non_tool_fields():
    """埋め込み有効時の RRF 融合でも kind/uri/arguments が落ちない。"""
    resource = {
        "server": "files",
        "name": "cfg",
        "description": "Config",
        "tags": ["local"],
        "inputSchema": {},
        "kind": "resource",
        "uri": "cfg://a",
        "arguments": [{"name": "id", "description": "", "required": True}],
        "score": 1.0,
    }
    fused = ToolIndex._rrf_fuse([resource], [resource])
    assert fused[0]["kind"] == "resource"
    assert fused[0]["uri"] == "cfg://a"
    assert fused[0]["arguments"] == resource["arguments"]


async def test_rebuild_index_survives_listing_failures():
    """list_resources 等が落ちてもツール索引は壊れない。"""
    proxy = _proxy(
        tools=[SimpleNamespace(name="demo_tool", description="Do a thing", parameters={})]
    )
    proxy.list_resources = AsyncMock(side_effect=RuntimeError("boom"))
    proxy.list_prompts = AsyncMock(side_effect=RuntimeError("boom"))
    proxy.list_resource_templates = AsyncMock(side_effect=RuntimeError("boom"))
    app = await create_meta_app(_mock_pm({"demo": proxy}), use_embeddings=False)
    assert await app.rebuild_index() == []
    assert [r["name"] for r in app.index.search("thing", 5, kinds=("tool",))] == [
        "demo_tool"
    ]


# ── 管理 API ─────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as c:
        yield c


async def test_admin_discovery_search_returns_all_kinds(client):
    index = ToolIndex(use_embeddings=False)
    await index.rebuild(RESOURCE_DOCS)
    prev = app_state.meta_app
    app_state.meta_app = SimpleNamespace(index=index)
    try:
        r = client.get("/admin/api/discovery/search", params={"query": "note file"})
        assert r.status_code == 200
        body = r.json()
        kinds = {hit["kind"] for hit in body["results"]}
        assert kinds == {"tool", "resource_template", "prompt"} or "tool" in kinds
        filtered = client.get(
            "/admin/api/discovery/search",
            params={"query": "note", "kind": "prompts"},
        )
        assert filtered.status_code == 200
        assert [h["kind"] for h in filtered.json()["results"]] == ["prompt"]
        bad = client.get(
            "/admin/api/discovery/search", params={"query": "note", "kind": "bogus"}
        )
        assert bad.status_code == 400
    finally:
        app_state.meta_app = prev


async def test_admin_discovery_search_without_meta_app(client):
    prev = app_state.meta_app
    app_state.meta_app = None
    try:
        r = client.get("/admin/api/discovery/search", params={"query": "note"})
        assert r.status_code == 200
        assert r.json()["results"] == []
    finally:
        app_state.meta_app = prev
