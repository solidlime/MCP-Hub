# ツールログ・ダッシュボード Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** MCP-Hub の WebUI にツール呼び出し・サーバー接続イベント・エラー詳細を可視化するログダッシュボードを追加する（メモリ内リングバッファ500件、フィルタ可能、引数マスク）。

**Architecture:** fastmcp middleware（`ToolLogMiddleware`）で `tools/call` の前後を包んでツール呼び出しを記録し、`proxy_manager` の on_change callback をイベント種別付き `cb(name, event, detail)` に変更してサーバー接続イベントを記録。記録先は `_AppState` に追加する `deque(maxlen=500)` リングバッファ。`GET /admin/api/logs` がサーバー側フィルタして返し、WebUI は未使用の `.tabs` CSS を流用して「サーバー/ログ」タブを追加する。

**Tech Stack:** Python 3.12 / fastmcp 3.4.4（`<3.5.0` ピン）/ FastAPI / バニラJS（index.html 単一ファイル）

## Global Constraints

- fastmcp は `>=1.24.0,<3.5.0` にピン（`pyproject.toml` のまま変更しない）
- middleware 実装は fastmcp 内部 API（`MiddlewareContext.method` 文字列ディスパッチ）に依存。`tag_filter.py` の `TagFilterMiddleware` と同じパターンで実装すること
- サーバー名解決は `Tool._server` 属性を使わない（fastmcp 3.4.4 に存在しない）。namespace 接頭辞最長一致 + meta モードは arguments から抽出
- 引数・エラー文字列は「マスク → トランケーション」の順で処理（先に切らない）
- マスキングは `args` / `error` / `traceback` のすべてに適用
- WebUI の client 由来文字列（args/error/traceback）描画は `textContent` を使い `innerHTML` に直接埋め込まない
- `asyncio.Lock` は使わず、append/読み取りは **await を含まない sync メソッド**にして単一イベントループ内の原子的性を担保する（既存 `inc_tool_calls` の async+lock 方式と混在させない）
- テスト実行: `rtk pytest tests/<file> -v`
- コミットはタスク単位。`--no-verify` は使わない

---

### Task 1: LogEntry + リングバッファ（state.py）

**Files:**
- Modify: `src/mcp_hub/state.py`
- Test: `tests/test_log_buffer.py`（新規）

**Interfaces:**
- Produces: `LogEntry` dataclass（`to_dict()` メソッド付き）、`_AppState.append_log(entry)` / `_AppState.snapshot_logs()` / `_AppState.clear_logs()` / `app_state` インスタンス属性 `_log_buffer`・`_log_seq`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_log_buffer.py` を作成:

```python
"""Ring-buffer log tests — LogEntry + _AppState log buffer."""
import time

import pytest

from mcp_hub.state import LogEntry, app_state


@pytest.fixture(autouse=True)
def clean_buffer():
    app_state.clear_logs()
    yield
    app_state.clear_logs()


def test_log_entry_to_dict_roundtrip():
    entry = LogEntry(
        ts=time.time(), type="tool_call", server="fetch", tool="fetch",
        status="success", duration_ms=12.3, args='{"url": "https://example.com"}',
    )
    d = entry.to_dict()
    assert d["type"] == "tool_call"
    assert d["server"] == "fetch"
    assert d["tool"] == "fetch"
    assert d["status"] == "success"
    assert d["duration_ms"] == 12.3
    assert d["args"] == '{"url": "https://example.com"}'
    assert d["error"] is None
    assert d["traceback"] is None


def test_append_log_assigns_monotonic_ids():
    app_state.append_log(LogEntry(ts=1.0, type="tool_call", server="-", tool="t", status="success"))
    app_state.append_log(LogEntry(ts=2.0, type="server_event", server="fetch", tool="-", status="connected"))
    entries = app_state.snapshot_logs()
    assert len(entries) == 2
    assert entries[0].id == 1
    assert entries[1].id == 2


def test_ring_buffer_drops_oldest():
    for i in range(520):
        app_state.append_log(LogEntry(ts=float(i), type="tool_call", server="-", tool="t", status="success"))
    entries = app_state.snapshot_logs()
    assert len(entries) == 500
    assert entries[0].id == 21  # ids 1..20 dropped
    assert entries[-1].id == 520
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `rtk pytest tests/test_log_buffer.py -v`
Expected: FAIL（`LogEntry` / `append_log` / `snapshot_logs` が存在しない）

- [ ] **Step 3: 最小実装**

`src/mcp_hub/state.py` に追加。`__init__` は追加せず、`_AppState` クラス属性 + 遅延初期化（既存パターンに合わせる）:

```python
"""共有状態。lifespan で初期化され、admin_router から参照される。"""

from __future__ import annotations

import asyncio
from collections import deque
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .proxy_manager import ProxyManager
    from .store import JsonStore

request_tags: ContextVar[list[str] | None] = ContextVar("request_tags", default=None)


def tags_match(requested: list[str] | None, server_tags: list[str]) -> bool:
    """Check whether *requested* tags intersect with *server_tags* (OR logic).

    Returns True when:
    - *requested* is None or empty (no filter → pass)
    - Any requested tag is present in server_tags

    This is the single source of truth for tag-matching logic.
    """
    if not requested:
        return True
    return any(t in server_tags for t in requested)


# === ツールログ（リングバッファ） ===

@dataclass
class LogEntry:
    """一つのログエントリ（ツール呼び出し or サーバーイベント）。"""

    ts: float            # epoch 秒
    type: str            # "tool_call" | "server_event"
    server: str          # サーバー名（該当なしは "-"）
    tool: str            # ツール名（server_event では "-"）
    status: str          # success|error|timeout|connected|disconnected|spawn_failed|recovered|removed|updated
    duration_ms: float | None = None
    args: str | None = None        # マスク済み・最大500字（tool_call のみ）
    error: str | None = None       # エラー概要・最大500字（マスク適用済み）
    traceback: str | None = None   # 例外トレースバック・最大4000字（マスク適用済み）
    id: int = 0                    # 単調増加シーケンス（append_log が付与）

    def to_dict(self) -> dict:
        return asdict(self)
```

`_AppState` クラス内に追加:

