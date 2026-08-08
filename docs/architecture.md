# アーキテクチャ

## 概要

MCP Hub は複数の MCP サーバーへのプロキシ兼レジストリです。
LLM クライアントは単一のエンドポイント (`/mcp`) に接続するだけで、背後に登録されたすべての子サーバーのツール・リソース・プロンプトにアクセスできます。

```
┌──────────────┐     ┌──────────────────────────────────────┐
│  LLM Client  │────▶│           MCP Hub (:26263)           │
│  (Claude等)   │     │                                      │
└──────────────┘     │  ┌────────┐  ┌────────┐  ┌────────┐ │
                     │  │ Proxy  │  │ Proxy  │  │ Proxy  │ │
                     │  │  #1    │  │  #2    │  │  #3    │ │
                     │  └───┬────┘  └───┬────┘  └───┬────┘ │
                     └──────┼──────────┼───────────┼───────┘
                            │          │           │
                     ┌──────┘    ┌─────┘     ┌─────┘
                     ▼           ▼           ▼
                 ┌────────┐ ┌────────┐ ┌────────┐
                 │ Server │ │ Server │ │ Server │
                 │  A     │ │  B     │ │  C     │
                 │(stdio) │ │(HTTP)  │ │(SSE)   │
                 └────────┘ └────────┘ └────────┘
```

---

## コアコンポーネント

### JsonStore — 永続化レイヤー

`src/mcp_hub/store.py`

サーバー設定の永続化を担当します。データは `{MCP_HUB_DATA_DIR}/hub.config.json` に JSON 形式で保存されます。

**特徴：**
- アトミック書き込み：一時ファイルに書き込んだ後 `os.replace` で置き換え（破損防止）
- `asyncio.Lock` によるスレッドセーフな読み書き
- テンプレート値 (`${VAR}`) は展開せずそのまま保存。展開は ProxyManager が行う

**主要メソッド：**

| メソッド | 説明 |
|---|---|
| `init(seed_servers)` | 初回起動時の初期化。バンドル設定のコピー、`MCP_HUB_RESEED` 処理 |
| `list_servers()` | 全サーバー一覧を `[{name, config}, ...]` 形式で返す |
| `get_server(name)` | 単一サーバーの設定を取得 |
| `add_server(name, config)` | サーバーを追加 |
| `update_server(name, config)` | サーバー設定を更新 |
| `remove_server(name)` | サーバーを削除 |
| `set_meta_mode(enabled)` | meta_mode を更新 + MCPDispatcher のキャッシュを無効化 |
| `set_embedding_model(model)` | 埋め込みモデル名を更新 |
| `set_full_info_tools(tools)` | フル公開ツール一覧を更新 |

レジストリ操作（`src/mcp_hub/registry.py`）は JsonStore の薄いラッパーとして機能します。

---

### ProxyManager — プロキシライフサイクル管理

`src/mcp_hub/proxy_manager.py`

子サーバーへの接続を管理し、FastMCP のプロキシ機構を使用してマウントします。

**責務：**
1. **接続管理**: `create_proxy()` で子サーバーへのプロキシを生成
2. **マウント/アンマウント**: FastMCP インスタンスへの proxy の追加・削除
3. **ヘルスモニタリング**: 定期的なヘルスチェックと障害サーバーの自動リカバリ
4. **ツール一覧・呼び出し**: 全サーバーのツール一覧取得とツール呼び出しのディスパッチ

**接続方式（`_create_proxy`）：**

```python
if url:
    # パスに /sse を含む → SSETransport
    # それ以外 → StreamableHttpTransport
    headers = config.get("headers")
    transport = SSETransport(url=url, headers=headers)  # または
    transport = StreamableHttpTransport(url=url, headers=headers)
    client = Client(transport=transport)
    proxy = create_proxy(client, name=name)
else:  # command
    transport = StdioTransport(command=command, args=args, env=env)
    proxy = create_proxy(transport, name=name)
```

**3 つのトランスポート対応：**

| トランスポート | 接続方式 | ユースケース |
|---|---|---|
| stdio | サブプロセス（`StdioTransport`） | ローカルの MCP サーバー |
| Streamable HTTP | HTTP POST（`StreamableHttpTransport`） | リモートの MCP サーバー |
| SSE | Server-Sent Events（`SSETransport`） | SSE ベースの MCP サーバー |

**ヘルスモニタリング：**

- 起動時に `start_health_monitor()` でバックグラウンドタスク開始
- デフォルト 60 秒間隔で全サーバーの接続確認
- タイムアウト（10秒）発生時はステータスを `error` に変更
- エラー状態のサーバーに対して指数バックオフリトライで再接続
- `on_change` コールバックでメタインデックスの再構築をトリガー

**同時実行制御：**

- `asyncio.Semaphore(50)` で同時ツール呼び出し数を制限（`MCP_HUB_MAX_CONCURRENT_CALLS` で調整可能）
- ツール呼び出し前に rebuild 完了を `asyncio.Event` で待機

