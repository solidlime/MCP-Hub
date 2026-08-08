# API リファレンス

MCP Hub は以下のインターフェースを提供します：

- **MCP Endpoint** (`/mcp`) — LLM クライアント向けの Streamable HTTP MCP エンドポイント
- **Admin REST API** (`/admin/api`) — サーバー管理・設定変更のための RESTful API
- **Admin Web UI** (`/admin/`) — ブラウザベースの管理インターフェース

---

## MCP Endpoint

### `POST /mcp`

Streamable HTTP トランスポートを使用した MCP プロトコルエンドポイント。
LLM クライアントはここに接続して全子サーバーのツール・リソース・プロンプトにアクセスします。

**動作モード：**

| モード | 説明 |
|---|---|
| 通常モード (`meta_mode=false`) | 全子サーバーの全ツール・リソース・プロンプトを直接公開 |
| Meta モード (`meta_mode=true`, デフォルト) | Progressive Discovery: `search_tools`、`execute_tool`、`list_upstream_tools` の 3 ツールのみ公開。ただし `full_info_tools` に指定したツールは通常ツールとしてフル公開される |

#### タグフィルタリング

`X-MCP-Hub-Tags` ヘッダーまたは `?tags=` クエリパラメーターでタグベースのフィルタリングが可能です。

```
POST /mcp
X-MCP-Hub-Tags: web,local
```

```
POST /mcp?tags=web,local
```

クエリパラメーターよりヘッダーが優先されます。タグフィルタリングは OR 論理で動作し、指定されたタグのいずれかを持つサーバーのツール・リソース・プロンプトのみが返ります。

#### 内部リソース: `hub://servents`

接続中の全サーバーの JSON スナップショットを返す内部 MCP リソースです。

```
resources/read hub://servers
```

戻り値：
```json
[
  {
    "name": "filesystem",
    "disabled": false,
    "tags": ["local"],
    "status": "connected",
    "tool_count": 12
  }
]
```

#### Progressive Discovery（meta_mode）

meta_mode が有効な場合、MCP エンドポイントは以下の 3 ツールのみを公開します：

| ツール | 説明 |
|---|---|
| `search_tools(query, top_k=10)` | BM25 + オプションの埋め込みベースセマンティック検索でツールを検索 |
| `execute_tool(server, tool_name, arguments)` | 検索で見つけたツールを実行 |
| `list_upstream_tools()` | 全アップストリームツールをサーバー別に一覧表示 |

#### フル公開ツール（`full_info_tools`）

meta_mode 有効時でも、`full_info_tools` に指定したツールは通常ツールとして `tools/list` にフル公開され、`tools/call` で直接呼び出すことができます（`execute_tool` を経由しません）。指定形式は `"{server}_{tool}"`（例: `"fetch_fetch"`）。

- フル公開ツールの呼び出しはタグフィルタリングの対象外（意図的仕様。`execute_tool` のタグ拒否もバイパスされます）
- ツール定義（description / inputSchema）は `ToolIndex` の検索インデックスから取得されるため、対象サーバーが接続中である必要があります
- 設定は管理 API の `PATCH /settings` または `hub.config.json` の `full_info_tools` で変更できます（Web UI のツール行 / サーバーカードの「フル公開」トグルからも操作可能）

---

## Admin REST API

ベースパス: `/admin/api`

### 認証

`MCP_HUB_API_KEY` 環境変数が設定されている場合、`X-API-Key` ヘッダーが必須になります。
`/admin/api/health` は認証対象外です。

```
X-API-Key: your-api-key-here
```

認証がない場合は `401` が返ります。

---

### ヘルスチェック

#### `GET /admin/api/health`

認証不要。サーバーの稼働状態を返します。

**Response:**
```json
{
  "status": "ok",
  "servers": 3
}
```

---

### メトリクス

#### `GET /admin/api/metrics`

**Response:**
```json
{
  "uptime_seconds": 3600.0,
  "servers_registered": 5,
  "servers_active": 3,
  "total_tools": 42,
  "tool_calls_total": 150,
  "tool_call_errors": 2
}
```

| フィールド | 説明 |
|---|---|
| `uptime_seconds` | 起動からの経過時間（秒） |
| `servers_registered` | 登録済みサーバー数（DB 上の全件） |
| `servers_active` | 現在接続中のサーバー数 |
| `total_tools` | 全サーバーのツール数の合計 |
| `tool_calls_total` | 累計ツール呼び出し回数 |
| `tool_call_errors` | 累計ツール呼び出しエラー回数 |

