# API リファレンス

MCP Hub は以下のインターフェースを提供します：

- **MCP Endpoint** (`/mcp`) — LLM クライアント向けの Streamable HTTP MCP エンドポイント
- **Admin REST API** (`/admin/api`) — サーバー管理・設定変更のための RESTful API
- **Admin Web UI** (`/admin/`) — ブラウザベースの管理インターフェース
- **Metrics** (`/metrics`) — Prometheus テキスト形式のメトリクス（認証なし）

---

## MCP Endpoint

### `POST /mcp`

Streamable HTTP トランスポートを使用した MCP プロトコルエンドポイント。
LLM クライアントはここに接続して全子サーバーのツール・リソース・プロンプトにアクセスします。

**動作モード：**

| モード | 説明 |
|---|---|
| 通常モード (`meta_mode=false`) | 全子サーバーの全ツール・リソース・プロンプトを直接公開 |
| Meta モード (`meta_mode=true`, デフォルト) | Progressive Discovery: `search_tools`、`search_resources`、`execute_tool`、`get_schema` の 4 ツールのみ公開。ただし `full_info_tools` に指定したツールは通常ツールとしてフル公開される |

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

#### 内部リソース: `hub://servers`

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

meta_mode が有効な場合、MCP エンドポイントは以下の 4 ツールのみを公開します：

| ツール | 説明 |
|---|---|
| `search_tools(query, top_k=3, detail="brief", server=None)` | BM25 + オプションの埋め込みベースセマンティック検索で**ツールのみ**を検索。`top_k` は最大 50 にクランプされる（巨大な値を渡しても返り値が肥大しない）。`detail` は `"brief"`（既定、inputSchema なし）/ `"schema"`（上位 1 件のみ圧縮 inputSchema）/ `"all"`（全件に圧縮 inputSchema、従来互換）。`server` を渡すと結果をその 1 サーバーに絞る（大小文字は無視）。未接続（インデックス不在）のサーバーなら `Tool not found`、接続済みだがタグフィルタ範囲外なら `Tool not available`（権限なし。実タグ一覧は返さない）を返し、存在の有無を漏らさない |
| `search_resources(query, top_k=3, kind=None, server=None)` | **リソース / リソーステンプレート / プロンプト**を同じ索引（`ToolIndex` を `kind` で分離）から検索。`kind` は `resource` / `resource_template` / `prompt`（複数形・ハイフン形も可）。結果には `uri`（テンプレートは `uri_template`）と `arguments`（テンプレートは JSON Schema `properties` から、プロンプトは `arguments` から正規化）が付く。実体の読み取りは従来どおり `resources/read` / `prompts/get` で行う |
| `execute_tool(server, tool_name, arguments)` | 検索で見つけたツールを実行。互換のため `{"arguments": {...}}` に `server` / `tool_name` / `arguments` を折り畳んだ形式（LLM が生成しがちなフラット呼び出し）も受け付ける。サーバー名の大文字小文字は case-insensitive に解決される。タグフィルタ範囲外のサーバーは `Tool not available`（権限なし）で拒否される |
| `get_schema(server, tool_name, compact=False)` | `search_tools` の `inputSchema` は既定で省かれるので、実行前に仕様が必要な時に呼ぶ。既定 (`compact=False`) は切り詰めなしの `description` と完全な `inputSchema`。`compact=True` は `search_tools` と同じ圧縮 `inputSchema`（名前 / 型 / 必須）を返す。サーバー名は case-insensitive に解決される |

`search_tools` の description には接続中のサーバー一覧（カタログ）が含まれます。`X-MCP-Hub-Tags` ヘッダによるタグフィルタが有効な場合、このカタログは**可視のサーバーのみ**に差し替えられます（タグ範囲外のサーバーは検索結果と同様、カタログからも見えません）。

#### `search_resources` のレスポンス形式

```json
{
  "results": [
    {
      "server": "notes",
      "name": "note://{id}",
      "description": "ノート本文",
      "kind": "resource_template",
      "uri": "note://{id}",
      "arguments": [{ "name": "id", "description": "note id", "required": true }],
      "score": 2.1
    }
  ],
  "kinds": ["tool", "resource", "resource_template", "prompt"]
}
```

