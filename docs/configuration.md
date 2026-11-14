# 設定リファレンス

MCP Hub は設定ファイルと環境変数によって構成されます。設定ファイルはサーバー定義を保持し、環境変数はランタイムの動作を制御します。

---

## 設定ファイル (`hub.config.json`)

### 格納場所

設定ファイルは `MCP_HUB_DATA_DIR` で指定されたディレクトリ内の `hub.config.json` です。

| 環境変数 | デフォルト値 | 説明 |
|---|---|---|
| `MCP_HUB_DATA_DIR` | `data/` | データディレクトリ。設定ファイル (`hub.config.json`) を格納 |

デフォルトではプロジェクトルートの `data/hub.config.json` が使用されます。

### フォーマット

```json
{
  "version": 1,
  "log_level": "info",
  "embedding_model": "cl-nagoya/ruri-v3-30m",
  "meta_mode": true,
  "full_info_tools": ["fetch_fetch"],
  "mcpServers": {
    "server-name": {
      "command": "python",
      "args": ["-m", "some_mcp_server"],
      "env": {
        "API_KEY": "${MY_API_KEY}",
        "TIMEOUT": "${TIMEOUT:-30}"
      },
      "tags": ["web", "local"],
      "headers": {
        "Authorization": "Bearer ${TOKEN}"
      },
      "disabled": false
    }
  }
}
```

### トップレベルフィールド

| フィールド | 型 | デフォルト | 説明 |
|---|---|---|---|
| `version` | int | `1` | 設定ファイルバージョン。現在は `1` のみ。 |
| `log_level` | string | `"info"` | ログレベル (`debug`, `info`, `warning`, `error`)。大文字小文字を区別しない。 |
| `embedding_model` | string | `"cl-nagoya/ruri-v3-30m"` | セマンティック検索に使用する埋め込みモデル。既定は日本語特化の lightweight モデル（256dim、ONNX Runtime 経路）。モデルは 2 経路で解決する: fastembed 登録モデル（例 `intfloat/multilingual-e5-small`）と ONNX Runtime 直叩きモデル（例 `cl-nagoya/ruri-v3-30m`。ModernBERT 系で fastembed 非対応）。どちらも `embeddings` extra（`fastembed` / `onnxruntime` / `tokenizers` / `huggingface_hub`）が必要。プレフィックスは必須のモデルにのみ自動付与する（モデルプロファイル管理。`cl-nagoya/ruri-v3-30m` = `検索クエリ: `/`検索文書: `・floor 0.80、`intfloat/multilingual-e5-small` = `query: `/`passage: `・floor 0.75、`intfloat/` 配下で名前に `e5` を含むその他のモデル = `query: `/`passage: `・floor 0.30＝ファミリ既定）。未知のモデル名を指定した場合は警告ログを出してデフォルトにフォールバック。 |
| `meta_mode` | bool | `true`（バンドル設定からシード） | Meta モード（Progressive Discovery）の有効/無効。`true` のとき `search_tools` / `search_resources` / `execute_tool` / `get_schema` の 4 ツールのみ公開。設定未保存時はバンドルされた `hub.config.json` の値が初回起動時にシードされます。 |
| `full_info_tools` | array\<string\> | `[]` | フル公開するツールのリスト。要素は `"{server}_{tool}"` 形式（例: `"fetch_fetch"`）。Meta モード時、ここに指定したツールのみ `tools/list` に通常ツールとしてフル公開され、`tools/call` で直接呼び出せる。未指定（空配列）なら現行の挙動と互換。 |
| `use_embeddings` | bool | `true` | セマンティック検索（fastembed）の有効/無効。`false` で BM25 のみ。fastembed が未インストールの場合は実効値が常に `false` になる（ハードゲート）。Web UI の「⚙️ Hub 設定」からも変更可（ランタイム反映・検索インデックス再構築あり）。 |
| `llm` | object | `{}`（＝機能オフ） | LLM による説明文生成（`POST /admin/api/llm/generate`）の設定。`{provider?, model?, api_key?, base_url?}`。`api_key` 未設定なら機能オフ（生成エンドポイントは `400`）。`PATCH /admin/api/settings` の `llm` で更新する（部分マージ。空 object で全削除＝オフ）。 |
| `tool_cache` | object | `{"enabled": false, "ttl": 60, "max_entries": 500, "max_bytes": 5000000, "allow": [], "deny": []}` | 任意機能: 冪等/参照系ツール結果の TTL キャッシュ。キーは `server` + `tool` + 正規化引数（ソート済み JSON の SHA-256）。成功結果のみ保存し、エラー/例外は保存しない。`allow` が空なら `deny` 以外すべて、非空なら `allow` 一致のみ（opt-in）。パターンは fnmatch で `"{server}_{tool}"` または `"{server}"` にマッチ。サーバーの再接続/設定変更/削除で該当エントリを破棄。`PATCH /admin/api/settings` の `tool_cache` で部分更新（値変更時は全エントリ破棄）。 |
| `mcpServers` | object | `{}` | MCP サーバー定義のマップ。キーがサーバー名。 |
| `shutdown_drain_timeout` | number | `10.0` | SIGTERM/SIGINT 後のドレイン待機上限（秒）。環境変数 `MCP_HUB_DRAIN_TIMEOUT` が優先。 |

