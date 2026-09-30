"""メタ経路のタグフィルタ（カタログ差し替え）と search_tools(server=...) のテスト。

背景（実測で確認済みの欠陥）:
- search_tools の description には rebuild が全サーバーの一行カタログを焼き込む。
  メタ経路には TagFilterMiddleware が居らず（normal アプリのみ）、タグフィルタは
  meta_provider が request_tags を直接読んで実装しているため、**カタログ文字列は
  誰も直していなかった**。X-MCP-Hub-Tags: dev,herta のクライアントは catalog で
  microsandbox を見て search_tools("microsandbox") を叩き 0 件になる（実測ログ）。
- search_tools に server 引数が無く、モデルは自然に {"query":..., "server":...} を
  送って Unexpected エラーになる（qwen3.7-plus 実測 3 回）。

ここは MetaCatalogMiddleware（カタログ差し替え）と
MetaTools.search_tools(server=...)（絞り込み）の契約を拘束する。
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import mcp.types as mt
import pytest
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.base import Tool

from mcp_hub.meta_provider import (
    CATALOG_MARKER,
    _TOOL_FORBIDDEN,
    _TOOL_NOT_FOUND,
    MetaTools,
    ToolIndex,
    create_meta_app,
)
from mcp_hub.state import request_tags
from mcp_hub.tag_filter import MetaCatalogMiddleware

# ── helpers ──────────────────────────────────────────────────────────────────


def _pm(servers: dict[str, list[str]], tags: dict[str, list[str]] | None = None):
    """{server: [tool_names]} の ProxyManager モック（server_tags 付き）。"""
    tags = tags or {}
    pm = MagicMock()
    pm._proxies = {}
    pm.call_tool = AsyncMock(return_value="ok")
    pm.get_connected_servers = MagicMock(side_effect=lambda: dict(pm._proxies))
    pm.server_description = MagicMock(return_value="")

    async def _list_tools_for_server(name, proxy):
        return await proxy.list_tools()

    pm.list_tools_for_server = AsyncMock(side_effect=_list_tools_for_server)
    pm.server_tags = MagicMock(side_effect=lambda n: list(tags.get(n, [])))
    for name, tool_names in servers.items():
        proxy = MagicMock()
        proxy.list_tools = AsyncMock(
            return_value=[
                SimpleNamespace(
                    name=t,
                    description=f"{t} description",
                    parameters={"type": "object"},
                )
                for t in tool_names
            ]
        )
        pm._proxies[name] = proxy
    return pm


async def _app(pm, servers: dict[str, list[str]], tags: dict[str, list[str]] | None = None):
    """create_meta_app + 実 rebuild 相当のセットアップ。

    実 rebuild_index は上流 list_tools を叩くので、ここでは同じ素材（entries）で
    焼き込みだけを再現する（_apply_catalog / catalog_for_tags が読む
    self._catalog_entries を直接埋める）。
    """
    app = await create_meta_app(pm, use_embeddings=False)
    entries = [
        {"server": s, "description": pm.server_description(s), "tools": list(names)}
        for s, names in servers.items()
    ]
    app._catalog_entries[:] = entries
    tool = await _stored_tool(app)
    assert tool is not None
    tool.description = (
        app.base_descriptions["search_tools"] + "\n\n" + app.catalog_for_tags(None)
    )
    return app


def _catalog_servers(text: str) -> list[str]:
    """description 中のカタログ行からサーバー名を拾う（表示順のまま）。"""
    return [ln.split(" (")[0][2:] for ln in text.splitlines() if ln.startswith("- ")]


def _desc_of(tool: object) -> str:
    return getattr(tool, "description", None) or ""


async def _stored_tool(app):
    """provider が保持する（= リクエスト間で共有される）search_tools の実体。"""
    tool = await app.mcp.get_tool("search_tools")
    assert tool is not None
    return tool


async def _rewrite(app, tags: list[str] | None, tools):
    """MetaCatalogMiddleware を実 description に対して 1 回走らせる。"""
    mw = MetaCatalogMiddleware(app.catalog_for_tags)

    async def call_next(context):  # noqa: ARG001
        return tools

    context = cast("MiddlewareContext[mt.ListToolsRequest]", SimpleNamespace(message=None))
    request_tags.set(tags)
    try:
        return list(await mw.on_list_tools(context, call_next))
    finally:
        request_tags.set(None)


async def _search_meta(servers: dict[str, list[str]], tags: dict[str, list[str]] | None = None):
    """index 直構築の MetaTools（create_meta_app の配線と同じ引数）。"""
    tags = tags or {}
    idx = ToolIndex(use_embeddings=False)
    docs = [
        {
            "server": srv,
            "name": name,
            "description": f"{name} description",
            "index_text": f"{name} description",
            "search_desc": "",
            "full_description": f"{name} description",
            "tags": list(tags.get(srv, [])),
            "inputSchema": {"type": "object"},
        }
        for srv, names in servers.items()
        for name in names
    ]
    await idx.rebuild(docs)
    meta = MetaTools(
        tool_index=idx,
        execute_tool_fn=lambda s, t, a: None,
        get_server_tags=lambda n: list(tags.get(n, [])),
        list_servers_fn=lambda: list(servers),
    )
    return meta, idx


def _searchable(n: int, server_tag: str = "") -> list[str]:
    """"search tool" にヒットする n 件のツール名。"""
    return [f"{server_tag}tool_{i}" for i in range(n)]


# ── MetaCatalogMiddleware: カタログのタグ差し替え ────────────────────────────


class TestMetaCatalogMiddleware:
    async def test_only_visible_servers_in_catalog(self):
        """タグ可視のサーバー行だけが残り、件数も差し替わる。"""
        pm = _pm(
            {"alpha": ["a_tool"], "beta": ["b_tool"]},
            {"alpha": ["dev"], "beta": ["herta"]},
        )
        app = await _app(pm, {"alpha": ["a_tool"], "beta": ["b_tool"]})
        stored = await _stored_tool(app)
        out = await _rewrite(app, ["dev"], [stored])
        desc = _desc_of(out[0])

        assert _catalog_servers(desc) == ["alpha"]
        assert f"{CATALOG_MARKER} (1)." in desc
        assert desc.endswith("- alpha (1 tool): a_tool")
        assert "beta" not in desc

    async def test_all_visible_tags_reproduce_baked_catalog(self):
        """全サーバー可視のタグなら焼き込みカタログと完全一致（組立の同一性）。"""
        pm = _pm(
            {"alpha": ["a_tool"], "beta": ["b_tool"]},
            {"alpha": ["dev"], "beta": ["herta"]},
        )
        app = await _app(pm, {"alpha": ["a_tool"], "beta": ["b_tool"]})
        stored = await _stored_tool(app)
        both = (await _rewrite(app, ["dev", "herta"], [stored]))[0].description
        assert both == stored.description

    async def test_no_tags_is_byte_identical(self):
        """タグヘッダが無ければ description はバイト単位で不変。"""
        pm = _pm(
            {"alpha": ["a_tool"], "beta": ["b_tool"]},
            {"alpha": ["dev"], "beta": ["herta"]},
        )
        app = await _app(pm, {"alpha": ["a_tool"], "beta": ["b_tool"]})
        stored = await _stored_tool(app)
        out = await _rewrite(app, None, [stored])
        assert out[0].description == stored.description
        assert out[0] is stored  # 差し替えなし＝元オブジェクトをそのまま返す

    async def test_stored_tool_not_mutated_and_repeatable(self):
        """共有オブジェクトを壊さない（リクエスト間リークの回帰）。"""
        pm = _pm({"alpha": ["a_tool"], "beta": ["b_tool"]}, {"alpha": ["dev"]})
        app = await _app(pm, {"alpha": ["a_tool"], "beta": ["b_tool"]})
        stored = await _stored_tool(app)
        before = stored.description

        first = (await _rewrite(app, ["dev"], [stored]))[0].description
        second = (await _rewrite(app, ["dev"], [stored]))[0].description
        third = (await _rewrite(app, ["herta"], [stored]))[0].description

        assert first == second  # 同一タグで 2 回叩いて同一
        assert first != third
        # stored（provider 保持の実体）は書き換わらない
        assert stored.description == before
        assert (await app.mcp.get_tool("search_tools")).description == before
        # タグ無し経路は依然として全件カタログ
        assert _catalog_servers((await _rewrite(app, None, [stored]))[0].description) == [
            "alpha",
            "beta",
        ]

    async def test_truncation_marker_uses_visible_count(self):
        """可視サーバーが多い時も _build_catalog と同じ打ち切りに従う。"""
        servers = {f"srv{i:03d}": [f"tool_{i}"] for i in range(60)}
        pm = _pm(servers, {name: ["dev"] for name in servers})
        app = await _app(pm, servers)
        stored = await _stored_tool(app)
        desc = (await _rewrite(app, ["dev"], [stored]))[0].description

        section = desc[desc.index(CATALOG_MARKER) :]
        # _build_catalog の打ち切り（_CATALOG_MAX_CHARS）が可視件数でも効く。
        assert re.search(r"\.\.\. and \d+ more servers$", section), section[-80:]
        header_count = int(re.search(rf"{CATALOG_MARKER} \((\d+)\)", desc).group(1))
        assert header_count == 60
        visible_lines = _catalog_servers(desc.rsplit("\n... and ", 1)[0])
        assert 0 < len(visible_lines) < 60
        # 全可視（タグ無し相当）のカタログと同一（打ち切り位置も同じ）
        assert section == app.catalog_for_tags(None)

    async def test_no_visible_server_drops_catalog_section(self):
        """可視サーバー 0 件ならカタログ節ごと落とし、base だけ残す。"""
        pm = _pm({"alpha": ["a_tool"]}, {"alpha": ["dev"]})
        app = await _app(pm, {"alpha": ["a_tool"]})
        stored = await _stored_tool(app)
        desc = (await _rewrite(app, ["nope"], [stored]))[0].description

        assert CATALOG_MARKER not in desc
        assert desc == app.base_descriptions["search_tools"]

    async def test_other_tools_pass_through_untouched(self):
        """full_info_tools 等の他ツールは同一オブジェクトのまま残る（消すな）。"""
        pm = _pm({"alpha": ["a_tool"], "beta": ["b_tool"]}, {"alpha": ["dev"]})
        app = await _app(pm, {"alpha": ["a_tool"], "beta": ["b_tool"]})
        stored = await _stored_tool(app)
        extra = Tool(name="fetch_fetch", description="full info", parameters={})
        out = await _rewrite(app, ["dev"], [stored, extra])

        assert [t.name for t in out] == ["search_tools", "fetch_fetch"]
        assert out[1] is extra
        assert "beta" not in out[0].description


# ── MetaTools.search_tools(server=...) ──────────────────────────────────────


class TestSearchToolsServerArg:
    async def test_filters_to_one_server(self):
        meta, _ = await _search_meta(
            {"alpha": _searchable(3, "a_"), "beta": _searchable(3, "b_")}
        )
        data = json.loads(await meta.search_tools("search tool", top_k=5, server="alpha"))
        assert data["results"]
        assert {r["server"] for r in data["results"]} == {"alpha"}

    async def test_case_insensitive_server(self):
        meta, _ = await _search_meta({"alpha": _searchable(2, "a_")})
        lower = json.loads(await meta.search_tools("search tool", top_k=5, server="alpha"))
        upper = json.loads(await meta.search_tools("search tool", top_k=5, server="ALPHA"))
        assert lower == upper
        assert {r["server"] for r in upper["results"]} == {"alpha"}

    async def test_unknown_server_is_not_found_without_revealing(self):
        """存在しないサーバーは not found（可視件数のヒントを出さない）。"""
        meta, _ = await _search_meta({"alpha": _searchable(2, "a_")})
        out = await meta.search_tools("search tool", top_k=5, server="ghost")
        assert out == _TOOL_NOT_FOUND

    async def test_tag_out_of_range_is_forbidden(self):
        """タグ範囲外の 3 経路は権限なしの統一応答（not found と区別）。"""
        meta, _ = await _search_meta(
            {"alpha": _searchable(2, "a_"), "secret": _searchable(2, "s_")},
            tags={"alpha": ["dev"], "secret": ["nope"]},
        )
        request_tags.set(["dev"])
        try:
            search_out = await meta.search_tools("search tool", top_k=5, server="secret")
            schema_out = await meta.get_schema("secret", "s_tool_0")
            exec_out = await meta.execute_tool("secret", "s_tool_0", {})
        finally:
            request_tags.set(None)
        assert search_out == _TOOL_FORBIDDEN
        assert schema_out == _TOOL_FORBIDDEN
        assert exec_out == _TOOL_FORBIDDEN
        assert _TOOL_FORBIDDEN != _TOOL_NOT_FOUND
        for out in (search_out, schema_out, exec_out):
            assert "server_tags" not in json.loads(out)
        # 範囲内だが 0 件のヒント（サーバー名 + 件数）とは別物であること
        assert "このサーバーのツール" not in search_out

    async def test_unknown_server_stays_not_found(self):
        """不存在サーバーは _TOOL_FORBIDDEN ではなく _TOOL_NOT_FOUND のまま。"""
        meta, _ = await _search_meta(
            {"alpha": _searchable(2, "a_")}, tags={"alpha": ["dev"]}
        )
        # タグ無しは従来どおり。タグ付きでも index 不在は存在ゲート先行。
        assert (
            await meta.search_tools("search tool", top_k=5, server="ghost")
            == _TOOL_NOT_FOUND
        )
        assert await meta.get_schema("ghost", "x_tool") == _TOOL_NOT_FOUND
        request_tags.set(["dev"])
        try:
            assert (
                await meta.search_tools("search tool", top_k=5, server="ghost")
                == _TOOL_NOT_FOUND
            )
        finally:
            request_tags.set(None)

    async def test_searches_deeper_than_top_k(self):
        """top_k より深く引いてから絞る（他サーバーが上位を占有しても見つかる）。"""
        meta, idx = await _search_meta(
            {"other": _searchable(20, "o_"), "target": ["t_marker_tool"]}
        )
        # 前提: top_k=2 の生検索では target が上位 2 件に入らない（絞り込みが必須）。
        raw = [r["server"] for r in idx.search("search tool", 2)]
        assert "target" not in raw
        data = json.loads(
            await meta.search_tools("search tool", top_k=2, server="target")
        )
        assert [r["server"] for r in data["results"]] == ["target"]

    async def test_respects_top_k_after_filtering(self):
        meta, _ = await _search_meta({"alpha": _searchable(5, "a_")})
        data = json.loads(await meta.search_tools("search tool", top_k=2, server="alpha"))
        assert len(data["results"]) == 2

    async def test_empty_result_hint_names_server_and_visible_count(self):
        meta, _ = await _search_meta(
            {"alpha": ["read_file", "write_file"], "beta": ["something"]}
        )
        request_tags.set(None)
        data = json.loads(
            await meta.search_tools("zzzznope", top_k=3, server="alpha")
        )
        assert data["results"] == []
        assert "server=alpha" in data["hint"]
        assert "2 件" in data["hint"]

    async def test_server_none_ranking_is_unchanged(self):
        """server=None は index.search の順位をそのまま使う（一切変えない）。"""
        meta, idx = await _search_meta(
            {"alpha": _searchable(3, "a_"), "beta": _searchable(3, "b_")}
        )
        data = json.loads(await meta.search_tools("search tool", top_k=3))
        assert [(r["server"], r["name"]) for r in data["results"]] == [
            (r["server"], r["name"]) for r in idx.search("search tool", 3)
        ]

    async def test_server_none_still_applies_tag_filter(self):
        """server=None 経路のタグフィルタ挙動は不変。"""
        meta, _ = await _search_meta(
            {"alpha": _searchable(3, "a_"), "beta": _searchable(3, "b_")},
            tags={"alpha": ["dev"], "beta": ["herta"]},
        )
        request_tags.set(["dev"])
        try:
            data = json.loads(await meta.search_tools("search tool", top_k=5))
        finally:
            request_tags.set(None)
        assert {r["server"] for r in data["results"]} == {"alpha"}

    async def test_wire_exposes_server_param(self):
        """tools/list の search_tools 入力に server が載る（LLM が送れる）。"""
        pm = _pm({"alpha": ["a_tool"]})
        app = await _app(pm, {"alpha": ["a_tool"]})
        tool = await _stored_tool(app)
        assert "server" in tool.parameters["properties"]
        assert "server" in (tool.description or "")


# ── E2E: 実 HTTP 経路（TestClient + ASGI tag_middleware + FastMCP middleware）──


@pytest.fixture
async def wire_app():
    """ASGI の tag_middleware（request_tags を set）付きの meta アプリ。

    これが「request_tags が FastMCP ミドルウェア実行時に set 済みか」の実測。
    ヘッダ無しなら contextvar は None のまま＝焼き込みカタログがそのまま出る。
    """
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    pm = _pm(
        {"alpha": ["a_tool"], "beta": ["b_tool"]},
        {"alpha": ["dev"], "beta": ["herta"]},
    )
    app_meta = await _app(pm, {"alpha": ["a_tool"], "beta": ["b_tool"]})
    http = app_meta.mcp.http_app(
        transport="streamable-http", path="/", stateless_http=True
    )
    app = FastAPI(lifespan=http.lifespan)
    app.mount("/mcp-meta", http)

    @app.middleware("http")
    async def tag_middleware(request: Request, call_next):
        try:
            raw = request.headers.get("X-MCP-Hub-Tags", "")
            if raw:
                request_tags.set([t.strip() for t in raw.split(",") if t.strip()])
            return await call_next(request)
        finally:
            request_tags.set(None)

    with TestClient(app) as c:
        yield SimpleNamespace(client=c, app=app_meta, pm=pm)


class TestWireCatalog:
    """実 HTTP 経路（ASGI tag_middleware → FastMCP ミドルウェア）での検証。"""

    _HEADERS = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    def _desc(self, wire, tags: str | None) -> str:
        headers = dict(self._HEADERS)
        if tags:
            headers["X-MCP-Hub-Tags"] = tags
        r = wire.client.post(
            "/mcp-meta/",
            json={"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": "1"},
            headers=headers,
        )
        assert r.status_code == 200
        data = "".join(
            line[6:] for line in r.text.split("\n") if line.startswith("data: ")
        )
        tools = json.loads(data)["result"]["tools"]
        return next(t for t in tools if t["name"] == "search_tools")["description"]

    async def test_tags_change_catalog_over_the_wire(self, wire_app):
        no_tags = self._desc(wire_app, None)
        dev = self._desc(wire_app, "dev")
        herta = self._desc(wire_app, "herta")

        assert _catalog_servers(no_tags) == ["alpha", "beta"]
        assert _catalog_servers(dev) == ["alpha"]
        assert _catalog_servers(herta) == ["beta"]
        assert len({no_tags, dev, herta}) == 3  # 3 設定で全違い＝漏洩なし

    async def test_repeat_same_tags_is_identical(self, wire_app):
        first = self._desc(wire_app, "dev")
        again = self._desc(wire_app, "dev")
        assert first == again
        # 挟んだ別タグのリクエストが次に影響しない（リークなし）
        self._desc(wire_app, None)
        assert self._desc(wire_app, "dev") == first

    async def test_no_tags_matches_baked_bytes(self, wire_app):
        """タグ無しは焼き込み（provider 保持の stored tool）と同一バイト。"""
        no_tags = self._desc(wire_app, None)
        baked = (await wire_app.app.mcp.get_tool("search_tools")).description
        assert no_tags == baked
        assert _catalog_servers(no_tags) == ["alpha", "beta"]

    async def test_other_tools_survive_over_the_wire(self, wire_app):
        """full_info_tools 等が middleware で消えない（追記は追記のまま）。"""
        headers = dict(self._HEADERS)
        r = wire_app.client.post(            "/mcp-meta/",
            json={"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": "1"},
            headers=headers,
        )
        data = "".join(
            line[6:] for line in r.text.split("\n") if line.startswith("data: ")
        )
        names = [t["name"] for t in json.loads(data)["result"]["tools"]]
        assert set(names) == {"search_tools", "execute_tool", "get_schema"}

    async def test_full_info_tools_survive_tags_rewrite(self, wire_app, monkeypatch):
        """full_info_tools の追記がタグ差し替えと同居しても消えない（実 middleware 構成）。

        本体が実測した干渉（FullInfoMiddleware.on_list_tools は追記のみ）を
        実 HTTP 経路で固定する。
        """
        from mcp_hub.full_info import FullInfoMiddleware
        from mcp_hub.state import app_state

        monkeypatch.setattr(
            app_state,
            "registry",
            SimpleNamespace(_data={"full_info_tools": ["alpha_a_tool"]}),
        )
        wire_app.app.mcp.add_middleware(
            FullInfoMiddleware(
                wire_app.pm,
                get_schema_fn=lambda s, t: {
                    "description": "full info",
                    "inputSchema": {"type": "object"},
                },
            )
        )

        def _list(tags: str | None) -> tuple[list[str], str]:
            headers = dict(self._HEADERS)
            if tags:
                headers["X-MCP-Hub-Tags"] = tags
            r = wire_app.client.post(
                "/mcp-meta/",
                json={"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": "1"},
                headers=headers,
            )
            assert r.status_code == 200
            data = "".join(
                line[6:] for line in r.text.split("\n") if line.startswith("data: ")
            )
            tools = json.loads(data)["result"]["tools"]
            desc = next(t for t in tools if t["name"] == "search_tools")["description"]
            return [t["name"] for t in tools], desc

        names, desc = _list("dev")
        assert "alpha_a_tool" in names  # 追記されたツールは残る
        assert {"search_tools", "execute_tool", "get_schema"} <= set(names)
        assert _catalog_servers(desc) == ["alpha"]  # かつカタログは絞られている

        names, _ = _list(None)
        assert "alpha_a_tool" in names


class TestRebuildSharesCatalogEntries:
    """実 rebuild 経路での ``_catalog_entries`` 共有を拘束する。

    他のテストは ``app._catalog_entries`` を直接埋めて焼き込みを再現するため、
    ``MetaApp.__init__`` が ``list(catalog_entries)`` とコピーして共有を断っても
    通ってしまう（#003 の変異試験(g) が生存した）。ここだけは実際に
    ``rebuild_index`` を回し、closure の ``catalog_entries`` と MetaApp 側が
    同一オブジェクトであることを振る舞いで見る。
    """

    async def test_rebuild_reflects_in_catalog_for_tags(self):
        pm = _pm(
            {"alpha": ["a_tool"], "beta": ["b_tool"]},
            {"alpha": ["dev"], "beta": ["herta"]},
        )
        app = await create_meta_app(pm, use_embeddings=False)
        # rebuild 前は素材が空（create_meta_app は焼き込まない）
        assert _catalog_servers(app.catalog_for_tags(None)) == []

        await app.rebuild_index()

        # 焼き込み（stored tool）と catalog_for_tags が同じ素材を見ている
        baked = _desc_of(await _stored_tool(app))
        assert _catalog_servers(baked) == ["alpha", "beta"]
        assert _catalog_servers(app.catalog_for_tags(None)) == ["alpha", "beta"]
        # タグ絞りも同じ素材から引ける（__init__ でコピーするとここが空になる）
        assert _catalog_servers(app.catalog_for_tags(["dev"])) == ["alpha"]
        assert _catalog_servers(app.catalog_for_tags(["herta"])) == ["beta"]

    async def test_second_rebuild_replaces_entries(self):
        """rebuild を重ねても古い素材が残らない（slice 代入で置換される）。"""
        pm = _pm({"alpha": ["a_tool"]}, {"alpha": ["dev"]})
        app = await create_meta_app(pm, use_embeddings=False)
        await app.rebuild_index()
        assert _catalog_servers(app.catalog_for_tags(None)) == ["alpha"]

        pm._proxies.pop("alpha")
        await app.rebuild_index()
        assert _catalog_servers(app.catalog_for_tags(None)) == []