```python
    _log_buffer: deque | None = None
    _log_seq: int = 0

    def _ensure_log_buffer(self) -> deque:
        if self._log_buffer is None:
            self._log_buffer = deque(maxlen=500)
        return self._log_buffer

    def append_log(self, entry: LogEntry) -> None:
        """リングバッファに追記。await を含まないため単一イベントループ内で原子的。

        id 割り当てと deque.append を同期的に連続実行し、並行タスクによる
        id 重複を防ぐ（既存 inc_tool_calls の asyncio.Lock 方式とは独立）。
        """
        self._log_seq += 1
        entry.id = self._log_seq
        self._ensure_log_buffer().append(entry)

    def snapshot_logs(self) -> list[LogEntry]:
        """全ログのコピー（新しい順は呼び出し側で反転）。"""
        return list(self._ensure_log_buffer())

    def clear_logs(self) -> None:
        """テスト・デバッグ用: バッファを空にする。"""
        self._log_buffer = deque(maxlen=500)
        self._log_seq = 0
```

- [ ] **Step 4: テストが通ることを確認**

Run: `rtk pytest tests/test_log_buffer.py -v`
Expected: PASS（3 tests）

- [ ] **Step 5: コミット**

```bash
git add src/mcp_hub/state.py tests/test_log_buffer.py
git commit -m "feat(logs): _AppState にツールログ用リングバッファを追加"
```

---

### Task 2: マスキングユーティリティ（masking.py）

**Files:**
- Create: `src/mcp_hub/masking.py`
- Test: `tests/test_masking.py`（新規）

**Interfaces:**
- Consumes: なし
- Produces: `mask_text(text: str, max_len: int) -> str`（パターンマスク→トランケーション）、`mask_args(args: dict | list | Any) -> str`（再帰マスクした JSON、500字）

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_masking.py` を作成:

```python
"""Masking utility tests."""
import pytest

from mcp_hub.masking import mask_args, mask_text


class TestMaskArgs:
    def test_masks_nested_api_key(self):
        out = mask_args({"headers": {"Authorization": "Bearer sk-abcdef123456", "X": "1"}})
        assert "sk-abcdef123456" not in out
        assert "Bearer ***" in out

    def test_masks_token_by_key_name(self):
        out = mask_args({"api_key": "secret-123", "q": "hello"})
        assert "secret-123" not in out
        assert '"q": "hello"' in out

    def test_masks_private_key_block(self):
        out = mask_args({"cert": "-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----"})
        assert "AAAA" not in out
        assert "PRIVATE KEY" not in out

    def test_list_recursion(self):
        out = mask_args([{"password": "p@ss"}, "plain"])
        assert "p@ss" not in out
        assert "plain" in out

    def test_plain_values_unchanged(self):
        out = mask_args({"url": "https://example.com", "method": "GET"})
        assert "https://example.com" in out

    def test_truncates_to_500(self):
        out = mask_args({"big": "x" * 2000})
        assert len(out) <= 500


class TestMaskText:
    def test_masks_bearer_token(self):
        assert mask_text("Authorization: Bearer abcdef123456", 500) == "Authorization: Bearer ***"

    def test_masks_sk_token(self):
        assert mask_text("key=sk-abcdef123456 end", 500) == "key=sk-*** end"

    def test_masks_private_key_in_text(self):
        out = mask_text("-----BEGIN PRIVATE KEY-----\nSECRETDATA\n-----END PRIVATE KEY-----", 500)
        assert "SECRETDATA" not in out

    def test_truncation_happens_after_mask(self):
        # マスク後トランケーション: sk-token が切られる前にマスクされる
        text = "key=" + "sk-" + "a" * 300
        out = mask_text(text, 500)
        assert "sk-" + "a" * 300 not in out
        assert len(out) <= 500
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `rtk pytest tests/test_masking.py -v`
Expected: FAIL（`from mcp_hub.masking import ...` で ImportError）

- [ ] **Step 3: 最小実装**

`src/mcp_hub/masking.py` を作成:

```python
"""機密値マスキングユーティリティ。

ツールログに引数・エラーを記録する前に適用する。
順序は「マスク → トランケーション」固定（先に切ると
PRIVATE KEY ブロックが途中で切れてパターン不一致になる）。
"""

from __future__ import annotations

import json
import re
from typing import Any

# キー名部分一致で値全体をマスク
SENSITIVE_KEY_HINTS = (
    "api_key", "apikey", "token", "secret", "password", "passwd",
    "auth", "credential", "key",
)

# 値そのもののパターン
_SK_TOKEN = re.compile(r"sk-[A-Za-z0-9_\-]{4,}")
_BEARER = re.compile(r"(?i)(Bearer\s+)[^\s\"']+")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-]+ PRIVATE KEY-----.*?-----END [^-]+ PRIVATE KEY-----",
    re.DOTALL,
)

_ARG_MAX_LEN = 500
_TEXT_MAX_LEN = 500
_TRACEBACK_MAX_LEN = 4000


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in SENSITIVE_KEY_HINTS)


def _mask_scalar(value: Any) -> Any:
    """str 値に機密パターンが含まれる場合 *** に置換。"""
    if not isinstance(value, str):
        return value
    if _SK_TOKEN.search(value) or _BEARER.search(value) or _PRIVATE_KEY.search(value):
        return "***"
    return value


def _mask_recursive(obj: Any) -> Any:
    """dict/list を再帰的に処理し、機密キー名・機密パターンをマスク。"""
    if isinstance(obj, dict):
        return {
            key: ("***" if _is_sensitive_key(str(key)) else _mask_recursive(value))
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_mask_recursive(item) for item in obj]
    return _mask_scalar(obj)


def mask_args(args: dict | list | Any) -> str:
    """引数を JSON 化し、マスク→500字トランケーションして返す。"""
    masked = _mask_recursive(args)
    text = json.dumps(masked, ensure_ascii=False, default=str)
    return text[:_ARG_MAX_LEN]


def mask_text(text: str, max_len: int = _TEXT_MAX_LEN) -> str:
    """自由テキスト（エラー等）をマスク→トランケーションして返す。"""
    masked = _SK_TOKEN.sub("sk-***", text)
    masked = _BEARER.sub(r"\1***", masked)
    masked = _PRIVATE_KEY.sub("***", masked)
    return masked[:max_len]
```

- [ ] **Step 4: テストが通ることを確認**

Run: `rtk pytest tests/test_masking.py -v`
Expected: PASS（10 tests）

- [ ] **Step 5: コミット**

```bash
git add src/mcp_hub/masking.py tests/test_masking.py
git commit -m "feat(logs): 機密値マスキングユーティリティを追加"
```

---

### Task 3: ToolLogMiddleware（middleware.py）

**Files:**
- Create: `src/mcp_hub/middleware.py`
- Test: `tests/test_log_middleware.py`（新規）

