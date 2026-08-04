# フル公開（メタOFF）機能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** meta モードで `full_info_tools` に指定されたツールだけを tools/list に通常ツールとしてフル公開し、tools/call で直接呼べるようにする。

**Architecture:** fastmcp middleware（meta_app に追加）で `on_list_tools` に Tool を追加し、`on_call_tool` では execute_tool 経由をスキップして `proxy_manager.call_tool` に直接転送する。設定は hub.config.json トップレベルの `full_info_tools` リスト（`app_state.registry._data` を同期参照・キャッシュなし）。

**Tech Stack:** Python 3.12 / fastmcp 3.4.4（.venv 内）/ pytest（`rtk pytest` で実行）/ 素の JS（`src/mcp_hub/static/index.html` 単一ファイル）

## Global Constraints

- テスト実行は `rtk pytest tests/<file> -v`（rtk 経由。直接 pytest は使わない）
- `git add` は変更対象ファイルのみ。作業ツリーに他タスクの WIP（README.md 変更、docs/ 削除・未追跡）が残っているので触らない
- コミットメッセージ: `feat(full-info): ...` / `test(full-info): ...`。`--no-verify` 不使用
- middleware チェーンは「先に add した方が外側（先に実行）」（fastmcp server.py:514 `reversed`）。**FullInfoMiddleware は ToolLogMiddleware（main.py:172）の直後に add する** — 直接転送（call_next スキップ）でも ToolLog が必ず実行されログ・metrics が保証される
- `server.tools[].name` は既に `"{server}_{tool}"` 形式（proxy_manager の mount(namespace=name) 経由）。**WebUI は tool.name をそのまま full_info_tools エントリに使う**（二重プレフィックス禁止）
- middleware の設定参照は `app_state.registry._data` の同期読み（store の `_write_internal` が常に最新化。invalidate_cache 不要）
- Tool 構築時の SEP-986 名規則例外は try/except で捕捉してスキップ
- XSS: クライアント由来文字列は必ず `escapeHtml` 経由（innerHTML に生文字列を入れない）
- async テストは `asyncio.run()` で実行（pytest-asyncio 不使用、既存 test_log_middleware.py と同じ流儀）
- 統合テストの TestClient パターンは tests/test_log_middleware.py:118-144 を踏襲（`monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))` → `create_app()` → `with TestClient(app)`）
- ブラウザ実機確認は orchestrator が担当（puppeteer MCP は Tailscale IP `100.112.180.92` 経由。127.0.0.1 は不可）

---

### Task 1: 共通名前分解関数 split_qualified_name

**Files:**
- Modify: `src/mcp_hub/middleware.py`（`resolve_server` の直後、:53 の後）
- Test: `tests/test_full_info.py`（新規作成）

**Interfaces:**
- Consumes: なし
- Produces: `split_qualified_name(name: str, connected: dict[str, Any]) -> tuple[str, str]` — `"{server}_{tool}"` 形式の名前を (server, tool) に分解。Task 2 の FullInfoMiddleware が on_list_tools / on_call_tool の両方で使用する

- [ ] **Step 1: テストファイル tests/test_full_info.py を作成（split_qualified_name のテストのみ）**

```python
"""FullInfoMiddleware / split_qualified_name のテスト。"""
from mcp_hub.middleware import split_qualified_name


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
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `rtk pytest tests/test_full_info.py -v`
Expected: FAIL with `ImportError: cannot import name 'split_qualified_name'`

- [ ] **Step 3: middleware.py に split_qualified_name を実装**

`src/mcp_hub/middleware.py` の `resolve_server`（:53 の return 後）の直後に追加:

```python
def split_qualified_name(name: str, connected: dict[str, Any]) -> tuple[str, str]:
    """'{server}_{tool}' 形式の名前を (server, tool) に分解する。

    connected のサーバー名のうち、name が f"{server}_" で始まる接頭辞の
    最長一致で server を特定し、残りを tool として返す。
    マッチしなければ ("-", name) を返す。
    full_info_tools のエントリ照合と on_call_tool の名前分解で共用する。
    """
    best: str | None = None
    for server in connected:
        if name.startswith(f"{server}_"):
            if best is None or len(server) > len(best):
                best = server
    if best is None:
        return "-", name
    return best, name[len(best) + 1:]
