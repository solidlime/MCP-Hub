# ツール単位のフル公開（メタOFF）機能 設計

- 日付: 2026-08-04
- ステータス: 承認済み（ユーザー + マダム・ヘルタ）※実装レビュー（#081）BLOCK 指摘11件反映済み

## 目的・背景

meta on モードでは、MCP-Hub が公開するツールは `search_tools` / `execute_tool` / `list_upstream_tools` の3つに集約される。クライアントは個別ツールの全情報（名前・説明・inputSchema）を見られず、直接呼ぶこともできない（検索 → execute_tool 経由のみ）。

ユーザーは「ツールごとのメタOFF設定」を新設し、**指定したツールだけ全情報をクライアントに伝える**（フル公開）ようにしたい。

## スコープ

- ツール単位のフル公開指定（グローバル設定 `full_info_tools` リスト）
- meta_app の専用 middleware による「tools/list への通常ツールとしての追加」+「tools/call の直接転送」
- WebUI: サーバーカード内のツール行に「フル公開」トグル

## 非目標（YAGNI）

- サーバー単位の meta_off フラグ（粒度はツール単位のみ。デフォルト meta on は維持）
- 検索結果へのフル情報提供（フル公開ツールは tools/list に通常ツールとして出る）
- タグフィルタの meta モード対応（normal モード限定の既存挙動を維持）
- MCPDispatcher・セッション管理の変更

## アーキテクチャ

```
meta on モードの /mcp (Streamable HTTP)
   │
   ▼
meta_app (FastMCP "MCP Hub Meta": search_tools/execute_tool/list_upstream_tools)
   │  tools/list, tools/call ディスパッチ
   ▼
ToolLogMiddleware（既存・main.py:172 で登録済み。最内側）
   ▼
[新規] FullInfoMiddleware ← 登録順序は ToolLogMiddleware の「直後」（後から add した方が外側）
   │                        on_list_tools: フル公開ツール定義を Sequence[Tool] に追加
   │                        on_call_tool: フル公開対象は execute_tool 経由スキップし直接転送
   ▼
proxy_manager.call_tool(server, tool, arguments) → 下流サーバーへ転送
```

**登録順序（重要）**: fastmcp の middleware チェーンは先に add したものが内側（`server.py:514` reversed）。ToolLogMiddleware は main.py:172 で登録済みのため、FullInfoMiddleware は **main.py:172 の直後**に `meta_app.mcp.add_middleware()` する。これによりフル公開ツールの直接転送（on_call_tool 内で call_next スキップ）でも ToolLogMiddleware が必ず実行され、ツールログ・metrics が記録される。

**タグフィルタの関係**: TagFilterMiddleware は meta_app には未登録（main.py:214 は normal 側のみ）。さらにフル公開ツールは execute_tool のタグ拒否（meta_provider.py:351-373）をバイパスして直接呼べる＝**タグ制限を意図的に無効化する手段**として機能する。これは仕様上の意図として明記する。

## 設定

hub.config.json のトップレベル（`meta_mode` と同列）に追加:

```json
{
  "version": 1,
  "meta_mode": true,
  "full_info_tools": ["fetch_fetch", "sequential-thinking_sequentialthinking"]
}
```

- 形式: `"{server}_{tool}"`（mount namespace と同じ命名規則）
- 未指定時 = 空リスト = **現行と完全互換**
- **読み取り方式（重要）**: `MCPDispatcher._cached_meta_mode`（main.py:60）は meta_mode 専用のキャッシュで、`invalidate_cache()` は middleware に届かない。**キャッシュ・invalidate は使わず、`app_state.registry._data` を同期的に直接参照する**。store.py の `_write_internal`（:68）が `_data` を常に最新化しており、PATCH → setter → `_data` 更新が同一イベントループ内で完結するため、キャッシュ不要で常に最新値が読める
- バリデーション: `PATCH /settings` で `full_info_tools` を受け取る際、**`list[str]` 型チェック + 各要素が `"{server}_{tool}"` 形式か**を検証する（文字列が入ると middleware が iterable として処理して壊れる）

## フル公開 middleware（新規 `src/mcp_hub/full_info.py`）

`ToolLogMiddleware` と同じ fastmcp `Middleware` クラスを継承。`meta_app.mcp.add_middleware()` で **main.py:172（ToolLogMiddleware 登録）の直後**に登録。

**依存注入**: ToolIndex の `get_schema(server, tool_name)` を呼ぶ関数を注入する（`MetaTools` の `execute_tool_fn` 注入と同様のパターン）。middleware は ToolIndex 自体に依存せず、`get_schema_fn: Callable[[str, str], dict | None]` のみを受け取る。

**名前分解（重要）**: `split("_", 1)` は誤り（サーバー名にアンダースコアを含む場合に壊れる）。**`ToolLogMiddleware.resolve_server`（middleware.py:44-52）の接頭辞最長一致ロジックを共通化**し、以下の順で解決する:

1. `tool_name` が `full_info_tools` エントリに**完全一致**するか（フル公開対象の判定はエントリ全体との完全一致）
2. 完全一致しなければ、`connected` のサーバー名で `f"{server}_"` 接頭辞**最長一致** → `(server, tool_name[len(server)+1:])` に分解
3. どちらも一致しなければ非対象（従来通り）

この共通関数を `on_list_tools` / `on_call_tool` の両方で使用する（`middleware.py` の `resolve_server` を拡張 or 共通ユーティリティに切り出し）。

### on_list_tools