- 索引はツールと同じくサーバー接続時（`rebuild_index`）に作られ、`X-MCP-Hub-Tags` のタグフィルタはツールと同一に適用される（範囲外サーバーのリソース/プロンプトは結果に出ない）。
- リソース/プロンプトの一覧取得に失敗する子サーバー（未実装を含む）でもツール索引は壊れない。失敗はログ警告のみで、そのサーバーを「失敗」扱いにはしない。
- `search_tools` はツールだけを返す（リソース/プロンプトは混ざらない）。

##### `search_tools` のレスポンス形式

```json
{
  "results": [
    {
      "server": "filesystem",
      "name": "read_file",
      "description": "Read a file's contents",
      "search_desc": "ファイルを読む。",
      "score": 1.2345
    }
  ],
  "servers": { "filesystem": "ローカルファイル操作" },
  "note": "スキーマが必要な場合は get_schema(server, tool_name) を呼んでください（compact=True で圧縮版）。"
}
```

- `results[].description` は**表示用のツール説明**（サーバー説明の前置なし）。`_DISPLAY_DESC_CHARS = 600` 字で切り詰め、切った時だけ末尾に `"…"` を付ける。
- `results[].search_desc` は**ツール単位の日本語 1 文**（`tool_search_desc`。admin UI / `PATCH /servers/{name}` で投入）。未設定なら空文字。英語 docstring の語彙の壁を越える検索補助と同時に、結果の一行要約として返す。
- **既定 (`detail="brief"`) の `results[]` は `inputSchema` を含まない**（フィールドは `server` / `name` / `description` / `search_desc` / `score`）。`tags` はフィルタ入力であり（サーバー側で適用済み）、結果には含めない。スキーマが要る時は `detail="schema"`（上位 1 件のみ）か `get_schema(server, tool_name)` を呼ぶ。
- `detail="schema"` は**上位 1 件のみ**に圧縮 `inputSchema` を付け、残りは brief。`detail="all"` は全件に圧縮 `inputSchema` を付け、従来と同じ応答（同じ `note` を含む）を返す。
- `results[].inputSchema`（`schema` / `all` 時）は**圧縮形**。`type` / `required` と各パラメータの `type`・短い `enum`（5 個以下かつ連結 80 字以下）・短い `default`（数値・真偽、または文字列化 40 字以下）のみで、`description` と深いネストは落とす。完全な仕様は `get_schema(server, tool_name)`、`get_schema(server, tool_name, compact=True)` で同じ圧縮形を取得できる。
- `note` はスキーマ取得方法の案内。**成功時のみ**付き、0 件やタグ全滅の早期 return では付かない。`detail="all"` だけは従来互換の文言を保つ。
- `servers` は**結果に現れたサーバーのみ**を対象にした `{server_name: server_description}` マップ（未ヒットのサーバーは含まない）。表示 `description` からサーバー前置を外した代わりに、LLM がサーバー文脈を得る経路。
- 検索（BM25 トークン・埋め込み）が使う**索引テキスト**は別物で、`サーバー説明 + tool_search_desc（ツール単位の日本語 1 文）+ ツール説明（_INDEX_DESC_CHARS = 400 字上限）` を前置付きで持つ（結果には出さない）。

`search_tools` が 0 件を返す場合、`{"message": "No matching tools found", "hint": ...}` を返します。`X-MCP-Hub-Tags` によるタグフィルタで全件除外された場合は、`hint` に「タグフィルタ ... により全件除外された可能性」を含めます（0 件ヒットの主因。additive で既存キーは不変）。

##### `get_schema` のレスポンス形式

`search_tools` は既定で `inputSchema` を省くので、実行前に仕様が必要な時に呼びます。`server` / `tool_name` は `search_tools` の結果の値を渡します（`server` は case-insensitive に解決されます）。`compact=True` を渡すと `search_tools` と同じ**圧縮** `inputSchema`（名前 / 型 / 必須）を返します。

```json
{
  "name": "read_file",
  "description": "Read a file's contents (full, untruncated)",
  "server": "filesystem",
  "inputSchema": {
    "type": "object",
    "required": ["path"],
    "properties": { "path": { "type": "string", "description": "Path to read" } }
  }
}
```

