# ツールログ・ダッシュボード 設計

- 日付: 2026-08-03
- ステータス: 承認済み（ユーザー + マダム・ヘルタ）

## 目的・背景

MCP-Hub は複数の MCP サーバーを集約するプロキシだが、「どのツールがいつ呼ばれたか」「サーバーがなぜ繋がらないか」がブラックボックス。ダッシュボード（WebUI）にツールログを追加し、以下を可視化する:

1. ツール呼び出しの記録（いつ・どのサーバー・どのツール・成功/失敗・所要時間）
2. サーバー接続イベント（接続・切断・起動失敗）
3. エラー詳細（例外トレースバック含む）— fetch 問題（McpError ImportError で「Connection closed」しか見えなかった件）のような診断困難を解消する

## スコープ

- ツール呼び出し記録 + サーバー接続イベント + エラー詳細、すべてダッシュボードでフィルタ表示可能
- 保持: メモリのみのリングバッファ（最新 500 件、古い順に破棄、再起動で消える）
- 引数はマスクして記録（機密値は `***` に置換）
- UI: 「サーバー / ログ」のタブ式ナビゲーションを追加

## 非目標（YAGNI）

- ログの永続化（SQLite / ファイル）
- クライアント識別（誰が呼んだか）
- ページネーション・エクスポート
- 複数ノード集約
- サーバー毎の stderr ファイル化（`StdioTransport` の `log_file` 配線。現状 stderr はグローバル sys.stderr にのみ出力され捕捉不可。例外トレースバックのみ記録する）

## アーキテクチャ

```
クライアント (MCP tools/call)
   │  POST /mcp (Streamable HTTP)
   ▼
fastmcp Server (/mcp app: normal_app と meta_app の両方)
   │  tools/call ディスパッチ
   ▼
[新規] ToolLogMiddleware（両 app に登録）← 呼び出し前後を包んで記録
   │       ・通常モード: namespace 接頭辞最長一致でサーバー特定
   │       ・meta モード: execute_tool の arguments から実サーバー・実ツール抽出
   │       ・execute_tool の JSON error は status=error に判定
   ▼
proxy_manager (ProxyTool → 下流サーバーへ転送)
   │
   ▼
[新規] on_change を cb(name, event, detail) 形式に変更
        ← 成功系（connected/recovered）だけでなく失敗系
           （spawn_failed/disconnected/removed/updated）にも発火
   ▼
_AppState.ToolLogBuffer (deque maxlen=500, リングバッファ)
   ▲
GET /admin/api/logs?type=&server=&status=&q=&limit= (サーバー側フィルタ)
   │
   ▼
WebUI ログタブ（フィルタ行 + テーブル、表示中のみ 5 秒ポーリング）
```

## データモデル

`src/mcp_hub/state.py` の `_AppState` にリングバッファを追加する。

```python
@dataclass
class LogEntry:
    id: int            # 単調増加シーケンス
    ts: float          # epoch 秒
    type: str          # "tool_call" | "server_event"
    server: str        # サーバー名（該当なしは "-"）
    tool: str          # ツール名（server_event では "-"）
    status: str        # "success" | "error" | "timeout" | "connected" | "disconnected" | "spawn_failed" | "recovered" | "removed" | "updated"
    duration_ms: float | None
    args: str | None   # マスク済み・最大500字（tool_call のみ）
    error: str | None  # エラー概要・最大500字（マスク適用済み）
    traceback: str | None  # 例外トレースバック・最大4000字（マスク適用済み、詳細展開用）
```

- バッファ: `collections.deque(maxlen=500)` + シーケンスカウンタ
- スレッドセーフ: asyncio.Lock（既存 `_AppState` のパターンに合わせる）。**id インクリメントと deque.append は同一ロック獲得内で原子的に実行**（別々だと並行時に id 重複）。読み取り側もロック下でスナップショットを取ること
- **error と traceback の分離**: トレースバックは500字に収まらないため、短い error（概要）と長い traceback（詳細展開用）を別フィールドに分離する

## 捕捉

### ツール呼び出し（ToolLogMiddleware）

fastmcp の middleware 機構を利用（`tag_filter.py` の TagFilterMiddleware が既存実績、`tools/call` ディスパッチは fastmcp `middleware.py` に存在）。

- `tools/call` 呼び出し前: `context.message`（`CallToolRequestParams`）から tool name・arguments を抽出、開始時刻記録
- 呼び出し後: 所要時間・成功/失敗を記録。エラー時は例外メッセージと `traceback.format_exc()` を記録
- タイムアウト: `error` ステータスで記録
- 副産物修正: middleware から `inc_tool_calls()` / `inc_tool_call_errors()` を呼び、MCP プロトコル経路でも metrics が正しく数えられるようにする（現状は REST 経路のみで過少計上）