1. `tools = list(await call_next(context))` で既存ツールリスト（3ツール）を取得。**戻り値は `Sequence[Tool]`**（ListToolsResult ではない）
2. `full_info_tools` の各エントリについて、エントリ全体との完全一致でサーバー・ツールを特定
3. `get_schema_fn(server, tool)` が None を返す（未接続・ツール不在）エントリは除外
4. 成功したエントリを fastmcp `Tool` オブジェクト（name=`{server}_{tool}`、description、inputSchema を `parameters` に）として `tools` に append して返す
5. **SEP-986 名規則**: `Tool` 構築時に `_validate_tool_name` が走る（tools/base.py:216-220）。サーバー名に日本語・空白等が含まれると警告/例外の可能性があるため、構築時に例外を捕捉してスキップする（エッジケース）

### on_call_tool

1. `context.message.name` を共通分解関数で `(server, tool)` に解決
2. フル公開対象（`full_info_tools` エントリに完全一致）でない → `call_next(context)`（従来通り）
3. フル公開対象 → **`proxy_manager.call_tool(server, tool, arguments)` を直接呼び、返り値をそのまま return** する（`call_tool` は既に `ToolResult` を返す（server.py:1179-1215）。包み直すと structured_content が失われるため、そのまま返す。execute_tool 経由の JSON 往復をスキップ）
4. 呼び出し例外 → `ToolResult(is_error=True, content=[TextContent(text=str(e))])` を返す（**content か structured_content は必須**。tools/base.py:119-120 で両方 None だと ValueError になる）

## API

- `GET /settings`（admin_router.py:80-86）: レスポンスに `full_info_tools` を追加
- `PATCH /settings`（admin_router.py:89-95）: `full_info_tools` の更新を受け付ける。**`list[str]` + 各要素 `"{server}_{tool}"` 形式のバリデーション**を行い、不正なら 422

`meta_mode` と同じ流儀で追加。専用エンドポイントは作らない。`store.py` に `set_full_info_tools` を追加する（キャッシュ無効化は不要。registry._data 直接参照のため）。

## WebUI（src/mcp_hub/static/index.html）

- `renderServerCard`（:2157-2236）内の各 `tool-item` に**「フル公開」トグル**を追加（既存の有効/無効トグルの toggle-switch スタイルを流用）
- **ツール名は `tool.name` をそのまま使う（重要）**: `server.tools[].name` は既に namespace 付き `"{server}_{tool}"`（proxy_manager.py:94 `mount(namespace=name)` → :390 の返す名前）。`server.name + "_" + tool.name` で連結すると `fetch_fetch_fetch` になる
- トグル操作 → `PATCH /settings` で `full_info_tools` に `tool.name` を追加/削除 → 成功後カード再レンダリング
- 状態は `loadSettings` 時に取得した `full_info_tools` から初期化（`tool.name` の完全一致で判定）
- **meta off 時**: normal モードでは全ツール公開済みなのでトグルは無意味。meta on 時のみ表示する（`metaMode` 状態で制御）

## エラー処理・関係

- フル公開対象が未接続・ツール不在: `tools/list` から除外、`tools/call` はエラー応答
- **ツールログ**: ToolLogMiddleware は meta_app に登録済み（main.py:172）で、FullInfoMiddleware はその**直後**に登録するため、フル公開ツールの呼び出しも自動的にログ記録される（登録順序で保証）
- **タグフィルタ**: normal モード限定の既存挙動を維持。フル公開ツールは execute_tool のタグ拒否をバイパスして直接呼べる（意図的仕様）

## テスト計画

1. **ユニットテスト**（tests/）:
   - middleware: on_list_tools でフル公開ツールが追加される / 未接続・不在ツールは除外される / 非対象ツールは従来通り / on_call_tool で直接転送される（`call_next` が呼ばれない）/ 直接転送の例外が `ToolResult(is_error=True, content=[TextContent])` になる / SEP-986 例外でスキップされる
   - 名前分解: 接頭辞関係サーバー（`fetch` / `fetch_tools`）のエッジケース / アンダースコア入りサーバー名
   - API: `PATCH /settings` で full_info_tools が更新され GET に反映される / 不正型（文字列・形式外）が 422 になる
   - 設定: 未指定時に空リスト（互換性）
   - **統合テスト**: 登録順序で ToolLog が必ず実行される（フル公開ツールの呼び出しで tool_call ログが記録される）/ tools/list で見えた名前で tools/call が通る往復
2. **ブラウザ確認**（AGENTS.md 必須）: Tailscale IP 経由 puppeteer で、トグル操作 → API 反映 → ツールカード再描画、コンソールエラーなし

## ファイル変更一覧

| ファイル | 変更 |
|---|---|
| `src/mcp_hub/full_info.py`（新規） | `FullInfoMiddleware`（on_list_tools / on_call_tool、get_schema_fn 注入、共通分解関数使用） |
| `src/mcp_hub/middleware.py` | `resolve_server` を共通分解関数に拡張（フル公開エントリ完全一致 + 接頭辞最長一致） |
| `src/mcp_hub/store.py` | `set_full_info_tools`（バリデーション付き、キャッシュ不要） |
| `src/mcp_hub/admin_router.py` | `GET/PATCH /settings` に `full_info_tools` 追加（422 バリデーション） |
| `src/mcp_hub/main.py` | `FullInfoMiddleware` を **ToolLogMiddleware 登録（:172）の直後**に登録（get_schema_fn 注入） |
| `src/mcp_hub/meta_provider.py` | （必要なら）ToolIndex の get_schema を公開 |
| `src/mcp_hub/static/index.html` | tool-item にフル公開トグル（`tool.name` をそのまま使用、meta on 時のみ表示） |
| `tests/` | ユニットテスト + 統合テスト追加 |