---

### MCPDispatcher — ASGI ディスパッチャー

`src/mcp_hub/main.py`（クラス `MCPDispatcher`）

`/mcp` パスにマウントされるカスタム ASGI アプリで、`meta_mode` 設定に応じて通常アプリと meta アプリを動的に切り替えます。

**動作フロー：**

```
Request → /mcp
  │
  ├─ meta_mode キャッシュが None → レジストリから読み取り
  │
  ├─ meta_mode = False → normal_app (全ツール公開)
  │
  └─ meta_mode = True  → meta_app (3ツールのみ公開)
                        └─ full_info_tools 指定ツールは通常ツールとして追加公開
```

**フル公開ツール（`full_info_tools`）：**
meta_mode = True でも、`full_info_tools` に登録された `"{server}_{tool}"` は `meta_app` に `FullInfoMiddleware` 経由で通常ツールとして公開されます。`tools/list` に description / inputSchema 付きで現れ、`tools/call` で直接呼び出せます（`execute_tool` 経由せず、ProxyManager に直接転送）。タグフィルタリングの対象外です（意図的仕様）。

**キャッシュ機構：**
- `_cached_meta_mode` でモードをキャッシュし、リクエストごとのレジストリ読み取りを回避
- `invalidate_cache()` でキャッシュをクリア（`set_meta_mode()` から呼ばれる）
- 次回リクエスト時にレジストリから再読み込み

**セッションクリーンアップ：**
- 5分間隔のバックグラウンドタスクで非アクティブサイド（直近で使用されていない方のアプリ）のセッションを削除
- これによりメモリリークを防止

---

### TagFilterMiddleware — タグフィルタリング

`src/mcp_hub/tag_filter.py`

FastMCP の Middleware 機構を使用し、`tools/list`、`resources/list`、`resources/templates/list`、`prompts/list` の応答をフィルタリングします。

**動作：**

1. リクエスト時に `tag_middleware`（main.py の HTTP ミドルウェア）が `X-MCP-Hub-Tags` ヘッダーまたは `?tags=` クエリを解析
2. パースされたタグを `request_tags` ContextVar に保存
3. TagFilterMiddleware が各 list レスポンスをインターセプト
4. 各アイテムの所属サーバーのタグとリクエストタグを比較（OR 論理）
5. マッチしないアイテムを応答から除外
6. ローカル（非プロキシ）のツール・リソースは常に通過

**タグマッチングロジック（`state.tags_match`）：**
```python
def tags_match(requested, server_tags):
    if not requested:
        return True  # フィルターなし → 全通過
    return any(t in server_tags for t in requested)  # OR
```

---

### ToolLogMiddleware — ツール呼び出しログ

`src/mcp_hub/middleware.py`

FastMCP の Middleware 機構を使用し、`tools/call` の実行前後をフックしてツール呼び出しをログリングバッファに記録します。`mcp_server`（通常モード）と `meta_app.mcp`（Meta モード）の両方に登録されます。

**記録内容（`state.LogEntry`）：**
- 呼び出し元サーバー・ツール名（`resolve_server` による名前分解）
- ステータス（`success` / `error`）と所要時間（ms）
- 引数・エラー・トレースバック（`masking.mask_args` / `mask_text` で機密マスキング、長さ制限付き）
- `execute_tool` 経由の呼び出しは結果の JSON に `error` キーが含まれる場合 `error` ステータスと判定

ログバッファは `_AppState` 上の `collections.deque(maxlen=500)` リングバッファで、メモリのみ（再起動で消去）。`inc_tool_calls` / `inc_tool_call_errors` もここで呼ばれ、MCP プロトコル経路のメトリクス過少計上が修正されています。

**サーバー接続イベント：**
`ProxyManager.on_change` コールバック（`cb(name, event, detail)` 形式）経由で、接続・切断・スパウン失敗・リカバリ等の `server_event` も同バッファに記録されます（`main._on_log_event`）。

---

### FullInfoMiddleware — フル公開ツール

`src/mcp_hub/full_info.py`

Meta モード時に `full_info_tools` で指定されたツールを通常ツールとして公開します。`meta_app.mcp` に ToolLogMiddleware の直後（内側）に登録されます（登録順により、直接転送でもツールログが必ず記録される）。

- **`on_list_tools`**: フル公開対象のツール定義（description / inputSchema）を `ToolIndex.get_schema` から取得し、`Tool` として `tools/list` の応答に追加
- **`on_call_tool`**: フル公開対象のツール名なら `call_next` をスキップし `ProxyManager.call_tool(server, tool, arguments)` に直接転送
- 名前分解は `split_qualified_name`（接頭辞最長一致）で `"{server}_{tool}"` を解釈
- 設定は `registry._data["full_info_tools"]` を同期参照（キャッシュ不要）

---

### MetaApp — Progressive Discovery

