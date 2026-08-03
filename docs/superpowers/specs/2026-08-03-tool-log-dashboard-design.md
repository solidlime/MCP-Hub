# ツールログ・ダッシュボード 設計

- 日付: 2026-08-03
- ステータス: 承認済み（ユーザー + マダム・ヘルタ）

## 目的・背景

MCP-Hub は複数の MCP サーバーを集約するプロキシだが、「どのツールがいつ呼ばれたか」「サーバーがなぜ繋がらないか」がブラックボックス。ダッシュボード（WebUI）にツールログを追加し、以下を可視化する:

1. ツール呼び出しの記録（いつ・どのサーバー・どのツール・成功/失敗・所要時間）
2. サーバー接続イベント（接続・切断・起動失敗）
3. エラー詳細（stderr traceback 含む）— fetch 問題（McpError ImportError で「Connection closed」しか見えなかった件）のような診断困難を解消する

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

## アーキテクチャ

```
クライアント (MCP tools/call)
   │  POST /mcp (Streamable HTTP)
   ▼
fastmcp Server (/mcp app)
   │  tools/call ディスパッチ
   ▼
[新規] ToolLogMiddleware ← 呼び出し前後を包んで記録（通常モード・meta モード両方捕捉）
   ▼
proxy_manager (ProxyTool → 下流サーバーへ転送)
   │
   ▼
[新規] on_change 拡張 ← 接続/切断/起動失敗イベントを記録
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
    status: str        # "success" | "error" | "timeout" | "connect" | "disconnect" | "spawn_failed" | "recovered"
    duration_ms: float | None
    args: str | None   # マスク済み・最大500字で切る
    error: str | None  # エラー詳細・最大500字で切る
```

- バッファ: `collections.deque(maxlen=500)` + シーケンスカウンタ
- スレッドセーフ: asyncio.Lock（既存 `_AppState` のパターンに合わせる）

## 捕捉

### ツール呼び出し（ToolLogMiddleware）

fastmcp の middleware 機構を利用（`tag_filter.py` の TagFilterMiddleware が既存実績、`tools/call` ディスパッチは fastmcp `middleware.py` に存在）。

- `tools/call` 呼び出し前: リクエストから server（namespace）・tool 名・arguments を抽出、開始時刻記録
- 呼び出し後: 所要時間・成功/失敗を記録。エラー時はエラーメッセージを記録
- タイムアウト: `error` ステータスで記録
- 副産物修正: middleware から `inc_tool_calls()` / `inc_tool_call_errors()` を呼び、MCP プロトコル経路でも metrics が正しく数えられるようにする（現状は REST 経路のみで過少計上）

### サーバー接続イベント（proxy_manager 拡張）

`ProxyManager.on_change()` コールバック（`proxy_manager.py:335-337`、サーバー追加/削除/更新/リカバリで発火）を拡張:

- 接続成功 → `server_event / connect`
- 切断 → `server_event / disconnect`
- 起動失敗 → `server_event / spawn_failed` + stderr トレースバックを `error` に記録（fetch 問題の「Connection closed」が詳細付きで見える）
- リカバリ → `server_event / recovered`

## マスキング仕様

マスク対象の判定（`src/mcp_hub/` にユーティリティ関数を追加）:

- キー名: `api_key`, `apikey`, `token`, `secret`, `password`, `passwd`, `auth`, `key`, `credential` を部分一致で検出 → 値を `***` に置換
- 値パターン: `sk-...`（OpenAI 系）、`Bearer <token>`、`-----BEGIN ... PRIVATE KEY-----` 等の機密らしいパターン → `***` に置換
- 深さ: ネストした dict / list を再帰的に処理

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
  - エラー行はクリックで `error` 詳細を展開表示
  - 空のときは「ログはまだありません」表示
- **更新**: `API.getLogs(filters)` を追加。**ログタブがアクティブな間だけ 5 秒ポーリング**、非アクティブ時は停止（`periodicRefresh` の 30 秒ポーリングとは独立した `setTimeout` ループ）

## テスト計画

1. **ユニットテスト**（tests/）:
   - リングバッファ: 500 件超で古い順に破棄される / シーケンス id が単調増加
   - マスキング: api_key / token / sk-... / Bearer / PRIVATE KEY / ネスト dict / 通常値はそのまま
   - フィルタ API: type / server / status / q / limit の各フィルタ動作
2. **ブラウザ確認**（AGENTS.md 必須）: Tailscale IP 経由の puppeteer（本環境では 127.0.0.1 は puppeteer から到達不可、`100.112.180.92` で到達可）で:
   - コンソールエラーなし
   - タブ切り替え動作
   - ツール呼び出し後にログ行が表示される（API 経由 or 実呼び出し）
   - フィルタ操作が動作

## ファイル変更一覧

| ファイル | 変更 |
|---|---|
| `src/mcp_hub/state.py` | `LogEntry` + リングバッファ追加 |
| `src/mcp_hub/masking.py`（新規） | マスキングユーティリティ |
| `src/mcp_hub/middleware.py`（新規） | `ToolLogMiddleware` |
| `src/mcp_hub/main.py` | middleware 登録 |
| `src/mcp_hub/proxy_manager.py` | `on_change` から接続イベント記録 |
| `src/mcp_hub/admin_router.py` | `GET /admin/api/logs` |
| `src/mcp_hub/static/index.html` | タブ化 + ログタブ UI |
| `tests/` | ユニットテスト追加 |