- `description` は切り詰めなしの**フル説明**（`full_description`。無ければ `description`）。
- `inputSchema` は上流ツールの**完全な** JSON Schema。`compact=True` の場合は `search_tools` と同じ圧縮形（`description` と深いネストを落とす）。
- 見つからない場合（ツール不在、`server` / `tool_name` が空）は `{"message": "Tool not found", "hint": "search_tools で server / tool_name を確認してください。"}` を返します。
- タグフィルタ範囲外のサーバーは権限なしとして `{"message": "Tool not available", "hint": "このサーバーは現在のタグフィルタでは利用できません。X-MCP-Hub-Tags ヘッダーを確認するか、タグなしで接続してください。"}` を返します（`search_tools` / `execute_tool` も同一応答。実タグ一覧は返しません）。

#### フル公開ツール（`full_info_tools`）

meta_mode 有効時でも、`full_info_tools` に指定したツールは通常ツールとして `tools/list` にフル公開され、`tools/call` で直接呼び出すことができます（`execute_tool` を経由しません）。指定形式は `"{server}_{tool}"`（例: `"fetch_fetch"`）。

- フル公開ツールの呼び出しはタグフィルタリングの対象外（意図的仕様。`execute_tool` のタグ拒否もバイパスされます）
- ツール定義（description / inputSchema）は `ToolIndex` の検索インデックスから取得されるため、対象サーバーが接続中である必要があります。description は表示用の切り詰めをしない**フル説明**（`get_schema` が返す `full_description`）です
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
  "servers": 3,
  "embedding_status": "active"
}
```

`embedding_status` は埋め込み検索の**実効状態**（設定の意図値でなく真実）を返します: `active` / `building` / `inactive:no-fastembed` / `inactive:setting` / `inactive:no-documents` / `error:<短い要約>`。embed 失敗で BM25 に恒久降格した場合は `error:*` になります（additive フィールド、既存キーは不変）。

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
| `tool_cache` | ツール結果キャッシュの統計（`hits` / `misses` / `entries` など。機能無効時は 0） |

---

#### `GET /metrics`（Prometheus テキスト形式）

スクレイパ向けのメトリクス。`/admin/api` の外にあり **API キー認証の対象外**（可視性は `/mcp` と同じで bind ホスト依存）。`include_in_schema=False` のため OpenAPI には出ません。

**Content-Type:** `text/plain; version=0.0.4; charset=utf-8`

```
# TYPE mcp_hub_tool_calls_total counter
mcp_hub_tool_calls_total{server="filesystem",status="success",tool="filesystem_read"} 12
# TYPE mcp_hub_tool_call_duration_seconds histogram
mcp_hub_tool_call_duration_seconds_bucket{le="0.1"} 9
mcp_hub_tool_call_duration_seconds_bucket{le="+Inf"} 12
mcp_hub_tool_call_duration_seconds_sum 0.834
mcp_hub_tool_call_duration_seconds_count 12
```

| メトリクス | 型 | ラベル | 説明 |
|---|---|---|---|
| `mcp_hub_tool_calls_total` | counter | `server`, `tool`, `status` | ツール呼び出し回数（`success` / `error`） |
| `mcp_hub_tool_call_duration_seconds` | histogram | — | ツール呼び出しレイテンシ |
| `mcp_hub_search_queries_total` | counter | — | `search_tools` の実行回数 |
| `mcp_hub_search_hits_total` / `mcp_hub_search_misses_total` | counter | — | 結果あり／なしの検索回数 |
| `mcp_hub_index_rebuilds_total` | counter | — | 検索インデックス再構築の回数 |
| `mcp_hub_index_rebuild_failures_total` | counter | — | ツール取得に失敗したサーバーを含む再構築の回数 |
| `mcp_hub_index_build_duration_seconds` | histogram | — | 索引再構築の所要時間 |
| `mcp_hub_embedding_cache_hits_total` / `_misses_total` | counter | — | 埋め込みディスクキャッシュの再利用／再計算 |
| `mcp_hub_health_check_failures_total` | counter | `server`, `reason` | ヘルスチェック失敗 |
| `mcp_hub_reconnects_total` | counter | — | 切断サーバーの復旧回数 |
| `mcp_hub_uptime_seconds` | gauge | — | プロセス起動からの経過秒 |
| `mcp_hub_servers_registered` / `mcp_hub_servers_active` | gauge | — | 登録サーバー数／接続中サーバー数 |
| `mcp_hub_tools_total` / `mcp_hub_tools_indexed` | gauge | — | キャッシュ済みツール数／索引済みツール数 |
| `mcp_hub_server_up` | gauge | `server` | 接続中なら 1、それ以外は 0 |

`/admin/api/metrics` の JSON は従来どおり全フィールド互換のまま残しています（`/metrics` はその上位互換ではなく、ラベル付き系列を含む別ビューです）。

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
  "full_info_tools": ["fetch_fetch"],
  "client_timeout": null,
  "connect_timeout": null,
  "use_embeddings": true,
  "embedding_status": "active",
  "embedding_rebuild": {
    "status": "idle",
    "in_progress": false,
    "trigger": null,
    "requested_model": null,
    "active_model": "cl-nagoya/ruri-v3-30m",
    "documents": 42,
    "embedding_status": "active",
    "error": null,
    "started_at": null,
    "finished_at": null
  },
  "llm": {
    "provider": null,
    "model": null,
    "base_url": null,
    "api_key_set": false
  }
}
```