### グレースフルシャットダウン（ドレイン）

SIGTERM/SIGINT を受けると uvicorn が lifespan の shutdown を実行し、以下を順に処理します（実装: `main.py:_graceful_drain`）。

1. **新規受付停止** — 以降の HTTP リクエストは `503`（`Retry-After: 1`）、`ProxyManager.call_tool` は `RuntimeError`（`shutting down`）。継続中のリクエストはそのまま継続。
2. **ドレイン待機** — 進行中リクエスト数（`app_state.inflight`）が 0 になるまで最大 `shutdown_drain_timeout` 秒待つ。残件があれば警告ログを出して先へ進む（無限待機しない）。
3. **バックグラウンド停止** — 索引再構築（debounce/リトライ含む）、埋め込み再構築、設定ファイル監視、ヘルスチェックループを cancel。
4. **子タスク回収** — 接続/再接続タスクを cancel して待ち（孤児プロセス防止）、その後 upstream client を閉じる。

進捗と結果は `Shutdown: ...` のログで出ます（ドレイン所要時間・残件数・停止したタスク数・閉じた接続数）。

uvicorn 側にも同じ上限を `timeout_graceful_shutdown`（切り上げの int 秒）で渡すため、SSE のように自分から閉じない接続が残っていても lifespan shutdown に到達します。

### 埋め込みモデルの移行（既存インストール向け）

`embedding_model` は**保存済みの設定値が優先**されます。バンドル既定を `cl-nagoya/ruri-v3-30m` に変更しても、既に `hub.config.json` に値が保存されている環境は自動では変わりません（旧既定 `intfloat/multilingual-e5-small` のままで動きます）。新既定に切り替えるには、Web UI の「⚙️ Hub 設定」または `PATCH /admin/api/settings/embedding-model` で明示的に変更してください。

- **反映タイミング**: サーバー再起動後（PATCH は値を保存するだけで、`rebuild_index()` によるインデックス再生成は行いません）。
- **初回起動時**: 新しいモデルのダウンロード（ruri-v3-30m は ONNX 約 147MB + tokenizer、Hugging Face キャッシュ）が走ります。オフライン環境では事前に取得してください。
- **キャッシュ**: モデルごとに別ファイルなので、切替時に旧モデルの埋め込みキャッシュを消す必要はありません（`MCP_HUB_EMBED_CACHE_DIR` 直下に残ります。不要になったら手動削除）。

### サーバーエントリフィールド