---

### ツールログ

#### `GET /admin/api/logs`

ツール呼び出しとサーバー接続イベントのログを取得します。ログはメモリ上のリングバッファ（最大 500 件）に保持され、再起動時に消去されます。

**Query Parameters:**

| パラメータ | 型 | 説明 |
|---|---|---|
| `type` | string | ログ種別でフィルタ（`tool_call` / `server_event`） |
| `server` | string | サーバー名でフィルタ |
| `status` | string | ステータスでフィルタ（`success` / `error` / `timeout` / `connected` / `disconnected` / `spawn_failed` / `recovered` / `removed` / `updated`） |
| `q` | string | サーバー名・ツール名・エラーメッセージに対する部分一致検索 |
| `limit` | int | 取得件数（デフォルト 100、最大 500） |

**Response:**
```json
{
  "entries": [
    {
      "id": 7,
      "ts": 1754320800.123,
      "type": "tool_call",
      "server": "fetch",
      "tool": "fetch_fetch",
      "status": "success",
      "duration_ms": 1234.5,
      "args": "{\"url\": \"https://example.com\", \"api_key\": \"***\"}",
      "error": null,
      "traceback": null
    }
  ],
  "total": 7
}
```

`args` と `error` には機密情報のマスキングが適用されます（`api_key` / `token` / `secret` / `password` / `auth` / `key` / `credential` を含むキー名、`sk-` トークン、`Bearer` ヘッダー、PEM 秘密鍵などは `***` に置換）。`args` は 500 文字、`error` は 500 文字、`traceback` は 4000 文字に切り詰められます。

---

### 設定

#### `GET /admin/api/settings`

**Response:**
```json
{
  "meta_mode": true,
  "full_info_tools": ["fetch_fetch"]
}
```

#### `PATCH /admin/api/settings`

meta_mode を切り替えたり、フル公開ツールを設定します。切り替え後、MCPDispatcher のキャッシュが自動的に無効化されます。

**Request Body:**
```json
{
  "meta_mode": false
}
```

`full_info_tools` を指定する場合、`list[str]` で全要素が `"{server}_{tool}"` 形式（`_` を含む）である必要があります。形式が不正な場合は `422 Unprocessable Entity` を返します。

**Request Body:**
```json
{
  "full_info_tools": ["fetch_fetch", "filesystem_read_file"]
}
```

**Response:**
```json
{
  "meta_mode": true,
  "full_info_tools": ["fetch_fetch", "filesystem_read_file"]
}
```

#### `GET /admin/api/settings/embedding-model`

**Response:**
```json
{
  "embedding_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
}
```

#### `PATCH /admin/api/settings/embedding-model`

埋め込みモデルを変更します。新しいモデルは次回の `rebuild_index()` から反映されます。

**Request Body:**
```json
{
  "embedding_model": "BAAI/bge-small-en-v1.5"
}
```

**Response:**
```json
{
  "embedding_model": "BAAI/bge-small-en-v1.5"
}
```

---

### サーバー管理

#### `GET /admin/api/servers`

サーバー一覧を取得します。

**Query Parameters:**

| パラメーター | 型 | デフォルト | 説明 |
|---|---|---|---|
| `include_tools` | boolean | `false` | `true` で各サーバーのツール一覧も含める。後方互換のためデフォルトは `false` だが、以前の動作では `true` 相当だった。高速な一覧取得には `false` を推奨。 |

**Response:**
```json
{
  "servers": [
    {
      "name": "filesystem",
      "config": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem"],
        "tags": ["local"]
      },
      "disabled": false,
      "status": "connected",
      "tools_count": 12,
      "tools": []
    }
  ]
}
```

#### `POST /admin/api/servers`

新しいサーバーを登録します。接続は非同期でバックグラウンド実行されます。

**Request Body:**
```json
{
  "name": "my-server",
  "config": {
    "command": "python",
    "args": ["-m", "my_mcp_server"],
    "env": {
      "API_KEY": "${MY_API_KEY}"
    },
    "tags": ["web", "api"],
    "headers": {},
    "disabled": false
  }
}
```

`config` のバリデーションルール：