`llm`（additive）は LLM 生成機能の設定状態。**`api_key` の値は返さず**、設定有無のみを `api_key_set` で公開する。

`embedding_rebuild`（additive）は埋め込み設定変更に伴う索引再構築の状態。`status` は `idle` / `running` / `success` / `error`、`trigger` は `embedding_model` / `use_embeddings`。`requested_model`（保存済み設定値）と `active_model`（索引に適用済みのモデル）を分けて返すので、UI は両者の差で「再構築中 / 未反映」を判定できる。`documents` は索引済み文書数、`error` は最後の失敗理由、`started_at` / `finished_at` は epoch 秒。

`embedding_status`（実効状態。取り得る値は `GET /admin/api/health` と同じ）は additive フィールドです。`use_embeddings` は設定意図ではなく実効値のため、embed 失敗により降格している場合は `use_embeddings=false` かつ `embedding_status="error:*"` になります。

`client_timeout` / `connect_timeout` は未設定時 `null` を返します（実装: `admin_router.py:128-138`）。設定値は `proxy_manager.py` で `MCP_HUB_CLIENT_TIMEOUT`（既定 `180.0`）/ `MCP_HUB_CONNECT_TIMEOUT`（既定 `30.0`）より優先されます。

`tool_cache`（additive）はツール結果キャッシュの正規化済み設定と実効統計:

```json
{
  "enabled": false,
  "ttl": 60.0,
  "max_entries": 500,
  "max_bytes": 5000000,
  "allow": [],
  "deny": [],
  "stats": {"enabled": false, "hits": 0, "misses": 0, "evictions": 0, "entries": 0, "bytes": 0}
}
```

#### `PATCH /admin/api/settings`

meta_mode を切り替えたり、フル公開ツールを設定します。切り替え後、MCPDispatcher のキャッシュが自動的に無効化されます。`use_embeddings` は bool で指定し、変更時は検索インデックスが再構築されます（非 bool は `422`）。`MCP_HUB_EMBEDDING=0` によるハードキルが有効な場合、実効値は常に `false` になります。

**Request Body:**
```json
{
  "meta_mode": false
}
```

タイムアウトを変更する場合（`0 < 値 <= 300` の数値または `null` でクリア、不正値は `422`。実装: `admin_router.py:174-182`）：

**Request Body:**
```json
{
  "client_timeout": 180.0,
  "connect_timeout": 30.0
}
```

`full_info_tools` を指定する場合、`list[str]` で全要素が `"{server}_{tool}"` 形式（`_` を含む）である必要があります。形式が不正な場合は `422 Unprocessable Entity` を返します。

**Request Body:**
```json
{
  "full_info_tools": ["fetch_fetch", "filesystem_read_file"]
}
```

`llm` で LLM 生成機能（`POST /admin/api/llm/generate`）を設定します。object でない場合は `422`。**部分マージ**（既存設定にマージするので、`api_key` を毎回送る必要はない）。空 object `{}` を送ると全削除＝機能オフ（`{"llm": {"provider": null}}` のように `null` を送ると値が `null` のまま保存されるので、オフにしたいときは `{}` を使う）。

**Request Body:**
```json
{
  "llm": {
    "provider": "openai",
    "model": "gpt-4o-mini",
    "api_key": "sk-...",
    "base_url": "https://api.example.com/v1"
  }
}
```