各サーバーエントリは以下のフィールドを持ちます：

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `command` | string | `url` と排他 | 子サーバーを起動するコマンド。`${VAR}` / `${VAR:-default}` テンプレート使用可。 |
| `url` | string | `command` と排他 | Streamable HTTP / SSE エンドポイントのURL。https:// または http:// のみ許可。 |
| `args` | string[] | 任意 | コマンドに渡す引数リスト。 |
| `env` | object (string→string) | 任意 | 子サーバーに設定する環境変数。`${VAR}` テンプレート使用可。`url` サーバーの場合、`TOKEN` / `API_KEY` / `SECRET` / `PASSWORD` / `AUTH` を含む変数がちょうど 1 つだけなら `Authorization: Bearer <値>` ヘッダーに自動変換（2 つ以上ある場合は曖昧なため無視）。 |
| `headers` | object (string→string) | 任意 | HTTP 接続時に送信するカスタムヘッダー。 |
| `tags` | string[] | 任意 | サーバーに付与するタグ。タグフィルタリングに使用。 |
| `description` | string | 任意 | サーバーの一行説明（最大 500 字）。検索インデックスの索引テキストに前置され、`search_tools` の結果で返る。 |
| `tool_search_desc` | object (string→string) | 任意 | ツール名 → 日本語1文（80 字以内目安）のマップ。索引テキストに前置され、日本語検索が英語 docstring の語彙の壁を越えるための検索ヒント。表示には使わない（`description` は不変）。`PATCH /admin/api/servers/{name}` の部分更新、または管理 WebUI の「検索用の説明」欄（自動保存）で設定。 |
| `disabled` | boolean | `false` | `true` でサーバーをスキップ。 |
| `limits` | object | 任意 | サーバー単位の呼び出し制限（`call_timeout` / `max_concurrent` / `rate_limit` / `rate_burst` / `rate_window`）。未指定のキーはグローバル既定にフォールバック。詳細は次節。 |

#### サーバー単位の呼び出し制限（`limits`）

サーバーごとにタイムアウト・同時実行数・レート制限を上書きできます。省略したキーはグローバル既定（`MCP_HUB_CALL_TOOL_TIMEOUT` / `MCP_HUB_MAX_CONCURRENT_CALLS` / レート制限なし）にフォールバックするため、既存設定はそのまま動きます。

| キー | 型 | 範囲 | 既定 | 説明 |
|---|---|---|---|---|
| `call_timeout` | number | 0 < v ≤ 3600 | `MCP_HUB_CALL_TOOL_TIMEOUT`（30） | 1回のツール呼び出しの上限秒。超過は `ToolError`。 |
| `max_concurrent` | integer | 1–64 | グローバルセマフォ（50） | 同一サーバーへの同時呼び出し上限。超過は順番待ち。 |
| `rate_limit` | number | 0 < v ≤ 10000 | 無制限 | `rate_window` あたりの呼び出し回数。超過は順番待ち、待機が窓のほぼ全体（99%）になる場合は即 `RateLimitExceeded`。 |
| `rate_burst` | integer | 1–10000 | `rate_limit` | バケット容量（瞬間的に許す呼び出し数）。 |
| `rate_window` | number | 0 < v ≤ 3600 | `1` | レート制限の窓（秒）。 |

```json
{
  "mcpServers": {
    "heavy-crawler": {
      "command": "npx",
      "args": ["heavy-crawler-mcp"],
      "limits": { "call_timeout": 120, "max_concurrent": 2, "rate_limit": 10, "rate_window": 60 }
    }
  }
}
```

- 適用点は `LimitsMiddleware`（`src/mcp_hub/middleware.py`）。normal / meta / full_info すべての `tools/call` がここを通るため、1箇所で効きます。
- 手書き設定の壊れた `limits` は起動時に警告して無視（＝制限なしで起動）。管理 API 経由の不正値は 422 で拒否します。
- 管理 API: `PATCH /admin/api/servers/{name}` の `limits` フィールド、または `PATCH /admin/api/servers/{name}/limits`（部分更新、`null` でキー削除、空になったら `limits` ごと削除）。
- 管理 WebUI: サーバー詳細の「呼び出し制限」欄に要約が表示され、「✏️ 変更」→ 編集モーダルの「呼び出し制限」で編集。

#### `command` と `url` の排他ルール

各サーバーエントリは `command` と `url` のいずれか一方のみを持つ必要があります。両方の指定、または両方の欠落はバリデーションエラーになります。