**サーバー名の解決（重要）**: middleware が受け取るのは `CallToolRequestParams`（name / arguments）のみで Tool オブジェクトは渡らない。`Tool._server` 属性は fastmcp 3.4.4 に存在しないため使えない。以下で解決する:

- **通常モード**: マウントは `{namespace}_{tool}` 形式（fastmcp Namespace transform、`f"{prefix}_"`）。`name` が `f"{server}_"` で始まる**最長一致**で `pm.get_connected_servers()` から逆引きする
- **meta モード**: tool name が `execute_tool` の場合、arguments の `{"server":..., "tool_name":..., "arguments":...}` から実サーバー・実ツールを抽出する
- **エッジケース**: サーバー名が互いに接頭辞関係（`fetch` と `fetch_tools`）の場合の曖昧さは、**最長一致で固定しテストで担保**する

**meta モードのエラー判定（必須）**: `execute_tool` はタグ拒否・ツール不在を JSON 文字列で 200 返すだけ（`is_error=False`）なので、middleware はそのまま成功と判定してしまう。`execute_tool` 呼び出し時は**戻り値の JSON に `error` キーがあるか / `result.is_error` を検査**して status を決める分岐を入れる。これがないと失敗診断という本機能の目的が meta モードで空洞化する。

**middleware 登録は normal_app と meta_app の両方（必須）**: 現状 `mcp_server.add_middleware(TagFilterMiddleware)` は normal 側のみ（main.py:192）。meta モード時は MCPDispatcher がリクエスト全体を meta_app に回すため、**meta_app にも登録しないと meta モードのログが完全に欠落する**。登録は `create_meta_app` 後（main.py:166 付近）に追加する。

### サーバー接続イベント（proxy_manager 拡張）

現状の `on_change()` コールバックは**成功系のみ**で発火する（`_connect_and_mount` 成功 :131、リカバリ成功 :460/:493、refresh、unregister）。初期接続失敗（:136-146）、health check の connected→error 遷移（:422-432）、リカバリ失敗（:458, :491）では**発火しない**。したがって `on_change` の「拡張」だけでは disconnect/spawn_failed を捕捉できない。

**イベント種別付き callback を新設**し、成功系・失敗系の両方で発火させる:

```python
# 新シグネチャ: cb(name: str, event: str, detail: dict | None)
# event: "connected" | "disconnected" | "spawn_failed" | "recovered" | "removed" | "updated"
```

発火箇所（proxy_manager.py）:
| イベント | 発火箇所 |
|---|---|
| `connected` | `_connect_and_mount` 成功（:131-135）、リカバリ成功（:459-464, :493）、refresh で proxy 再生成成功（:245-254） |
| `disconnected` | health check で connected→error 遷移（:422-432）、unregister_server |
| `spawn_failed` | 初期接続失敗（:136-146）、リカバリ失敗（:458, :491）— 例外メッセージ + `traceback.format_exc()` を detail に |
| `recovered` | リカバリ成功時 |
| `removed` | unregister_server |
| `updated` | refresh_server（**proxy が実際に再生成されたときのみ**。tags だけの PATCH で connect が出ると誤解を招く） |

既存 consumer の `_on_change_rebuild`（main.py:174-183）は引数なしで呼ばれる前提なので、新シグネチャに合わせて更新する。また **unregister_server の callback ループ（:215-216）には try/except が無い**ため、新しい callback が例外を投げると DELETE API が 500 になる。他と同様に保護する。

**エラー詳細の制約（stderr は取れない）**: `StdioTransport` は subprocess stderr をグローバル `sys.stderr` に書くだけで、サーバーごとの捕捉はできない（`_create_proxy` で `log_file` 未指定）。捕捉できるのは**例外メッセージ + `traceback.format_exc()`** のみ。fetch 問題のような起動時 ImportError は、初期接続失敗時の例外に MCP SDK の「Connection closed」等が現れるので、それを記録する。サーバー毎の stderr ファイル化（`log_file` 配線）は**今回のスコープ外**とする。

**代替案の検討メモ（不採用）**: middleware ではなく `proxy_manager.call_tool()`（:317-333）での一元記録も検討した。REST・meta・通常の3経路が全て通り namespace パースも不要で metrics 修正も自然に1箇所で済む。ただし (a) ローカルツール（meta の3ツール自身）の呼び出し記録が消える、(b) ディスパッチ前エラー（ツール不在等）が記録できない、(c) REST 経路は admin API の認証済み操作でありログ目的（下流サーバーの利用状況）とは性質が違う、の理由で **middleware 方式を採用**する。

## マスキング仕様

マスク対象の判定（`src/mcp_hub/` にユーティリティ関数を追加）:

- キー名: `api_key`, `apikey`, `token`, `secret`, `password`, `passwd`, `auth`, `key`, `credential` を部分一致で検出 → 値を `***` に置換
- 値パターン: `sk-...`（OpenAI 系）、`Bearer <token>`、`-----BEGIN ... PRIVATE KEY-----` 等の機密らしいパターン → `***` に置換
- 深さ: ネストした dict / list を再帰的に処理
- **適用対象**: `args` だけでなく **`error` / `traceback` にも適用**（fetch 系エラーは URL+トークンを含むことが多い）
- **順序固定**: 「マスク → トランケーション」の順で処理（先に切ると PRIVATE KEY ブロックが途中で切れてパターン不一致になる）

## API

`src/mcp_hub/admin_router.py` に追加（`/metrics` の直後、既存ルーターに従う）:

```
GET /admin/api/logs?type=tool_call&server=fetch&status=error&q=mcp&limit=100
→ {
    "entries": [LogEntry...],   # 新しい順
    "total": 123,               # フィルタ適用後の総数
  }
```

- フィルタはサーバー側で適用（リングバッファ 500 件の走査は軽量）
- `limit` デフォルト 100、最大 500
- `q` は tool 名 / args / error に対する部分一致

## WebUI（src/mcp_hub/static/index.html）

- **タブナビゲーション**: 既存の未使用 `.tabs` CSS（`index.html:857-890`）を流用。「サーバー」「ログ」タブを追加。サーバー追加ボタン・metrics-bar・タグフィルタはサーバータブに属する
- **ログタブの構成**:
  - フィルタ行: 種類 select（all / tool_call / server_event）、サーバー select（all + 動的）、ステータス select、検索 input、手動更新ボタン
  - テーブル: 時刻 / 種別 / サーバー / ツール / ステータス（色分けバッジ）/ 所要時間 / 引数（マスク済み）/ 詳細
  - エラー行はクリックで `error` / `traceback` 詳細を展開表示
  - 空のときは「ログはまだありません」表示
  - **XSS 対策**: client 由来の文字列（args / error / traceback）を描画するときは `innerHTML` を使わず **`textContent` / エスケープ必須**
- **更新**: `API.getLogs(filters)` を追加。**ログタブがアクティブな間だけ 5 秒ポーリング**、非アクティブ時は停止（`periodicRefresh` の 30 秒ポーリングとは独立した `setTimeout` ループ）

## テスト計画

1. **ユニットテスト**（tests/）:
   - リングバッファ: 500 件超で古い順に破棄される / シーケンス id が単調増加（並行 append でも id 重複なし）
   - マスキング: api_key / token / sk-... / Bearer / PRIVATE KEY / ネスト dict / 通常値はそのまま / **マスク→トランケーション順序** / error・traceback にも適用
   - フィルタ API: type / server / status / q / limit の各フィルタ動作
   - **サーバー名解決**: namespace 最長一致（`fetch` と `fetch_tools` の接頭辞関係のエッジケース含む）/ meta モードは `execute_tool` の arguments から抽出
   - **meta モードのエラー判定**: `execute_tool` が JSON error を返した場合に status=error になる
   - **on_change 発火**: 成功系・失敗系（初期接続失敗 / connected→error 遷移 / リカバリ失敗）の全箇所でイベントが記録される / tags だけの PATCH では connect が出ない
2. **ブラウザ確認**（AGENTS.md 必須）: Tailscale IP 経由の puppeteer（本環境では 127.0.0.1 は puppeteer から到達不可、`100.112.180.92` で到達可）で:
   - コンソールエラーなし
   - タブ切り替え動作
   - ツール呼び出し後にログ行が表示される（API 経由 or 実呼び出し）
   - フィルタ操作が動作

## ファイル変更一覧

| ファイル | 変更 |
|---|---|
| `src/mcp_hub/state.py` | `LogEntry` + リングバッファ追加 |
| `src/mcp_hub/masking.py`（新規） | マスキングユーティリティ（args / error / traceback 対応、マスク→トランケーション順） |
| `src/mcp_hub/middleware.py`（新規） | `ToolLogMiddleware`（namespace 最長一致 + meta args 抽出 + execute_tool エラー判定） |
| `src/mcp_hub/main.py` | **normal_app と meta_app の両方**に middleware 登録、`_on_change_rebuild` を新シグネチャに更新 |
| `src/mcp_hub/proxy_manager.py` | `on_change` を `cb(name, event, detail)` 形式に変更 + 失敗系 4 箇所（初期接続失敗 / connected→error / リカバリ失敗×2）への発火追加 + unregister callback ループの try/except 保護 |
| `src/mcp_hub/admin_router.py` | `GET /admin/api/logs` |
| `src/mcp_hub/static/index.html` | タブ化 + ログタブ UI（textContent で描画） |
| `tests/` | ユニットテスト追加 |