**Response:**
```json
{
  "meta_mode": true,
  "full_info_tools": ["fetch_fetch", "filesystem_read_file"],
  "client_timeout": 180.0,
  "connect_timeout": 30.0,
  "use_embeddings": true,
  "embedding_status": "active",
  "llm": {
    "provider": "openai",
    "model": "gpt-4o-mini",
    "base_url": "https://api.example.com/v1",
    "api_key_set": true
  }
}
```

`tool_cache` で結果キャッシュの設定を**部分マージ**で更新します。値の変更（有効化・TTL・allow/deny・上限）は既存エントリの意味を変えるため全エントリを破棄します。不正値は `422`（`enabled` は bool、`ttl` は `0 < ttl <= 86400`、`max_entries` は 1 以上、`max_bytes` は 0 以上、`allow` / `deny` は空でない文字列のリスト）。

**Request Body:**
```json
{
  "tool_cache": {"enabled": true, "ttl": 30, "allow": ["fetch_*"]}
}
```

#### `DELETE /admin/api/cache`

ツール結果キャッシュの全エントリを破棄します（設定とヒット/ミス統計は保持）。

**Response:**
```json
{"cleared": 12, "stats": {"enabled": true, "hits": 34, "misses": 40, "evictions": 0, "entries": 0, "bytes": 0}}
```

キャッシュは成功結果のみ保持し、例外・`is_error`・meta モード `execute_tool` の JSON `error` 応答は保存しません。ヒット時はツールログに `status="cache_hit"` が記録され、`/admin/api/metrics` の `mcp_hub_tool_calls_total{status="cache_hit"}` と `tool_cache` 統計で可視化されます。サーバーの再接続/設定変更/削除時は該当サーバーのエントリを自動破棄します。

#### `GET /admin/api/settings/embedding-model`

**Response:**
```json
{
  "embedding_model": "cl-nagoya/ruri-v3-30m"
}
```

#### `PATCH /admin/api/settings/embedding-model`

埋め込みモデルを保存し、**バックグラウンドで索引を再構築します（再起動待ちなし）**。応答は受け付け状態のみを返し、進捗・成功/失敗は `GET /admin/api/settings` の `embedding_rebuild` で追跡します（`PATCH /admin/api/settings` の `use_embeddings` 変更も同じ経路で再構築します）。任意の fastembed 対応モデル名、または ONNX Runtime 経路の登録モデル名（`cl-nagoya/ruri-v3-30m`）を指定できます。プレフィックスはモデルプロファイルから自動付与されます（`cl-nagoya/ruri-v3-30m` = `検索クエリ: `/`検索文書: `、`intfloat/` 配下で名前に `e5` を含むモデル = `query: `/`passage: `）。

モデル切替時は旧モデルのベクトルと embedder を破棄してから再構築するため、モデル混在による誤ヒットは起きません。**再構築中は埋め込みが無いので検索は BM25 のみで継続します**（`embedding_status` は `building` ではなく `inactive:*` / `error:*` を返し、`embedding_rebuild.status="running"` と併せて実効状態が分かります）。再構築中の新規変更は前のタスクをキャンセルして置き換えます（設定の最終値だけが意味を持ちます）。

**Request Body:**
```json
{
  "embedding_model": "cl-nagoya/ruri-v3-30m"
}
```

**Response:**
```json
{
  "embedding_model": "cl-nagoya/ruri-v3-30m",
  "embedding_status": "inactive:setting",
  "rebuild": {
    "status": "running",
    "in_progress": true,
    "trigger": "embedding_model",
    "requested_model": "cl-nagoya/ruri-v3-30m",
    "active_model": "cl-nagoya/ruri-v3-30m",
    "documents": 0,
    "embedding_status": "inactive:setting",
    "error": null,
    "started_at": 1750000000.0,
    "finished_at": null
  }
}
```

`rebuild`（additive）は `GET /admin/api/settings` の `embedding_rebuild` と同形です。`embedding_model` が無い場合は 400、空文字・パス/URL・制御文字・256 文字超は 422 を返します。

---

#### `POST /admin/api/reload`

設定ファイル（`{MCP_HUB_DATA_DIR}/hub.config.json` = 管理 API が保存するストア）を再読込し、差分を**再起動なしで**適用します。

