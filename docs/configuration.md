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
  "embedding_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
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
| `embedding_model` | string | `"sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"` | セマンティック検索に使用する埋め込みモデル。fastembed がインストールされている場合に有効。fastembed がサポートしないモデルを指定した場合は警告ログを出してデフォルトにフォールバック。 |
| `meta_mode` | bool | `true`（バンドル設定からシード） | Meta モード（Progressive Discovery）の有効/無効。`true` のとき `search_tools` / `execute_tool` / `list_upstream_tools` の 3 ツールのみ公開。設定未保存時はバンドルされた `hub.config.json` の値が初回起動時にシードされます。 |
| `full_info_tools` | array\<string\> | `[]` | フル公開するツールのリスト。要素は `"{server}_{tool}"` 形式（例: `"fetch_fetch"`）。Meta モード時、ここに指定したツールのみ `tools/list` に通常ツールとしてフル公開され、`tools/call` で直接呼び出せる。未指定（空配列）なら現行の挙動と互換。 |
| `mcpServers` | object | `{}` | MCP サーバー定義のマップ。キーがサーバー名。 |

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
| `disabled` | boolean | `false` | `true` でサーバーをスキップ。 |

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
| `MCP_HUB_HEALTH_TIMEOUT` | `10` | ヘルスチェックのタイムアウト（秒） |
| `MCP_HUB_RETRY_MAX` | `3` | サーバー接続の最大リトライ回数 |
| `MCP_HUB_RETRY_DELAY` | `1.0` | リトライ間隔のベース delay（秒）。指数バックオフ適用 |
| `MCP_HUB_MAX_CONCURRENT_CALLS` | `50` | 同時ツール呼び出しの最大数（DoS 対策セマフォ） |
| `MCP_HUB_CALL_TOOL_TIMEOUT` | `30` | ツール呼び出しのタイムアウト（秒） |
| `MCP_HUB_LIST_TOOLS_RETRY_DELAY` | `0.3` | `list_tools()` リトライ時の遅延（秒） |
| `MCP_HUB_CLIENT_TIMEOUT` | (未設定) | アップストリームへのリクエスト読み取りタイムアウト（秒）。未設定時は fastmcp/mcp SDK のデフォルト。応答しないサーバーが tools/list をブロックするのを防ぐには `2`〜`5` を推奨。WebUI の Hub 設定「⏱️ 接続タイムアウト」からも設定可能（保存値が env より優先） |
| `MCP_HUB_CONNECT_TIMEOUT` | `30.0` | 起動時の接続確認 `list_tools()` のタイムアウト（秒）。WebUI の Hub 設定「⏱️ 接続タイムアウト」からも設定可能（保存値が env より優先） |

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