```

- [ ] **Step 4: テストが通ることを確認**

Run: `rtk pytest tests/test_full_info.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 回帰確認**

Run: `rtk pytest tests/test_log_middleware.py -v`
Expected: PASS（7 passed、既存 resolve_server のテストに影響なし）

- [ ] **Step 6: コミット**

```bash
git add src/mcp_hub/middleware.py tests/test_full_info.py
git commit -m "feat(full-info): '{server}_{tool}' を分解する split_qualified_name を追加"
```

---

### Task 2: FullInfoMiddleware（full_info.py 新規）

**Files:**
- Create: `src/mcp_hub/full_info.py`
- Test: `tests/test_full_info.py`（追記）

**Interfaces:**
- Consumes: `split_qualified_name`（Task 1）、`app_state.registry._data["full_info_tools"]`、`proxy_manager.get_connected_servers()` / `proxy_manager.call_tool(server, tool, arguments)`（既存）、`ToolIndex.get_schema(server, tool_name) -> dict|None`（既存 meta_provider.py:283-293）
- Produces: `FullInfoMiddleware(proxy_manager, get_schema_fn: Callable[[str, str], dict|None])` — Task 4 の main.py が `FullInfoMiddleware(proxy_manager, get_schema_fn=meta_app.index.get_schema)` で登録する

- [ ] **Step 1: full_info.py を作成**

`src/mcp_hub/full_info.py`:

```python
"""フル公開（メタOFF）機能用 middleware。

meta モードで hub.config.json の full_info_tools に指定されたツール
（"{server}_{tool}" 形式）を、tools/list に通常ツールとして追加し、
tools/call では execute_tool 経由をスキップして直接下流サーバーへ転送する。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool, ToolResult
from mcp.types import TextContent

from .middleware import split_qualified_name
from .state import app_state

if TYPE_CHECKING:
    import mcp.types as mt

logger = logging.getLogger(__name__)

# (server, tool_name) -> ツール定義 dict（{name, description, server, inputSchema}）| None
GetSchemaFn = Callable[[str, str], dict | None]


class FullInfoMiddleware(Middleware):
    """full_info_tools に指定されたツールをフル公開する。"""

    def __init__(self, proxy_manager, get_schema_fn: GetSchemaFn) -> None:
        super().__init__()
        self._pm = proxy_manager
        self._get_schema = get_schema_fn

    def _full_info_entries(self) -> set[str]:
        """hub.config.json の full_info_tools を同期参照する（キャッシュ不要）。"""
        registry = app_state.registry
        if registry is None:
            return set()
        data = getattr(registry, "_data", None) or {}
        entries = data.get("full_info_tools") or []
        return {e for e in entries if isinstance(e, str)}

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = list(await call_next(context))
        connected = self._pm.get_connected_servers()
        for entry in sorted(self._full_info_entries()):
            server, tool_name = split_qualified_name(entry, connected)
            if server == "-":
                continue  # 未接続サーバーのエントリは除外
            schema = self._get_schema(server, tool_name)
            if schema is None:
                continue  # ツール不在は除外
            try:
                tools.append(
                    Tool(
                        name=entry,
                        description=schema.get("description") or "",
                        parameters=schema.get("inputSchema") or {},
                    )
                )
            except Exception:
                # SEP-986 名規則違反などはスキップ
                logger.warning("full_info_tools のツール追加をスキップ: %s", entry)
        return tools

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        name = context.message.name
        if name not in self._full_info_entries():
            return await call_next(context)
        connected = self._pm.get_connected_servers()
        server, tool_name = split_qualified_name(name, connected)
        if server == "-":
            return ToolResult(is_error=True, content=[TextContent(text=f"Server for {name!r} not found")])
        arguments = context.message.arguments or {}
        try:
            return await self._pm.call_tool(server, tool_name, arguments)
        except Exception as e:
            return ToolResult(is_error=True, content=[TextContent(text=str(e))])
```