適用順:
1. **サーバー差分** — 追加はバックグラウンド接続、削除はアンマウント + upstream 切断、設定変更はプロキシ再生成（`ProxyManager.apply_server_configs`）。
2. **ランタイム設定** — `use_embeddings` / `embedding_model`（変更時のみ索引再構築）、`log_level`、`meta_mode`（dispatcher キャッシュ破棄）、ツール結果キャッシュ設定。`full_info_tools` / `llm` / タグはリクエスト毎にストアから読むため再適用不要。
3. **検索インデックス再構築** — 埋め込み設定が変わった時だけ実行（失敗しても BM25 で検索は継続し `index_rebuilt: false`）。

**Response:**
```json
{
  "ok": true,
  "servers": {
    "added": ["new-server"],
    "removed": ["old-server"],
    "updated": ["changed-server"],
    "unchanged": ["keep-server"]
  },
  "settings": {
    "meta_mode": true,
    "use_embeddings": true,
    "embedding_model": "cl-nagoya/ruri-v3-30m",
    "embedding_model_changed": false,
    "index_rebuilt": false
  }
}
```

**Errors:**

| ステータス | 条件 |
|---|---|
| `500` | 設定ファイルが不正（JSON 壊れ / `mcpServers` が dict でない / サーバーエントリが不正）。**適用前に弾くためランタイムは変更前のまま。** |
| `401` | `MCP_HUB_API_KEY` 設定時にキー不一致 |

再接続に失敗したサーバーは `status: "error"` になるだけで、他のサーバーは稼働を続けます（部分適用を `ok` で返す）。

**環境変数 `MCP_HUB_WATCH_CONFIG=<秒>`** を設定すると、設定ファイルの mtime を監視して同じ処理を自動実行します（未設定/`0`＝無効、既定は無効）。監視ループは不正設定を警告ログにして継続し、次回変更時に追いつきます。

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
      "effective_state": "healthy",
      "tools_count": 12,
      "tool_count": 12,
      "resource_count": 4,
      "prompt_count": 2,
      "last_success_at": 1710000000.0,
      "last_error": null,
      "consecutive_failures": 0,
      "retry_backoff_seconds": 300.0,
      "retry_in_seconds": null,
      "next_retry_at": null,
      "health_history": [{"at": 1710000000.0, "event": "ok", "detail": "12 tools"}],
      "tools": []
    }
  ]
}
```

接続状態フィールド（WebUI と同一定義）:

| フィールド | 説明 |
|---|---|
| `effective_state` | 実効状態。`healthy` / `initializing` / `recovering` / `error` / `disabled` / `unknown`。直近の失敗 (`status="error"`) は `disabled` より優先して表示する。 |
| `consecutive_failures` | 直近の連続失敗数（成功でリセット）。 |
| `last_success_at` / `last_error` | 最終成功時刻（epoch 秒）と直近エラー `{reason, message, at}`。 |
| `retry_backoff_seconds` / `retry_in_seconds` / `next_retry_at` | 復旧クールダウン（`MCP_HUB_RECOVERY_COOLDOWN`）と、`error` 時の次回リトライまでの残り秒数・時刻。 |
| `tool_count` / `resource_count` / `prompt_count` | 既知の件数（リソース/プロンプトは `/resources` `/prompts` 取得時に更新）。 |
| `health_history` | 直近 20 件の接続イベント（`ok` / `failure` / `connected` など）。 |

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
    "disabled": false,
    "description": "Search the web and fetch pages"
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
| `description` | Meta モードで `search_tools` の description に焼き込まれる一行説明。最大 500 文字。省略時はツール名の一覧にフォールバック。 |

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
更新後、プロキシの再生成と再マウントが行われます（`tags` / `description` /
`tool_search_desc` **のみ**の更新は例外で、プロキシ再生成を行わず設定のみ更新する）。

**Request Body:** `ServerConfig` の部分適用（`POST /servers` の `config` と同構造）

```json
{
  "tags": ["new-tag"],
  "disabled": true
}
```

`tool_search_desc` は `{ツール名: 日本語1文}` の object（それ以外は `422`）。索引テキストに
前置され、**保存と同時に**インデックスが再構築されて検索へ反映される（プロキシの再生成は不要）。

```json
{
  "tool_search_desc": {
    "ha_search": "照明や電球を操作し室温を確認する"
  }
}
```

**Response:**
```json
{
  "name": "my-server",
  "config": { ... }
}
```

`limits` フィールドはサーバー単位の呼び出し制限（`call_timeout` / `max_concurrent` /
`rate_limit` / `rate_burst` / `rate_window`）。`null` を送るとキーが削除されます。
`limits` **のみ**の更新もプロキシ再生成を行わず（制限は呼び出し毎に参照）、既存の
同時実行スロット・レート状態だけがリセットされます。

```json
{
  "limits": { "call_timeout": 120, "max_concurrent": 2, "rate_limit": 10, "rate_window": 60 }
}
```

#### `PATCH /admin/api/servers/{name}/limits`

サーバー単位の呼び出し制限を部分更新します（マージ）式。`null` で個別キーを削除し、
結果が空になったら config から `limits` キーごと削除（＝全項目グローバル既定）。

**Request Body:** 上記 `limits` オブジェクトの部分適用（未知キー・範囲外は `422`）

```json
{ "rate_limit": 5, "rate_window": 30 }
```
**Response:**
```json
{ "name": "my-server", "limits": { "rate_limit": 5, "rate_window": 30 } }
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