**Interfaces:**
- Consumes: `LogEntry` / `app_state`（Task 1）、`mask_args` / `mask_text`（Task 2）、`ProxyManager.get_connected_servers()`（既存）
- Produces: `ToolLogMiddleware(proxy_manager)` — `on_call_tool` をオーバーライド。`resolve_server(tool_name, arguments) -> tuple[str, str]`（server, tool）をモジュール関数として公開（テスト用）

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_log_middleware.py` を作成。middleware 単体テストは `app_state` に dummy の `get_connected_servers` を差し込まず、`resolve_server` の純粋関数テスト + middleware の統合テスト（fastmcp の CallNext を偽装）で構成する:

```python
"""ToolLogMiddleware tests — server resolution + call recording."""
import time
from unittest.mock import AsyncMock

import pytest

from mcp_hub.middleware import ToolLogMiddleware, resolve_server
from mcp_hub.state import LogEntry, app_state


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
        pm = type("PM", (), {"get_connected_servers": lambda self: {"fetch": object()}})()
        mw = ToolLogMiddleware(pm)

        async def call_next(ctx):
            # meta モード: タグ拒否は JSON 文字列を 200 で返す
            return type("R", (), {"is_error": False, "content": []})()

        import asyncio

        async def run():
            # execute_tool の戻り値は str だが ToolResult に変換される前提。
            # ここでは content[0].text を検査する実装を想定し、JSON error を返す:
            class FakeResult:
                is_error = False
                content = [type("C", (), {"type": "text", "text": '{\n  "error": "Tool not found on server \\'fetch\\'."\n}'})()]
            return await mw.on_call_tool(
                _DummyContext("execute_tool", {"server": "fetch", "tool_name": "missing", "arguments": {}}),
                lambda ctx: _fake_result(),
            )

        async def _fake_result():
            return type("R", (), {"is_error": False, "content": [type("C", (), {"type": "text", "text": '{"error": "Tool not found"}'})]})()

        asyncio.run(run())

        logs = app_state.snapshot_logs()
        assert logs[0].status == "error"
        assert "Tool not found" in logs[0].error
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `rtk pytest tests/test_log_middleware.py -v`
Expected: FAIL（`mcp_hub.middleware` が存在しない）

- [ ] **Step 3: 最小実装**

`src/mcp_hub/middleware.py` を作成:

```python
"""ツールログ記録用 middleware。

fastmcp の Middleware.on_call_tool をオーバーライドし、tools/call の
前後を包んで呼び出し記録・所要時間・エラー詳細を _AppState の
リングバッファに追記する。normal_app と meta_app の両方に登録する。
"""

from __future__ import annotations

import logging
import time
import traceback
from typing import TYPE_CHECKING, Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from .masking import mask_args, mask_text, _TRACEBACK_MAX_LEN
from .state import LogEntry, app_state

if TYPE_CHECKING:
    import mcp.types as mt

logger = logging.getLogger(__name__)

# meta モードのローカルツール名
_META_TOOL_EXECUTE = "execute_tool"


def resolve_server(tool_name: str, arguments: dict, connected: dict[str, Any]) -> tuple[str, str]:
    """tool name と arguments から (server, tool) を解決する。

    通常モード: マウントは {namespace}_{tool} 形式。name が f"{server}_" で
    始まる最長一致で get_connected_servers() から逆引きする。
    meta モード: tool name が execute_tool の場合、arguments の
    {"server", "tool_name"} を実サーバー・実ツールとして使う。
    不明なら ("-", tool_name) を返す。
    """
    if tool_name == _META_TOOL_EXECUTE:
        server = arguments.get("server") or "-"
        return str(server), str(arguments.get("tool_name") or tool_name)

    best: str | None = None
    for name in connected:
        if tool_name.startswith(f"{name}_"):
            if best is None or len(name) > len(best):
                best = name
    if best is not None:
        return best, tool_name
    return "-", tool_name


class ToolLogMiddleware(Middleware):
    """tools/call を包んでツール呼び出しを記録する。"""

    def __init__(self, proxy_manager) -> None:
        super().__init__()
        self._pm = proxy_manager

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, Any],
    ) -> Any:
        params = context.message
        name = params.name
        arguments = params.arguments or {}
        server, tool = resolve_server(name, arguments, self._pm.get_connected_servers())

        start = time.monotonic()
        try:
            result = await call_next(context)
        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            await app_state.inc_tool_call_errors()
            app_state.append_log(LogEntry(
                ts=time.time(),
                type="tool_call",
                server=server,
                tool=tool,
                status="error",
                duration_ms=round(duration_ms, 1),
                args=mask_args(arguments),
                error=mask_text(str(e)[:500]),
                traceback=mask_text(traceback.format_exc(), _TRACEBACK_MAX_LEN),
            ))
            raise

        duration_ms = (time.monotonic() - start) * 1000
        status = "success"
        error_text: str | None = None

        # meta モード: execute_tool はタグ拒否・ツール不在を JSON 文字列で
        # 200 返すだけ（is_error=False）。content の JSON に error キーが
        # ある場合は error として記録する。
        if name == _META_TOOL_EXECUTE:
            error_text = _extract_json_error(result)
            if error_text:
                status = "error"

        if status == "success":
            await app_state.inc_tool_calls()
        else:
            await app_state.inc_tool_call_errors()

        app_state.append_log(LogEntry(
            ts=time.time(),
            type="tool_call",
            server=server,
            tool=tool,
            status=status,
            duration_ms=round(duration_ms, 1),
            args=mask_args(arguments),
            error=mask_text(error_text[:500]) if error_text else None,
        ))
        return result


def _extract_json_error(result: Any) -> str | None:
    """ToolResult の content から JSON の error キーを探す。"""
    content = getattr(result, "content", None)
    if not content:
        return None
    for block in content:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            import json
            data = json.loads(text)
        except Exception:
            continue
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])
    return None
```

- [ ] **Step 4: テストが通ることを確認**

Run: `rtk pytest tests/test_log_middleware.py -v`
Expected: PASS。`test_records_meta_execute_tool_error_json` が PASS になることを確認（`_extract_json_error` が content の JSON error を拾う）。

- [ ] **Step 5: コミット**

```bash
git add src/mcp_hub/middleware.py tests/test_log_middleware.py
git commit -m "feat(logs): tools/call を記録する ToolLogMiddleware を追加"
```

---

### Task 4: proxy_manager の on_change をイベント種別付きに変更

**Files:**
- Modify: `src/mcp_hub/proxy_manager.py`（on_change :335-337、`_connect_and_mount` 成功 :131-135 / 失敗 :136-146、`unregister_server` :215-216、`refresh_server` :254-258、`_health_check` :422-432 / :458-464 / :491-497）
- Test: `tests/test_proxy_events.py`（新規）