`src/mcp_hub/meta_provider.py`

meta_mode が有効な場合に動作する特殊な FastMCP アプリです。全子サーバーのツール定義をクライアントに直接公開する代わりに、3 つのメタツールのみを公開します。

**効果の比較：**

| 項目 | 通常モード | Meta モード |
|---|---|---|
| 公開ツール数 | 全ツール（例: 84 ツール） | 3 ツール + `full_info_tools` 指定分 |
| コンテキスト消費 | ~15K tokens | ~500 tokens（フル公開ツール分を除く） |
| ツール発見 | クライアントの `tools/list` | `search_tools` のクエリ検索 |

フル公開ツールは Meta モードでも `tools/list` / `tools/call` で直接利用できます（`FullInfoMiddleware`）。

**3 つのメタツール：**

| ツール | 説明 |
|---|---|
| `search_tools(query, top_k=10)` | キーワードまたはセマンティック検索でツールを発見 |
| `execute_tool(server, tool_name, arguments)` | 検索で見つけたツールを実行 |
| `list_upstream_tools()` | 全ツールの概要をサーバー別に一覧 |

**ToolIndex — 検索エンジン：**

`ToolIndex` クラスがツールの検索インデックスを管理します。

- **プライマリ検索**: fastembed による dense retrieval（コサイン類似度）
  - モデル: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`（設定で変更可能。fastembed 非対応モデル指定時はデフォルトにフォールバック）
  - ドキュメント: `"{server}/{name}: {description}"` 形式で埋め込み
- **フォールバック**: BM25Okapi によるキーワード検索
  - fastembed 未インストール時、またはセマンティック検索が 0 件の場合に使用
  - コード認識トークナイザー（camelCase 分割、digit 境界分割）
  - BM25F 近似のためのトークン重複（名前×5、サーバー名×3、説明×2、パラメーター名×1）
- **小規模コーパスフォールバック**: 5 件以下のサーバーでは単純な TF 重み付け

**インデックス再構築（rebuild_index）：**

- サーバー追加・削除・更新時に `on_change` コールバック経由でトリガー
- 起動時のカスケード接続は 500ms のデバウンスで統合
- 全接続サーバーを走査し、ツール定義を収集後 ToolIndex に投入

---

## サーバーライフサイクル

```
起動
  │
  ├─ load_config() → hub.config.json を読み込み
  │
  ├─ JsonStore.init() → hub.config.json の初期化・シード
  │
  ├─ ProxyManager.load_all() → hub.config.json から全サーバーを非同期で接続開始
  │    各サーバー: create_task(_connect_and_mount)
  │      ├─ _create_proxy() → トランスポート選択＋プロキシ生成
  │      ├─ list_tools() で接続確認（タイムアウト 30s）
  │      ├─ mcp.mount() でマウント
  │      ├─ on_change → rebuild_index（500ms デバウンス）
  │      └─ 失敗 → status="error"、ヘルスモニターがリカバリ
  │
  ├─ ヘルスモニター起動（60秒間隔）
  │
  └─ Meta インデックス初回構築

運転中
  │
  ├─ リクエスト → MCPDispatcher が振り分け
  ├─ 定期的ヘルスチェック → 障害サーバーの自動リカバリ
  ├─ 5分ごとに非アクティブセッションをクリーンアップ
  │
  └─ API 経由の変更 → registry 更新 → on_change → rebuild_index

終了
  │
  ├─ dispatcher.shutdown() → セッションクリーンアップタスク停止
  ├─ health_monitor 停止
  └─ FastMCP の lifespan 終了
```

---

## 既知の制限事項

### Progress Notification 非対応

FastMCP の `ProxyProvider` は `onprogress` コールバックを公開していないため、子サーバーからの進捗通知（Progress Notification）はクライアントに転送されません。長時間実行ツールの進捗状況はクライアントから確認できません。

### FastMCP 内部 API 依存

MCP Hub は以下の FastMCP 内部/プライベート API に依存しています：

- `FastMCP._mcp_server`
- `FastMCP._lifespan_manager`
- `FastMCP.providers`
- `FastMCP.local_provider`
- `session_manager`
- `StreamableHTTPASGIApp`
- `FastMCPStreamableHTTPSessionManager`

これらの API は FastMCP のメジャーバージョン更新で変更される可能性があります。
現在は FastMCP `<4.0.0` で動作確認済みです。

**バージョンガード：**
- `pyproject.toml` で `fastmcp>=3.4.0,<4.0.0` にピン止め
- 起動時に `fastmcp.__version__` が `>=4.0.0` の場合、警告ログを出力
- インストール時に FastMCP `>=3.5.0` が検出されると `sys.exit(1)` で強制終了

### SSE 再接続

SSE トランスポートを使用するサーバーは、切断時に FastMCP のクライアントライブラリが自動的に再接続を試みますが、この挙動は FastMCP 側の実装に依存します。