#### `POST /admin/api/tools/install`

依存パッケージのインストールコマンドを実行します（pip, uv, uv-tool, npm）。
インストールされたパッケージは Docker ボリュームに永続化されます。
ベースパス `/admin/api` 配下（実装: `admin_router.py` の `APIRouter(prefix="/admin/api")` + `@router.post("/tools/install")`、UI `index.html` の `fetch('/admin/api/tools/install')` が正）。

**Request Body:**
```json
{
  "manager": "uv",
  "packages": ["yt-dlp"],
  "constraints": ["mcp<2"]
}
```

- `manager`: `pip` / `uv` / `uv-tool` / `npm` のいずれか。
- `packages`: パッケージ指定のリスト。PyPI パッケージ名のほか、PEP 508 互換の URL 指定（`git+https://...`、`https://` の sdist/wheel、`name[extra]==ver`）を pip/uv/uv-tool で許可。空文字・先頭 `-`・ホワイトスペース・制御文字は 400。
- `constraints`（省略可）: 依存ピンのリスト（例: `"mcp<2"`）。uv-tool は各項が `--with <spec>` として渡され、pip/uv は追加要件として argv 末尾に連結される。npm は未対応（400）。

pip / uv の install コマンドは自動的に `--target`（pip-extras 永続化ディレクトリ）が付加され、永続化ディレクトリにインストールされます。`uv-tool` は `uv tool install` / `npm` は `npm install` として実行されます。コマンドは固定 argv でシェルなし実行されます。

**Response:**
```json
{
  "success": true,
  "returncode": 0,
  "stdout": "...",
  "stderr": ""
}
```

**タイムアウト:** 300 秒（install/uninstall 共通、セマフォで直列化）

#### `POST /admin/api/tools/uninstall`

依存パッケージをアンインストールします。

**Request Body:**
```json
{
  "manager": "uv-tool",
  "packages": ["j-quants-doc-mcp"]
}
```

- `uv-tool` / `npm`: 各マネージャーの uninstall コマンドを実行。
- `pip` / `uv`: `--target` ディレクトリから該当パッケージのディレクトリを削除（前方一致、トラバーサル防御あり）。依存の連鎖は削除されません。
- `constraints` は非対応（400）。

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

### LLM 生成

#### `POST /admin/api/llm/generate`

OpenAI 互換 API（`{base_url}/chat/completions`、`Authorization: Bearer {api_key}`）で
日本語の説明文（1文・80字以内を指示）を生成します。プロンプトはサーバー側に置く。

**注意:** `base_url` は管理者が指定した URL へそのままリクエストを送ります（`{base_url}/chat/completions` に `Authorization: Bearer {api_key}` を付けて POST。SSRF 耐性は admin 認証が前提）。プロンプトには対象のサーバー名・ツール名・docstring / サーバー説明が材料として補間されます。

**Request Body:**
```json
{
  "kind": "server_description",
  "server": "my-server"
}
```

| フィールド | 必須 | 説明 |
|---|---|---|
| `kind` | 必須 | `"server_description"` / `"tool_search_desc"` / `"server_bundle"`（それ以外は `422`）。 |
| `server` | 必須 | 接続済みサーバー名（未接続は `404`）。 |
| `tool_name` | `tool_search_desc` のとき必須 | 対象ツール名（無いと `400`、見つからないと `404`）。 |
| `tools` | `server_bundle` のとき任意 | 生成対象のツール名の配列。**最大 30 件**（31 件以上は `400`）。空配列 `[]` も可（サーバー説明だけを生成）。 |