| フィールド | ルール |
|---|---|
| `command` | 空でない文字列。`$()`（サブシェル）、`;`、`&`、`|`、`` ` ``、`<`、`>` 禁止。`${VAR}` テンプレートは許可。最大 512 文字。 |
| `url` | `http://` または `https://` のみ。最大 2048 文字。 |
| `args` | 最大 50 要素。各要素最大 1024 文字。 |
| `env` | キー最大 256 文字、値最大 4096 文字。`PATH`、`LD_PRELOAD` 等の危険変数はブロック。`command` / `url` どちらのサーバーにも指定可。`url` サーバーでは `TOKEN` / `API_KEY` / `SECRET` / `PASSWORD` / `AUTH` を含む変数が 1 つだけの場合 `Authorization: Bearer` ヘッダーに自動変換。 |
| `tags` | 各タグ最大 64 文字の文字列。 |
| `headers` | キー最大 256 文字、値最大 8192 文字。制御文字禁止。 |
| `disabled` | ブール値。`true` で登録のみ行い接続しない。 |

**Status Code:** `201 Created`

**Response:**
```json
{
  "name": "my-server",
  "config": { ... },
  "status": "connecting"
}
```

**エラーレスポンス:**

| Status | 条件 |
|---|---|
| `409 Conflict` | 同名サーバーが既に存在する |
| `422 Unprocessable Entity` | バリデーションエラー |
| `400 Bad Request` | その他のエラー |

#### `GET /admin/api/servers/{name}/connection`

クライアントが接続するための接続情報を返します。

**Response:**
```json
{
  "url": "http://localhost:26263/mcp?tags=web,local",
  "tags": ["web", "local"],
  "example_header": "X-MCP-Hub-Tags: web,local"
}
```

#### `PATCH /admin/api/servers/{name}`

サーバー設定を部分更新します。送信されたフィールドのみ既存設定にマージされます。
更新後、プロキシの再生成と再マウントが行われます。

**Request Body:** `ServerConfig` の部分適用（`POST /servers` の `config` と同構造）

```json
{
  "tags": ["new-tag"],
  "disabled": true
}
```

**Response:**
```json
{
  "name": "my-server",
  "config": { ... }
}
```

#### `DELETE /admin/api/servers/{name}`

サーバーを削除しアンマウントします。

**Status Code:** `204 No Content`

#### `POST /admin/api/servers/{name}/test`

サーバーの接続テストを実行します。

**Response（成功時）:**
```json
{
  "success": true,
  "tools_count": 12,
  "tools": [
    {"name": "read_file", "description": "Read a file's contents"},
    ...
  ]
}
```

**Response（失敗時）:**
```json
{
  "success": false,
  "tools_count": 0,
  "tools": [],
  "error": "Connection refused"
}
```

#### `GET /admin/api/servers/{name}/resources`

接続済みサーバーの MCP リソース一覧を取得します。

**Response:**
```json
{
  "resources": [
    {
      "uri": "file:///path",
      "name": "files",
      "description": "File system resources"
    }
  ]
}
```

#### `GET /admin/api/servers/{name}/prompts`

接続済みサーバーの MCP プロンプト一覧を取得します。

**Response:**
```json
{
  "prompts": [
    {
      "name": "analyze",
      "description": "Analyze code"
    }
  ]
}
```

#### `GET /admin/api/servers/{name}/resource-templates`

接続済みサーバーのリソーステンプレート一覧を取得します。

**Response:**
```json
{
  "resource_templates": [
    {
      "uriTemplate": "file:///{path}",
      "name": "file",
      "description": "Access any file"
    }
  ]
}
```

---

### ツール操作

#### `POST /tools/install`

依存パッケージのインストールコマンドを実行します（pip, npm, uv 等）。
インストールされたパッケージは Docker ボリュームに永続化されます。

**Request Body:**
```json
{
  "command": "pip install yt-dlp"
}
```

pip / uv pip の install コマンドは自動的に `--target /home/mcp-hub/pip-extras` が付加され、永続化ディレクトリにインストールされます。

**Response:**
```json
{
  "success": true,
  "returncode": 0,
  "stdout": "...",
  "stderr": ""
}
```

**タイムアウト:** 120 秒

#### `POST /admin/api/servers/{name}/tools/{tool_name}/call`

特定のサーバーのツールを直接呼び出します。

**Request Body:**
```json
{
  "arguments": {
    "path": "/home",
    "recursive": true
  }
}
```

**Response:**
```json
{
  "result": { ... }
}
```

---

## Admin Web UI

### `GET /admin/`

ブラウザベースの管理インターフェース。サーバーの一覧表示、追加、編集、削除、タグ管理が行えます。

### 静的アセット

`GET /admin/static/*` — CSS、JS、その他フロントエンドアセット。