- [ ] **Step 2: tests/test_full_info.py に FullInfoMiddleware のテストを追記**

`tests/test_full_info.py` の末尾に追加:

```python
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
    if tool_name == "missing":
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
        pm.call_tool = AsyncMock(return_value=ToolResult(content=[TextContent(text="ok")]))
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
```

- [ ] **Step 3: テストが失敗することを確認**

Run: `rtk pytest tests/test_full_info.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mcp_hub.full_info'`

- [ ] **Step 4: テストが通ることを確認**

Run: `rtk pytest tests/test_full_info.py -v`
Expected: PASS（7 passed: split 3 + list 2 + call 4 = 9 passed）

- [ ] **Step 5: コミット**

```bash
git add src/mcp_hub/full_info.py tests/test_full_info.py
git commit -m "feat(full-info): meta モードでツールをフル公開する FullInfoMiddleware を追加"
```

---

### Task 3: store.set_full_info_tools + /settings API 拡張

**Files:**
- Modify: `src/mcp_hub/store.py`（`set_embedding_model` :200-204 の直後）
- Modify: `src/mcp_hub/admin_router.py:80-95`（GET/PATCH /settings）
- Test: `tests/test_full_info_api.py`（新規作成）

**Interfaces:**
- Consumes: なし
- Produces: `JsonStore.set_full_info_tools(tools: list[str]) -> None`、`GET /admin/api/settings` が `full_info_tools` を返す、`PATCH /admin/api/settings` が `full_info_tools` を受け付ける（不正は 422）。Task 4 の統合テストと Task 5 の WebUI が使用

- [ ] **Step 1: テストファイル tests/test_full_info_api.py を作成**

```python
"""full_info_tools 設定 API のテスト。"""
import pytest
from fastapi.testclient import TestClient

from mcp_hub.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as c:
        yield c


class TestFullInfoSettingsAPI:
    def test_get_settings_includes_full_info_tools(self, client):
        r = client.get("/admin/api/settings")
        assert r.status_code == 200
        body = r.json()
        assert "full_info_tools" in body
        assert body["full_info_tools"] == []

    def test_patch_sets_full_info_tools(self, client):
        r = client.patch("/admin/api/settings", json={"full_info_tools": ["fetch_fetch"]})
        assert r.status_code == 200
        assert r.json()["full_info_tools"] == ["fetch_fetch"]

        r2 = client.get("/admin/api/settings")
        assert r2.json()["full_info_tools"] == ["fetch_fetch"]

    def test_patch_rejects_non_list(self, client):
        r = client.patch("/admin/api/settings", json={"full_info_tools": "fetch_fetch"})
        assert r.status_code == 422

    def test_patch_rejects_entry_without_underscore(self, client):
        r = client.patch("/admin/api/settings", json={"full_info_tools": ["fetch"]})
        assert r.status_code == 422

    def test_patch_rejects_non_string_entry(self, client):
        r = client.patch("/admin/api/settings", json={"full_info_tools": [123]})
        assert r.status_code == 422

    def test_patch_meta_mode_still_works(self, client):
        r = client.patch("/admin/api/settings", json={"meta_mode": True})
        assert r.status_code == 200
        assert r.json()["meta_mode"] is True
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `rtk pytest tests/test_full_info_api.py -v`
Expected: FAIL（GET /settings に `full_info_tools` が無い、PATCH が 422 を返さない等）

- [ ] **Step 3: store.py に set_full_info_tools を実装**

`src/mcp_hub/store.py` の `set_embedding_model`（:200-204）の直後に追加:

```python
    async def set_full_info_tools(self, tools: list[str]) -> None:
        """フル公開ツール一覧（"{server}_{tool}" 形式）を保存する。"""
        async with self._lock:
            data = await self._read_locked()
            data["full_info_tools"] = list(tools)
            await self._write_internal(data)