**Response（`server_description` / `tool_search_desc`）:**
```json
{
  "text": "照明や電球を操作し室温を確認する"
}
```

**Response（`server_bundle`）:** 既存 2 kind とは形が異なる（`text` ではなく `description` + `tool_search_desc`）:
```json
{
  "description": "照明や電球を操作し室温を確認する",
  "tool_search_desc": {
    "turn_on": "指定した照明を点灯する",
    "get_temperature": "現在の室温を取得する"
  }
}
```

`server_bundle` はサーバー説明と、`tools` で指定した各ツールの検索用の説明を **1 回の LLM 呼び出し**でまとめて返します（管理 UI の ✨ サーバー説明生成と一括生成が使用。`tools` を 30 件ずつに分割して呼ぶ）。プロンプトには指定ツールの docstring だけを材料にし、`tools` に無いツール名・非文字列・空白のみの値は応答から黙って落とします。出力が大きいためこの kind だけタイムアウトを **60 秒**にしています（既存 2 kind は 30 秒）。

**Errors:**

| Status | 条件 |
|---|---|
| `400` | `llm.api_key` 未設定（`PATCH /admin/api/settings` で設定）。または `tool_search_desc` で `tool_name` なし。または `server_bundle` で `tools` が 30 件超。 |
| `404` | `server` が未接続、または `tool_name` が当該サーバーに見つからない。 |
| `422` | `kind` が不正。 |
| `502` | 上流 LLM への接続失敗、上流 HTTP エラー、応答の解析失敗（`server_bundle` の JSON 解析失敗を含む）、空応答。 |

---

### 検索・発見

#### `GET /admin/api/discovery/search`

メタ経路と同じ索引（ツール / リソース / リソーステンプレート / プロンプト）を横断検索します。管理 UI の「検索」タブが使用します。

| パラメータ | 型 | 既定 | 説明 |
|---|---|---|---|
| `query` | string | 必須 | 検索語 |
| `top_k` | int | 10 | 最大件数（50 にクランプ） |
| `kind` | string | なし | `tool` / `resource` / `resource_template` / `prompt`。複数形・ハイフン形も可。不正値は 400 |
| `server` | string | なし | サーバー名で絞り込み（大小文字無視） |

**Response:**
```json
{
  "query": "note file",
  "kinds": ["tool", "resource", "resource_template", "prompt"],
  "results": [
    { "server": "filesystem", "name": "read_file", "description": "...", "kind": "tool", "score": 3.2, "tags": ["local"] }
  ]
}
```

- 管理 API は認証済みの全権限ビューなので**タグフィルタは適用しない**（`tags` は結果に含める）。
- メタ経路の応答形状（ツールヒットに `kind` を付けない）は既存契約のまま。`kind` を付けるのはこの管理 API のみ。
- 索引未構築（`meta_app` 不在）では `{"results": [], "kinds": [...]}` を返す。

---

## Admin Web UI

### `GET /admin/`

ブラウザベースの管理インターフェース。左レールにサーバー一覧（名前・説明・タグでの絞り込み、「未設定のみ」トグル、件数表示）、右ペインに選択中サーバーの詳細（説明・ツール・ログ・接続情報）を表示する master-detail 構成です。

- サーバーの追加・編集・削除・タグ管理・有効/無効トグル・接続テスト
- **サーバー説明**と**ツールの検索用の説明**（`tool_search_desc`）は**自動保存**（入力停止 0.8 秒後に 1 回の `PATCH` にまとめて送信）。保存バーに「未保存の変更 N件／保存中…／保存しました／保存に失敗しました」を表示し、失敗時は「再試行」で送り直せます
- LLM（設定 → LLM）を構成すると「✨ 説明を生成」で**サーバー説明と未設定ツールの説明を 1 回でまとめて生成**できます（`server_bundle`。既存のツール説明は上書きせず空欄のみ流し込みます）。ツール行の ✨ と一括生成（「未設定を生成」「すべて再生成」）も `server_bundle` を使い、30 件ずつに分割して呼びます（未設定ならボタンは出ません）
- 索引に入る語彙の設計根拠（実測パネル）を併設