- **command**: stdio トランスポート。サブプロセスとして起動され、stdin/stdout で通信。
- **url**: Streamable HTTP または SSE トランスポート。パスに `/sse` を含む場合は SSE、それ以外は Streamable HTTP として扱われる。

#### 環境変数テンプレート

`command`、`args`、`env` の値では `${VAR}` および `${VAR:-default}` 形式のテンプレートが使用できます。展開は `proxy_manager._create_proxy()` で行われ、設定ファイルにはテンプレートのまま保存されます。

`url` サーバーで `headers` を指定せず、`env` に認証情報系の変数（`TOKEN` / `API_KEY` / `SECRET` / `PASSWORD` / `AUTH` を含む名前）がちょうど 1 つだけある場合、`Authorization: Bearer <値>` ヘッダーが自動生成されます。複数ある場合は曖昧なため自動変換されません（`headers` で明示指定してください）。例: Mapbox MCP サーバーの場合、`env` に `MAPBOX_ACCESS_TOKEN` を設定するだけで認証が通ります。

```json
{
  "command": "node",
  "args": ["${MY_SCRIPT}"],
  "env": {
    "PORT": "${PORT:-8080}"
  }
}
```

---

## 環境変数一覧

| 環境変数 | デフォルト | 説明 |
|---|---|---|
| `MCP_HUB_PORT` | `26263` | HTTP サーバーのリスンポート |
| `MCP_HUB_HOST` | `0.0.0.0` | バインドするホストアドレス |
| `MCP_HUB_DATA_DIR` | `data` | データディレクトリ（設定ファイル・DB の格納先） |
| `MCP_HUB_RESEED` | (未設定) | `1` を設定すると起動時に DB をクリアし、設定ファイルから再シードする |
| `MCP_HUB_LOG` | `text` | `json` を設定すると JSON 構造化ログを出力 |
| `MCP_HUB_API_KEY` | (未設定) | 設定すると管理 API に X-API-Key 認証が有効になる |
| `MCP_HUB_HEALTH_INTERVAL` | `60` | ヘルスチェックの間隔（秒）。0以下で無効化 |
| `MCP_HUB_HEALTH_TIMEOUT` | `25` | ヘルスチェックのタイムアウト（秒）。202 ポーリング対応サーバー（EDINET 等）は tools/list に最大 20 秒かかるため、10 秒以下だと毎回ヘルスチェック失敗 → 切断になる。20 秒以上を推奨 |
| `MCP_HUB_HEALTH_MAX_FAILURES` | `3` | ヘルスチェックの連続失敗許容回数（実装: `proxy_manager.py:426,639`） |
| `MCP_HUB_LIST_TOOLS_TIMEOUT` | `10.0` | `list_tools` 集約のタイムアウト（秒。実装: `proxy_manager.py:425`） |
| `MCP_HUB_RETRY_MAX` | `3` | サーバー接続の最大リトライ回数 |
| `MCP_HUB_RETRY_DELAY` | `1.0` | リトライ間隔のベース delay（秒）。指数バックオフ適用 |
| `MCP_HUB_MAX_CONCURRENT_CALLS` | `50` | 同時ツール呼び出しの最大数（DoS 対策セマフォ） |
| `MCP_HUB_CALL_TOOL_TIMEOUT` | `30` | ツール呼び出しのタイムアウト（秒） |
| `MCP_HUB_LIST_TOOLS_RETRY_DELAY` | `0.3` | `list_tools()` リトライ時の遅延（秒） |
| `MCP_HUB_CLIENT_TIMEOUT` | `180.0` | アップストリームへのリクエスト読み取りタイムアウト（秒）。デフォルト 180 秒。応答しないサーバーによる tools/list のブロック防止は `MCP_HUB_LIST_TOOLS_TIMEOUT` が担当。短すぎる値（例: 5 秒）は正常稼働サーバーのツール実行も失敗させるため非推奨。WebUI の Hub 設定「⏱️ 接続タイムアウト」からも設定可能（保存値が env より優先） |
| `MCP_HUB_CONNECT_TIMEOUT` | `30.0` | 起動時の接続確認 `list_tools()` のタイムアウト（秒）。WebUI の Hub 設定「⏱️ 接続タイムアウト」からも設定可能（保存値が env より優先） |
| `MCP_HUB_RECOVERY_COOLDOWN` | `300.0` | ヘルスチェックで死んだサーバーへの自動再接続試行の最小間隔（秒）。ヘルスチェック間隔（デフォルト 60s）より長く設定すること |
| `MCP_HUB_EMBEDDING` | `1` | `0` に設定するとセマンティック検索を強制無効化（ハードキル）。`use_embeddings` 設定や Web UI トグルより優先され、ランタイム設定で再有効化できない |
| `MCP_HUB_EMBED_CACHE_DIR` | `~/.cache/mcp-hub/embeddings` | 文書単位の埋め込みディスクキャッシュ（npz）の置き場。再起動・再接続時の再 embed を省略する（A1） |
| `MCP_HUB_SESSION_IDLE_TIMEOUT` | (未設定) | アップストリームセッションのアイドル有効期限（秒）。未設定時は SDK 既定（期限なし）を維持（実装: `main.py:75-84`） |
| `MCP_HUB_WATCH_CONFIG` | (未設定) | 秒数を指定すると `hub.config.json` の mtime を監視し、変更時に `POST /admin/api/reload` と同じ差分適用を自動実行。`0`/不正値で無効（実装: `main.py:_watch_interval`, `reload.py:watch_config`） |
| `MCP_HUB_DRAIN_TIMEOUT` | `10` | SIGTERM/SIGINT 後、進行中リクエストが片付くのを待つ上限（秒）。`hub.config.json` の `shutdown_drain_timeout` より優先（不正値は設定値へフォールバック。実装: `main.py:_drain_timeout`） |
| `MCP_HUB_BOOTSTRAP_REQUIRE_HASH` | (未設定) | `1` で bootstrap のダウンロードを SHA256 ピン必須にする（未ピン URL は拒否）。既定は未ピン URL を警告して続行（実装: `bootstrap.py:_verify_download`） |
| `MCP_HUB_EXTRAS_PINS` | (未設定) | カンマ区切りで fastembed 系の要求仕様を上書き（既定 `bootstrap.py:EXTRAS_PINS` のバージョン範囲ピン） |
| `MCP_HUB_PIP_INDEX_URL` | bootstrap: `https://pypi.org/simple` / 管理 API: 未設定 | pip/uv のレジストリ。bootstrap は未設定なら pypi.org、管理 API の install は設定時のみ `--index-url` を付与。`https://` 以外は警告して無視（既定レジストリ） |
| `MCP_HUB_NPM_REGISTRY` | (未設定) | 未設定なら npm 既定レジストリ。設定時 `npm install --registry` を付与。`https://` 以外は無視 |
| `MCP_HUB_INSTALL_ALLOWED_HOSTS` | (未設定) | カンマ区切り。設定時のみ、URL 仕様（`https://` / `git+https://`）の取得元ホストをこのリストに限定。未設定は任意の https ホストを許可（後方互換。制限は `docs/security.md` 5 章） |

### `MCP_HUB_RESEED=1` の動作

1. 起動時、`JsonStore.init()` が `MCP_HUB_RESEED=1` を検出
2. 保存されている全サーバー定義をクリア
3. 設定ファイル（`hub.config.json`）の `mcpServers` で上書き
4. 設定ファイルが存在しない場合はバンドルされたデフォルト設定、または空の状態になる

これにより、「設定ファイルを正として DB を再構築する」操作が可能です。

### ログ出力

`MCP_HUB_LOG` 環境変数でログ形式を切り替えられます：

- `text`（デフォルト）: 標準的なプレーンテキスト形式
- `json`: JSON 構造化ログ。各行が `{timestamp, level, logger, message}` 形式の JSON オブジェクト

JSON ログ出力例：
```json
{"timestamp": "2026-07-20T12:00:00.000+00:00", "level": "INFO", "logger": "mcp_hub.main", "message": "MCP Hub started on 0.0.0.0:26263"}
```