```

- [ ] **Step 4: admin_router.py の GET /settings を拡張**

`src/mcp_hub/admin_router.py:80-86` を以下の形に変更:

```python
@router.get("/settings")
async def get_settings():
    registry = _get_registry()
    data = await registry._read()
    return {
        "meta_mode": data.get("meta_mode", False),
        "full_info_tools": data.get("full_info_tools", []),
    }
```

- [ ] **Step 5: admin_router.py の PATCH /settings を拡張**

`src/mcp_hub/admin_router.py:89-95` を以下の形に変更:

```python
@router.patch("/settings")
async def update_settings(body: dict):
    registry = _get_registry()
    if "meta_mode" in body:
        await registry.set_meta_mode(bool(body["meta_mode"]))
    if "full_info_tools" in body:
        tools = body["full_info_tools"]
        if not isinstance(tools, list) or not all(
            isinstance(t, str) and "_" in t for t in tools
        ):
            raise HTTPException(
                status_code=422,
                detail="full_info_tools は '{server}_{tool}' 形式の文字列リストである必要があります",
            )
        await registry.set_full_info_tools(tools)
    data = await registry._read()
    return {
        "meta_mode": data.get("meta_mode", False),
        "full_info_tools": data.get("full_info_tools", []),
    }
```

`HTTPException` は admin_router.py:14 で import 済み。

- [ ] **Step 6: テストが通ることを確認**

Run: `rtk pytest tests/test_full_info_api.py -v`
Expected: PASS（6 passed）

- [ ] **Step 7: 回帰確認**

Run: `rtk pytest tests/test_admin_api.py tests/test_log_api.py -v`
Expected: PASS（既存 30 + 6 passed に影響なし）

- [ ] **Step 8: コミット**

```bash
git add src/mcp_hub/store.py src/mcp_hub/admin_router.py tests/test_full_info_api.py
git commit -m "feat(full-info): full_info_tools 設定の保存と /settings API を追加"
```

---

### Task 4: main.py 配線 + 統合テスト

**Files:**
- Modify: `src/mcp_hub/main.py`（:172 の直後、import 追加）
- Test: `tests/test_full_info.py`（追記）

**Interfaces:**
- Consumes: `FullInfoMiddleware`（Task 2）、`meta_app.index.get_schema`（既存 create_meta_app が返す MetaApp の属性）
- Produces: meta_app の middleware チェーンが `[ToolLogMiddleware, FullInfoMiddleware]` の順（ToolLog が外側）になる

- [ ] **Step 1: main.py に import を追加**

`src/mcp_hub/main.py` のモジュールレベル import に追加:

```python
from .full_info import FullInfoMiddleware
```

- [ ] **Step 2: main.py の meta 側に FullInfoMiddleware を登録**

`src/mcp_hub/main.py:172` の `meta_app.mcp.add_middleware(ToolLogMiddleware(proxy_manager))` の**直後**に追加:

```python
    # フル公開（メタOFF）ミドルウェア。ToolLog の直後に登録する（先に add した
    # 方が外側で先に実行される）。FullInfo が on_call_tool で直接転送
    # （call_next スキップ）しても ToolLog が必ず実行され、ログ・metrics が保証される。
    meta_app.mcp.add_middleware(
        FullInfoMiddleware(proxy_manager, get_schema_fn=meta_app.index.get_schema)
    )