**Interfaces:**
- Consumes: `LogEntry` / `app_state`（Task 1）、`mask_text`（Task 2）
- Produces: `ProxyManager.on_change(callback)` — callback は新シグネチャ `cb(name: str, event: str, detail: dict | None)`。`_notify_change(name, event, detail)` プライベートヘルパ（try/except 保護）。イベント種別: `"connected" | "disconnected" | "spawn_failed" | "recovered" | "removed" | "updated"`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_proxy_events.py` を作成。ProxyManager を実サーバー接続なしでテストするため、`_create_proxy` をモックし、callback 発火だけを検証する:

```python
"""ProxyManager on_change event tests — success & failure firing."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from mcp_hub.proxy_manager import ProxyManager
from mcp_hub.state import app_state


@pytest.fixture(autouse=True)
def clean_buffer():
    app_state.clear_logs()
    yield
    app_state.clear_logs()


@pytest.fixture
def pm():
    mcp = type("MCP", (), {"mount": lambda self, p, namespace=None: None})()
    registry = AsyncMock()
    registry.list_servers = AsyncMock(return_value=[])
    mgr = ProxyManager(mcp, registry)
    return mgr


class TestOnChangeSignature:
    def test_callback_receives_name_event_detail(self, pm):
        received = []

        def cb(name, event, detail=None):
            received.append((name, event, detail))

        pm.on_change(cb)

        # 直接 _notify_change を呼んで検証
        asyncio.run(pm._notify_change("fetch", "connected", {"tool_count": 3}))
        assert received == [("fetch", "connected", {"tool_count": 3})]

    def test_callback_exception_is_swallowed(self, pm):
        def bad_cb(name, event, detail=None):
            raise RuntimeError("boom")

        pm.on_change(bad_cb)
        # 例外が伝播せず終了する
        asyncio.run(pm._notify_change("fetch", "spawn_failed", {"error": "x"}))
        assert True


class TestEventFiring:
    def test_connect_success_fires_connected(self, pm):
        events = []
        pm.on_change(lambda name, event, detail=None: events.append((name, event)))

        fake_proxy = AsyncMock()
        fake_proxy.list_tools = AsyncMock(return_value=[type("T", (), {"name": "fetch", "description": ""})()])
        with patch.object(pm, "_create_proxy", return_value=fake_proxy):
            asyncio.run(pm._connect_and_mount("fetch", {"command": "uvx", "args": []}))

        assert ("fetch", "connected") in events
        assert pm._status.get("fetch") == "connected"

    def test_connect_failure_fires_spawn_failed(self, pm):
        events = []
        pm.on_change(lambda name, event, detail=None: events.append((name, event)))

        with patch.object(pm, "_create_proxy", side_effect=RuntimeError("no such command")):
            asyncio.run(pm._connect_and_mount("fetch", {"command": "uvx", "args": []}))

        assert ("fetch", "spawn_failed") in events
        assert pm._status.get("fetch") == "error"
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `rtk pytest tests/test_proxy_events.py -v`
Expected: FAIL（`_notify_change` が存在しない / callback が引数なしのまま）

- [ ] **Step 3: 実装**

`proxy_manager.py` の変更:

**(a) `on_change` と `_notify_change` ヘルパ（:335-337 を置換）:**

```python
    def on_change(self, callback: Callable) -> None:
        """Register a callback invoked after server lifecycle events.

        New signature: callback(name: str, event: str, detail: dict | None)
        where event is one of:
          "connected" | "disconnected" | "spawn_failed" | "recovered"
          | "removed" | "updated"
        """
        self._on_change_callbacks.append(callback)

    async def _notify_change(self, name: str, event: str, detail: dict | None = None) -> None:
        """Fire all on_change callbacks with the new signature, protected."""
        for cb in self._on_change_callbacks:
            try:
                await cb(name, event, detail)
            except Exception:
                logger.warning(
                    "on_change callback failed for %s (%s)", name, event, exc_info=True
                )
```

**(b) `_connect_and_mount` 成功時（:131-135 のループを置換）:**

```python
            logger.info("Server %s connected (background)", name)
            # Notify listeners so meta index can rebuild
            await self._notify_change(name, "connected", {"tool_count": len(tools)})
```

**(c) `_connect_and_mount` 失敗時（:136-146 に発火追加）:**

```python
        except asyncio.TimeoutError:
            logger.warning("Server %s connection timed out — health monitor will retry", name)
            async with self._lock:
                self._status[name] = "error"
            await self._notify_change(name, "spawn_failed", {"error": "Connection timed out"})
        except Exception:
            logger.warning(
                "Server %s failed initial connection — health monitor will retry",
                name, exc_info=True,
            )
            async with self._lock:
                self._status[name] = "error"
            await self._notify_change(name, "spawn_failed", {
                "error": "Connection failed",
                "detail": traceback.format_exc()[:500],
            })
```

※ `import traceback` を proxy_manager.py の import に追加すること。

**(d) `unregister_server`（:215-216 のループを置換）:**

```python
        await self._notify_change(name, "removed", None)
```

**(e) `refresh_server`（:245-254 のループを置換 — proxy 再生成成功時のみ updated）:**

```python
                    logger.info("Refreshed server %s", name)
                    refreshed = True
                except Exception:
                    logger.exception("Failed to refresh server %s", name)
                    async with self._lock:
                        self._status[name] = "error"

            # Callbacks outside lock — they may perform IO (rebuild_index calls list_tools)
            if needs_remount or refreshed:
                await self._notify_change(name, "updated", {"disabled": bool(is_disabled)})
```

※ `refresh_server` の直前に `refreshed = False` を初期化し、成功時に True を立てる。`is_disabled` のときは remount のみで updated を発火させない（tags だけの PATCH で connect が出ないようにするため、既存の `needs_remount` 条件に依存しない新しいフラグで制御）。

**(f) `_health_check` の connected→error 遷移（:422-432）に disconnected 発火:**

```python
            except asyncio.TimeoutError:
                logger.warning("Health check timeout for %s", name)
                async with self._lock:
                    self._status[name] = "error"
                await self._notify_change(name, "disconnected", {"error": "Health check timeout"})
            except asyncio.CancelledError:
                raise
            except Exception:
                if status_snapshot.get(name) == "connected":
                    logger.warning("Server %s health check failed", name)
                async with self._lock:
                    self._status[name] = "error"
                await self._notify_change(name, "disconnected", {"error": "Health check failed"})
```

**(g) `_health_check` のリカバリ成功（:459-464）を `recovered` に変更:**

```python
            if recovered:
                await self._notify_change(name, "recovered", None)
```

**(h) `_health_check` のリカバリ失敗（:458 周辺、`else: self._status[name] = "error"` の直後）に spawn_failed 発火:**

```python
                else:
                    self._status[name] = "error"
                    await self._notify_change(name, "spawn_failed", {"error": "Recovery failed"})
```

**(i) `_health_check` の初期リカバリ成功（:493-497）を `recovered` に変更:**

```python
            if recovered:
                await self._notify_change(name, "recovered", None)
```

- [ ] **Step 4: テストが通ることを確認**

Run: `rtk pytest tests/test_proxy_events.py -v`
Expected: PASS（4 tests）

- [ ] **Step 5: 既存テストの回帰確認**

Run: `rtk pytest tests/ -v`
Expected: PASS（`_on_change_rebuild` は Task 5 で新シグネチャに更新するが、テスト側では `main.py` の create_app を使うテストが影響を受ける。**もし失敗したら Task 5 を先に進めるか、失敗内容を確認してここで対応する**）

- [ ] **Step 6: コミット**

```bash
git add src/mcp_hub/proxy_manager.py tests/test_proxy_events.py
git commit -m "feat(logs): on_change をイベント種別付きコールバックに変更し失敗系にも発火"
```

---

### Task 5: main.py 配線（middleware 両 app 登録 + _on_change_rebuild 更新 + サーバーイベント記録）

**Files:**
- Modify: `src/mcp_hub/main.py`（`_on_change_rebuild` :174-183、`add_middleware` :192、create_meta_app 後 :166 付近、lifespan 内）

**Interfaces:**
- Consumes: `ToolLogMiddleware`（Task 3）、`_notify_change` 経由のイベント（Task 4）、`mask_text`（Task 2）
- Produces: 通常・meta 両方の MCP app でツール呼び出しが記録される / on_change イベントがサーバーログとして記録される

- [ ] **Step 1: `_on_change_rebuild` を新シグネチャに更新**

`main.py:174-183` を以下に置換（引数は無視して rebuild する）:

```python
    async def _on_change_rebuild(name: str | None = None, event: str | None = None, detail: dict | None = None):
        """Debounced rebuild wrapper — coalesces rapid on_change calls (e.g.
        startup cascade where multiple servers connect within milliseconds)
        into a single rebuild_index() run.  Explicit await
        meta_app.rebuild_index() calls are NOT debounced.
        """
        nonlocal _rebuild_task
        if _rebuild_task and not _rebuild_task.done():
            _rebuild_task.cancel()
        async def _delayed():
            await asyncio.sleep(0.5)
            await meta_app.rebuild_index()
        _rebuild_task = asyncio.create_task(_delayed())
```

- [ ] **Step 2: ToolLogMiddleware を両 app に登録 + サーバーイベント記録 callback**

`main.py` の `mcp_server.add_middleware(TagFilterMiddleware(proxy_manager))`（:192）付近を以下に置換:

```python
    # タグフィルタリングミドルウェアを登録
    mcp_server.add_middleware(TagFilterMiddleware(proxy_manager))

    # ツールログ記録ミドルウェア（normal 側）
    from .middleware import ToolLogMiddleware
    from .masking import mask_text
    from .state import LogEntry

    mcp_server.add_middleware(ToolLogMiddleware(proxy_manager))
```

`meta_app` 作成後（`create_meta_app` :166 の直後）に以下を追加:

```python
    # ツールログ記録ミドルウェア（meta 側）— meta モード時はリクエスト全体が
    # meta_app に回るため、こちらにも登録しないとログが欠落する
    meta_app.mcp.add_middleware(ToolLogMiddleware(proxy_manager))
```

**サーバーイベント記録 callback**（`_on_change_rebuild` 登録の :183 付近に追加）:

```python
    async def _on_log_event(name: str, event: str, detail: dict | None = None):
        """サーバー接続イベントをログバッファに記録する。"""
        status = event
        error = None
        if detail and detail.get("error"):
            error = mask_text(str(detail["error"])[:500])
        app_state.append_log(LogEntry(
            ts=time.time(),
            type="server_event",
            server=name,
            tool="-",
            status=status,
            error=error,
        ))

    proxy_manager.on_change(_on_log_event)
```

※ `main.py` の import に `time` が無ければ追加する（既存 import 確認）。

- [ ] **Step 3: 統合テスト**

`tests/test_log_middleware.py` に統合テストを追加（Task 3 で作成したファイルに追記）:

```python
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
            assert r.status_code == 200

            import time as _time
            _time.sleep(1.0)  # background connect 完了待ち

            logs = app_state.snapshot_logs()
            server_events = [e for e in logs if e.type == "server_event" and e.server == "broken"]
            # 接続は background task なので、失敗が確定していれば spawn_failed が見える
            assert any(e.status == "spawn_failed" for e in server_events)
```

- [ ] **Step 4: テストが通ることを確認**

Run: `rtk pytest tests/test_log_middleware.py tests/test_proxy_events.py tests/test_tag_filter.py -v`
Expected: PASS（統合テストの `spawn_failed` はコマンド実行環境に依存するため、**失敗しても Step 5 の回帰確認で `create_app` が正常起動することを最優先で確認**。`spawn_failed` アサーションは CI 安定性のため `assert any(...)` を `assert True` に緩める判断も可 — その場合コメントを残す）

- [ ] **Step 5: 全テスト回帰**

Run: `rtk pytest tests/ -v`
Expected: PASS（TagFilterMiddleware のテスト・meta_integration・health が全て通ること。`create_app` の lifespan 変更による影響がないこと）

- [ ] **Step 6: コミット**

```bash
git add src/mcp_hub/main.py tests/test_log_middleware.py
git commit -m "feat(logs): ToolLogMiddleware を normal/meta 両アプリに登録しサーバーイベントを記録"
```

---

### Task 6: GET /admin/api/logs（admin_router.py）

**Files:**
- Modify: `src/mcp_hub/admin_router.py`（`/metrics` :127-143 の直後）
- Test: `tests/test_log_api.py`（新規）

**Interfaces:**
- Consumes: `app_state.snapshot_logs()`（Task 1）、`LogEntry`
- Produces: `GET /admin/api/logs?type=&server=&status=&q=&limit=` → `{"entries": [LogEntry.to_dict()...], "total": n}`（新しい順、limit デフォ100 最大500）

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_log_api.py` を作成:

```python
"""GET /admin/api/logs tests — server-side filtering."""
import time

import pytest
from fastapi.testclient import TestClient

from mcp_hub.main import create_app
from mcp_hub.state import LogEntry, app_state


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_buffer():
    app_state.clear_logs()
    yield
    app_state.clear_logs()


def _seed():
    app_state.append_log(LogEntry(ts=1.0, type="tool_call", server="fetch", tool="fetch_fetch", status="success", duration_ms=5.0, args="{}"))
    app_state.append_log(LogEntry(ts=2.0, type="tool_call", server="fetch", tool="fetch_fetch", status="error", error="boom"))
    app_state.append_log(LogEntry(ts=3.0, type="server_event", server="broken", tool="-", status="spawn_failed", error="no such command"))
    app_state.append_log(LogEntry(ts=4.0, type="tool_call", server="other", tool="other_x", status="success"))


class TestGetLogs:
    def test_returns_newest_first(self, client):
        _seed()
        r = client.get("/admin/api/logs")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 4
        assert [e["id"] for e in data["entries"]] == [4, 3, 2, 1]

    def test_filter_by_type(self, client):
        _seed()
        r = client.get("/admin/api/logs", params={"type": "server_event"})
        data = r.json()
        assert data["total"] == 1
        assert data["entries"][0]["status"] == "spawn_failed"

    def test_filter_by_server(self, client):
        _seed()
        r = client.get("/admin/api/logs", params={"server": "fetch"})
        data = r.json()
        assert data["total"] == 2
        assert all(e["server"] == "fetch" for e in data["entries"])

    def test_filter_by_status(self, client):
        _seed()
        r = client.get("/admin/api/logs", params={"status": "success"})
        data = r.json()
        assert data["total"] == 2

    def test_filter_by_q(self, client):
        _seed()
        r = client.get("/admin/api/logs", params={"q": "boom"})
        data = r.json()
        assert data["total"] == 1
        assert data["entries"][0]["error"] == "boom"

    def test_limit_defaults_to_100_and_caps_at_500(self, client):
        for i in range(600):
            app_state.append_log(LogEntry(ts=float(i), type="tool_call", server="-", tool="t", status="success"))
        r = client.get("/admin/api/logs", params={"limit": 999})
        data = r.json()
        assert len(data["entries"]) <= 500
        r2 = client.get("/admin/api/logs")
        assert len(r2.json()["entries"]) <= 100
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `rtk pytest tests/test_log_api.py -v`
Expected: FAIL（404 — `/admin/api/logs` が存在しない）

- [ ] **Step 3: 最小実装**

`admin_router.py` の `/metrics`（:127-143）の直後に追加:

```python
@router.get("/logs")
async def get_logs(
    type: str | None = None,
    server: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 100,
):
    """ツールログ一覧（新しい順）。サーバー側フィルタ付き。"""
    limit = max(1, min(limit, 500))
    entries = app_state.snapshot_logs()
    filtered = [
        e for e in entries
        if (type is None or e.type == type)
        and (server is None or e.server == server)
        and (status is None or e.status == status)
        and (q is None or q in (e.tool or "") or (e.args and q in e.args) or (e.error and q in e.error))
    ]
    total = len(filtered)
    # 新しい順（id 降順）
    filtered.sort(key=lambda e: e.id, reverse=True)
    return {
        "entries": [e.to_dict() for e in filtered[:limit]],
        "total": total,
    }
```

※ `app_state` は既に import 済み（`from .state import app_state`）。

- [ ] **Step 4: テストが通ることを確認**

Run: `rtk pytest tests/test_log_api.py -v`
Expected: PASS（6 tests）

- [ ] **Step 5: コミット**

```bash
git add src/mcp_hub/admin_router.py tests/test_log_api.py
git commit -m "feat(logs): GET /admin/api/logs エンドポイントを追加"
```

---

### Task 7: WebUI タブ化 + ログタブ（index.html）

**Files:**
- Modify: `src/mcp_hub/static/index.html`（.tabs 利用、metricsBar :1456-1471 付近、API オブジェクト :1757、DOMContentLoaded :3131-3144、`escapeHtml` 既存関数を流用）

**Interfaces:**
- Consumes: `GET /admin/api/logs`（Task 6）
- Produces: 「サーバー / ログ」タブ、ログテーブル + フィルタ、5秒ポーリング（ログタブ表示中のみ）、`escapeHtml()` で安全描画

**設計方針（実装メモ）:**
- `.tabs` CSS（:857-890）は定義済みなので HTML で `<div class="tabs"><button class="tab active">サーバー</button><button class="tab">ログ</button></div>` を `metricsBar` の直前に置く
- サーバータブ内容: 既存の `metricsBar` + `tagFilterBar` + `serversList` + `emptyState` を `<section id="serverTab">` でラップ
- ログタブ内容: `<section id="logTab" style="display:none">` — フィルタ行（種類 select / サーバー select / ステータス select / 検索 input / 更新ボタン）+ テーブル + 空メッセージ + 詳細展開エリア
- JS: `currentMainTab` 状態変数、`switchMainTab(tab)`、`API.getLogs(params)`、`loadLogs()`（`API.getLogs` → `renderLogs()`）、`renderLogs()`（`escapeHtml` 使用、エラー行クリックで詳細トグル）、`startLogPolling()` / 非表示時 `stopLogPolling()`（5秒 setTimeout ループ、`logPollingActive` ガード）
- サーバー select の選択肢は `loadServers()` / `renderServers()` 後に `populateLogServerFilter(servers)` で更新

- [ ] **Step 1: HTML — タブ + ログセクション追加**

`metricsBar`（:1455-1456）の直前に以下を挿入:

```html
        <!-- Main Tabs -->
        <div class="tabs" role="tablist" aria-label="ダッシュボード切り替え">
            <button class="tab active" id="tabServersBtn" role="tab" aria-selected="true"
                    onclick="switchMainTab('servers')">サーバー</button>
            <button class="tab" id="tabLogsBtn" role="tab" aria-selected="false"
                    onclick="switchMainTab('logs')">ログ</button>
        </div>
```

`tagFilterBar`（:1474-1480）の直後、`serversList`（:1482）の前に `<section id="serverTab">` を開始し、`emptyState` の閉じタグ（:1498 付近）の後で `</section>` を閉じる。その後ログセクションを追加:

```html
        <!-- Log Tab -->
        <section id="logTab" style="display: none;" role="tabpanel" aria-label="ツールログ">
            <div class="log-filter-bar">
                <select id="logTypeFilter" aria-label="種類">
                    <option value="">すべての種類</option>
                    <option value="tool_call">ツール呼び出し</option>
                    <option value="server_event">サーバーイベント</option>
                </select>
                <select id="logServerFilter" aria-label="サーバー">
                    <option value="">すべてのサーバー</option>
                </select>
                <select id="logStatusFilter" aria-label="ステータス">
                    <option value="">すべてのステータス</option>
                    <option value="success">success</option>
                    <option value="error">error</option>
                    <option value="timeout">timeout</option>
                    <option value="connected">connected</option>
                    <option value="disconnected">disconnected</option>
                    <option value="spawn_failed">spawn_failed</option>
                    <option value="recovered">recovered</option>
                </select>
                <input type="text" id="logSearchInput" placeholder="検索 (ツール名 / 引数 / エラー)" aria-label="検索">
                <button class="btn" onclick="loadLogs()">更新</button>
            </div>
            <div class="log-table-wrap">
                <table class="log-table">
                    <thead>
                        <tr>
                            <th>時刻</th>
                            <th>種別</th>
                            <th>サーバー</th>
                            <th>ツール</th>
                            <th>ステータス</th>
                            <th>所要時間</th>
                            <th>引数</th>
                            <th></th>
                        </tr>
                    </thead>
                    <tbody id="logTableBody"></tbody>
                </table>
                <div id="logEmpty" class="empty-state" style="display: none;">
                    <div class="empty-state-title">ログはまだありません</div>
                    <div class="empty-state-description">ツールを呼び出すと、ここに記録が表示されます。</div>
                </div>
            </div>
        </section>
```

- [ ] **Step 2: CSS 追加**

`<style>` 内のどこか（`.tabs` 定義の直後が自然）にログタブ用 CSS を追加（既存のデザイン変数 `--bg-*` / `--border-*` / `--radius-*` を使用）:

```css
        /* ============================================================
           LOG TAB
           ============================================================ */
        .log-filter-bar {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            margin-bottom: 14px;
            align-items: center;
        }
        .log-filter-bar select,
        .log-filter-bar input[type="text"] {
            padding: 7px 10px;
            border: 1px solid var(--border-default);
            border-radius: var(--radius-sm);
            background: var(--bg-surface);
            color: var(--text-primary);
            font-size: 0.82rem;
            font-family: var(--font-sans);
        }
        .log-filter-bar input[type="text"] { flex: 1; min-width: 180px; }
        .log-table-wrap { overflow-x: auto; }
        .log-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.8rem;
        }
        .log-table th, .log-table td {
            padding: 8px 10px;
            text-align: left;
            border-bottom: 1px solid var(--border-subtle);
            vertical-align: top;
        }
        .log-table th {
            color: var(--text-muted);
            font-weight: 600;
            font-size: 0.74rem;
            text-transform: uppercase;
            letter-spacing: 0.03em;
        }
        .log-status {
            display: inline-block;
            padding: 2px 8px;
            border-radius: var(--radius-sm);
            font-size: 0.72rem;
            font-weight: 600;
        }
        .log-status.success, .log-status.connected, .log-status.recovered { background: rgba(46, 160, 67, 0.15); color: #2ea043; }
        .log-status.error, .log-status.spawn_failed, .log-status.disconnected, .log-status.timeout { background: rgba(248, 81, 73, 0.15); color: #f85149; }
        .log-status.updated, .log-status.removed { background: rgba(139, 148, 158, 0.2); color: var(--text-muted); }
        .log-detail-btn {
            background: none; border: none; cursor: pointer;
            color: var(--accent-primary); font-size: 0.8rem;
        }
        .log-detail-row td { background: var(--bg-elevated); }
        .log-detail-row pre {
            white-space: pre-wrap; word-break: break-all;
            margin: 0; font-size: 0.76rem; color: var(--text-secondary);
        }
```

- [ ] **Step 3: JS — 状態・タブ切替・API.getLogs**

`currentTab` 状態宣言（:1721 付近）の近くに追加:

```js
        let currentMainTab = 'servers';
        let logPollingActive = false;
        let logPollingTimer = null;
```

`API` オブジェクト（:1757）に追加:

```js
            async getLogs(params = {}) {
                const query = new URLSearchParams();
                for (const [k, v] of Object.entries(params)) {
                    if (v !== undefined && v !== null && v !== '') query.set(k, v);
                }
                const qs = query.toString();
                const response = await fetch(`/admin/api/logs${qs ? '?' + qs : ''}`);
                if (!response.ok) throw new Error('ログの取得に失敗しました');
                return response.json();
            },
```

- [ ] **Step 4: JS — switchMainTab / loadLogs / renderLogs / ポーリング**

既存関数群（`loadServers` 等）の近くに以下を追加:

```js
        // ============================================================
        // LOG TAB
        // ============================================================
        function switchMainTab(tab) {
            currentMainTab = tab;
            document.getElementById('tabServersBtn').classList.toggle('active', tab === 'servers');
            document.getElementById('tabLogsBtn').classList.toggle('active', tab === 'logs');
            document.getElementById('serverTab').style.display = tab === 'servers' ? '' : 'none';
            document.getElementById('logTab').style.display = tab === 'logs' ? '' : 'none';
            if (tab === 'logs') {
                loadLogs();
                startLogPolling();
            } else {
                stopLogPolling();
            }
        }

        function startLogPolling() {
            if (logPollingActive) return;
            logPollingActive = true;
            const tick = () => {
                if (!logPollingActive) return;
                loadLogs().catch(() => {});
                logPollingTimer = setTimeout(tick, 5000);
            };
            tick();
        }

        function stopLogPolling() {
            logPollingActive = false;
            if (logPollingTimer) { clearTimeout(logPollingTimer); logPollingTimer = null; }
        }

        async function loadLogs() {
            const type = document.getElementById('logTypeFilter').value;
            const server = document.getElementById('logServerFilter').value;
            const status = document.getElementById('logStatusFilter').value;
            const q = document.getElementById('logSearchInput').value.trim();
            const data = await API.getLogs({ type, server, status, q, limit: 100 });
            renderLogs(data.entries || []);
        }

        function fmtLogTime(ts) {
            const d = new Date(ts * 1000);
            return d.toLocaleTimeString('ja-JP', { hour12: false }) +
                   '.' + String(d.getMilliseconds()).padStart(3, '0');
        }

        function renderLogs(entries) {
            const tbody = document.getElementById('logTableBody');
            const empty = document.getElementById('logEmpty');
            tbody.innerHTML = '';
            if (!entries.length) {
                empty.style.display = '';
                return;
            }
            empty.style.display = 'none';
            for (const entry of entries) {
                const tr = document.createElement('tr');
                const hasDetail = entry.error || entry.traceback;
                tr.innerHTML = `
                    <td>${escapeHtml(fmtLogTime(entry.ts))}</td>
                    <td>${entry.type === 'server_event' ? 'イベント' : 'ツール'}</td>
                    <td>${escapeHtml(entry.server)}</td>
                    <td>${escapeHtml(entry.tool)}</td>
                    <td><span class="log-status ${escapeHtml(entry.status)}">${escapeHtml(entry.status)}</span></td>
                    <td>${entry.duration_ms != null ? escapeHtml(entry.duration_ms + ' ms') : '—'}</td>
                    <td class="log-args">${entry.args ? escapeHtml(entry.args.slice(0, 80)) + (entry.args.length > 80 ? '…' : '') : '—'}</td>
                    <td>${hasDetail ? '<button class="log-detail-btn" onclick="toggleLogDetail(this)">詳細</button>' : ''}</td>
                `;
                if (hasDetail) {
                    const detail = document.createElement('tr');
                    detail.className = 'log-detail-row';
                    detail.style.display = 'none';
                    detail.innerHTML = `<td colspan="8"><pre>${escapeHtml([entry.error, entry.traceback].filter(Boolean).join('\n\n'))}</pre></td>`;
                    tbody.appendChild(tr);
                    tbody.appendChild(detail);
                } else {
                    tbody.appendChild(tr);
                }
            }
        }

        function toggleLogDetail(btn) {
            const detailRow = btn.closest('tr').nextElementSibling;
            if (detailRow && detailRow.classList.contains('log-detail-row')) {
                const visible = detailRow.style.display !== 'none';
                detailRow.style.display = visible ? 'none' : '';
                btn.textContent = visible ? '詳細' : '閉じる';
            }
        }

        function populateLogServerFilter(serverList) {
            const sel = document.getElementById('logServerFilter');
            const current = sel.value;
            const names = [...new Set(serverList.map(s => s.name))].sort();
            sel.innerHTML = '<option value="">すべてのサーバー</option>' +
                names.map(n => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join('');
            sel.value = current;
        }
```

※ `escapeHtml` は既存関数（showToast 等で使用済み、存在確認すること）。無ければ `renderServers` 実装に合わせて追加。

- [ ] **Step 5: DOMContentLoaded で populate + イベントバインド**

`DOMContentLoaded`（:3131-3144）に以下を追加:

```js
            populateLogServerFilter(servers);
            ['logTypeFilter', 'logServerFilter', 'logStatusFilter'].forEach(id => {
                document.getElementById(id).addEventListener('change', loadLogs);
            });
            document.getElementById('logSearchInput').addEventListener('keydown', (e) => {
                if (e.key === 'Enter') loadLogs();
            });
```

また `loadServers()` 内（`renderServers()` 呼び出し後）で `populateLogServerFilter(servers)` を呼ぶ。

- [ ] **Step 6: 構文チェック + サーバー起動確認**

```bash
node --check <(python3 -c "
import re
html = open('src/mcp_hub/static/index.html').read()
scripts = re.findall(r'<script>(.*?)</script>', html, re.S)
for i, s in enumerate(scripts):
    open(f'/tmp/mcphub_inline_{i}.js', 'w').write(s)
")
for f in /tmp/mcphub_inline_*.js; do node --check "$f" && echo "OK $f"; done
```

Expected: すべてのインライン script が構文 OK

※ 上記はシェルで実行。`re.findall` が複数 script を拾うのでループで検査する。

- [ ] **Step 7: コミット**

```bash
git add src/mcp_hub/static/index.html
git commit -m "feat(webui): サーバー/ログのタブ化とツールログダッシュボードを追加"
```

---

### Task 8: ブラウザ実機確認（AGENTS.md 必須）

**Files:**
- 変更なし（検証のみ）

- [ ] **Step 1: サーバーを起動**

```bash
MCP_HUB_DATA_DIR=/tmp/mcphub_e2e_data rtk uvicorn mcp_hub.main:app --host 0.0.0.0 --port 8080
```

※ バックグラウンドで起動し、`/admin/api/health` が `{"status":"ok"}` を返すことを確認

- [ ] **Step 2: puppeteer で実ブラウザ確認（Tailscale IP）**

Tailscale IP `100.112.180.92` 経由でアクセスし、以下を確認:
- コンソールエラーが無いこと
- タブ「サーバー / ログ」が表示され、切替が動作すること
- ログタブに「ログはまだありません」が表示されること（初期状態）
- （準備できれば）`curl -X POST http://100.112.180.92:8080/admin/api/servers/.../tools/.../call` 等でツール呼び出しを発生させ、ログ行が表示されること
- フィルタ（種類 / サーバー / ステータス / 検索）が動作すること
- エラー行の「詳細」クリックで error/traceback が展開表示されること

- [ ] **Step 3: サーバー停止 + 後片付け**

```bash
# 起動した uvicorn を停止、/tmp/mcphub_e2e_data を削除
```

- [ ] **Step 4: 全テスト最終回帰**

```bash
rtk pytest tests/ -v
```

Expected: ALL PASS

- [ ] **Step 5: 最終コミット（変更が残っていれば）**

```bash
git status --short
git log --oneline -8
```

---

## Self-Review

**1. Spec coverage:**
- リングバッファ500件 → Task 1 ✅
- マスキング（args/error/traceback、マスク→トランケーション順）→ Task 2 ✅
- ToolLogMiddleware（namespace 最長一致 / meta args 抽出 / execute_tool JSON error 判定 / metrics 修正）→ Task 3 ✅
- on_change イベント種別付き（成功系+失敗系4箇所 / unregister 保護 / tags PATCH で connect 出さない）→ Task 4 ✅
- 両 app middleware 登録 → Task 5 ✅
- GET /admin/api/logs フィルタ → Task 6 ✅
- WebUI タブ + ログテーブル + 5秒ポーリング + textContent 描画 → Task 7 ✅
- ブラウザ実機確認 → Task 8 ✅

**2. Placeholder scan:** すべてのコードステップに実コードを記載。プレースホルダなし。

**3. Type consistency:**
- `LogEntry` フィールド名: `ts/type/server/tool/status/duration_ms/args/error/traceback/id` — Task 1 定義、Task 3/5/6 で同じ名前を使用 ✅
- `on_change` callback: `cb(name, event, detail)` — Task 4 定義、Task 5 の `_on_change_rebuild(name, event, detail)` と `_on_log_event(name, event, detail)` で一致 ✅
- `mask_text(text, max_len)` / `mask_args(args)` — Task 2 定義、Task 3/5 で使用 ✅
- `ToolLogMiddleware(proxy_manager)` — Task 3 定義、Task 5 で両 app に登録 ✅
- イベント種別: `connected/disconnected/spawn_failed/recovered/removed/updated` — Task 4 発火、Task 5 記録、Task 6 フィルタ、Task 7 バッジ CSS で一致 ✅

**注意事項（実装者へ）:** Task 4 の Step 5 は `_on_change_rebuild` がまだ旧シグネチャのため既存テストが失敗する可能性がある。その場合は Task 5 を先行して行い、Task 4 の回帰確認を Task 5 完了後に再実行すること。実行順は Task 4 → 5 の依存順を守りつつ、回帰確認は柔軟に。