```

- [ ] **Step 3: 統合テストを tests/test_full_info.py に追記**

`tests/test_full_info.py` の末尾に追加:

```python
class TestIntegration:
    def test_middleware_registration_order(self, tmp_path, monkeypatch):
        """meta_app の middleware チェーンが [ToolLog, FullInfo] の順であること
        （先に add した ToolLog が外側 = 先に実行される）。"""
        from fastapi.testclient import TestClient

        from mcp_hub.full_info import FullInfoMiddleware
        from mcp_hub.main import create_app
        from mcp_hub.middleware import ToolLogMiddleware
        from mcp_hub.state import app_state

        monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
        app = create_app()
        with TestClient(app) as client:
            dispatcher = app_state.mcp_dispatcher
            assert dispatcher is not None
            chain = dispatcher.meta_app.mcp.middleware
            assert isinstance(chain[0], ToolLogMiddleware)
            assert isinstance(chain[1], FullInfoMiddleware)

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
```

- [ ] **Step 4: テストが通ることを確認**

Run: `rtk pytest tests/test_full_info.py -v`
Expected: PASS（11 passed: split 3 + list 2 + call 4 + integration 2）

- [ ] **Step 5: 全回帰**

Run: `rtk pytest tests/ -q`
Expected: PASS（既存 177 + 新規 17 = 194 passed 相当）

- [ ] **Step 6: コミット**

```bash
git add src/mcp_hub/main.py tests/test_full_info.py
git commit -m "feat(full-info): meta_app に FullInfoMiddleware を登録"
```

---

### Task 5: WebUI フル公開トグル（index.html）

**Files:**
- Modify: `src/mcp_hub/static/index.html`（状態変数 :1721-1731 付近、renderServerCard の tool-item :2223-2229、loadSettings :3192-3197、toggleServerEnabled の直後）

**Interfaces:**
- Consumes: `API.getSettings()` / `API.patchSettings(body)`（既存 :1989-1995）、`metaMode` 変数（既存 :1852）、`servers` 状態（既存）、`escapeHtml` / `showToast`（既存）
- Produces: `let fullInfoTools = []` 状態、`toggleFullInfoTool(serverName, toolName, checked)` 関数。renderServerCard が meta on 時のみ「フル公開」トグルを描画

- [ ] **Step 1: 状態変数を追加**

`src/mcp_hub/static/index.html` の状態変数ブロック（:1721-1731、`let metaMode = null;` の近く）に追加:

```javascript
let fullInfoTools = [];
```

- [ ] **Step 2: renderServerCard の tool-item にフル公開トグルを追加**

`src/mcp_hub/static/index.html` の renderServerCard 内 tool-item（:2223-2229、tool-info と「実行」ボタンの tool-action を含む div）の**末尾（tool-action の直後）**に追加。既存の toggle-wrapper パターン（:2186-2195 の `toggle-switch` / `toggle-track` / `toggle-thumb` / `toggle-label` クラス）を流用する:

```html
<span class="full-info-toggle" ${metaMode === true ? '' : 'style="display:none"'}>
  <label class="toggle-switch toggle-switch-sm">
    <input type="checkbox" ${fullInfoTools.includes(tool.name) ? 'checked' : ''}
           onchange="toggleFullInfoTool('${escapeHtml(server.name)}', '${escapeHtml(tool.name)}', this.checked)">
    <span class="toggle-track"></span>
    <span class="toggle-thumb"></span>
  </label>
  <span class="toggle-label">フル公開</span>
</span>
```

- [ ] **Step 3: toggleFullInfoTool 関数を追加**

`src/mcp_hub/static/index.html` の `toggleServerEnabled` 関数の直後に追加:

```javascript
async function toggleFullInfoTool(serverName, toolName, checked) {
  const entry = toolName; // tool.name は既に "{server}_{tool}" 形式
  const next = new Set(fullInfoTools);
  if (checked) { next.add(entry); } else { next.delete(entry); }
  fullInfoTools = [...next];
  try {
    await API.patchSettings({ full_info_tools: fullInfoTools });
  } catch (e) {
    showToast('フル公開設定の更新に失敗しました', 'error');
    if (servers && servers.length) renderServers(); // 失敗時は再描画で元に戻す
  }
}
```

- [ ] **Step 4: loadSettings で fullInfoTools を読み込み、metaMode 確定後に再描画**

`src/mcp_hub/static/index.html` の `loadSettings`（:3192-3197）を以下の形に変更:

```javascript
async function loadSettings() {
  try {
    const settings = await API.getSettings();
    metaMode = settings.meta_mode;
    fullInfoTools = settings.full_info_tools || [];
    // 設定読込は loadServers と並行で走るため、後から読めた場合は
    // 再描画してフル公開トグルの表示/チェック状態を反映する
    if (servers && servers.length) renderServers();
  } catch (e) {
    console.error('設定の読み込みに失敗しました', e);
  }
}
```

（既存の loadSettings が try/catch なしで `metaMode = settings.meta_mode;` のみの形なら、try/catch を付けて上記に置き換える。既存の形を壊さないこと）

- [ ] **Step 5: JS 構文チェック**

インライン script を抽出して node で構文チェック:

```bash
node -e "const fs=require('fs');const html=fs.readFileSync('src/mcp_hub/static/index.html','utf8');const m=html.match(/<script>([\\s\\S]*?)<\\/script>/);fs.writeFileSync('/tmp/mcphub_inline.js',m[1]);" && node --check /tmp/mcphub_inline.js
```

Expected: `OK /tmp/mcphub_inline.js`（エラーなし）

- [ ] **Step 6: コミット**

```bash
git add src/mcp_hub/static/index.html
git commit -m "feat(webui): ツールカードにフル公開トグルを追加"
```

---

### Task 6: ブラウザ実機確認 + 最終回帰（orchestrator 担当）

**Files:**
- 変更なし（検証のみ）

**Interfaces:**
- Consumes: Task 1-5 の成果（コミット済みコード）

- [ ] **Step 1: 全テスト回帰**

Run: `rtk pytest tests/ -q`
Expected: PASS

- [ ] **Step 2: 検証サーバー起動**

```bash
MCP_HUB_PORT=26263 MCP_HUB_DATA_DIR=/tmp/mcphub_fullinfo_data python3 -m mcp_hub.main > /tmp/mcphub_server.log 2>&1 &
```

（`rtk python` は python を解決できないため `python3` を使う。事前に `mkdir -p /tmp/mcphub_fullinfo_data` し、hub.config.json に `{"meta_mode": true}` を書いておくと meta on で起動できる）

- [ ] **Step 3: 実機ブラウザ確認（Tailscale puppeteer、100.112.180.92:26263）**

orchestrator が puppeteer MCP で以下を確認:
- コンソールエラーなし
- サーバーカードのツール行に「フル公開」トグルが表示される（meta on 時）
- トグルをクリック → PATCH /settings が反映され、`full_info_tools` に `"{server}_{tool}"` が入る（fetch で /admin/api/settings を確認）
- 再読み込み後もトグル状態が保持される
- meta off にするとトグルが表示されない

- [ ] **Step 4: 検証サーバー停止と掃除**

```bash
kill <PID>  # nohup で起動した python3 プロセス
```

- [ ] **Step 5: コミット履歴確認**

Run: `git log --oneline -8`
Expected: Task 1-5 の feat(full-info) コミット 5 件が並んでいる

---

## Self-Review メモ（プラン作成時に実施済み）

- **仕様カバレッジ**: 仕様書の全セクション（設定 / middleware / API / main 配線 / WebUI / テスト）が Task 1-6 で網羅。ora-1 指摘 11 件は全て実装に反映（①tool.name 直接使用→Task5、②_data 同期参照→Task2、③split_qualified_name 最長一致→Task1、④登録順序→Task4、⑤Sequence[Tool]→Task2、⑥ToolResult 直接 return+例外時 content→Task2、⑦タグバイパスは仕様書に明記済み（実装変更なし）、⑧422 バリデーション→Task3、⑨SEP-986 try/except→Task2、⑩統合テスト→Task4、⑪meta on 時のみ表示→Task5）
- **プレースホルダ**: なし（全ステップに実コード）
- **型整合**: `split_qualified_name(name, connected) -> tuple[str,str]`（Task1→2）、`FullInfoMiddleware(proxy_manager, get_schema_fn)`（Task2→4）、`set_full_info_tools(list[str])`（Task3→4,5）、`fullInfoTools` / `toggleFullInfoTool`（Task5 内一貫）
