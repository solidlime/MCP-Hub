# MCP Hub

MCPサーバーのプロキシ・レジストリ。複数のMCPサーバーを統合管理し、単一エンドポイントでLLMに公開する。

## 機能

- **サーバー管理**: 複数MCPサーバーの登録・接続・ヘルスチェック
- **ツールプロキシ**: 全サーバーのツールを透過的に集約
- **管理UI**: Webブラウザからサーバー管理 (`/admin/`)
  - コマンド/URL モード切替: コマンド版・URL版を1つのフォームで管理
  - HTTPヘッダー設定: URLモードで `Authorization` 等のカスタムヘッダーを設定可能
  - クォート付き値の自動除去: `"value"` や `'value'` をそのままコピペ可能
- **Streamable HTTP**: FastMCP の streamable-http トランスポート

## 起動

```bash
pip install mcp-hub
python -m mcp_hub.main
```

## 設定

`hub.config.json` にサーバー設定を記述する。

```json
{
  "servers": [
    {
      "name": "filesystem",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "env": {
        "HOME": "/home/user"
      }
    },
    {
      "name": "brave-search",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-brave-search"],
      "env": {
        "BRAVE_API_KEY": "your-key"
      },
      "disabled": false
    },
    {
      "name": "remote-server",
      "url": "http://nas:8080/mcp",
      "headers": {
        "Authorization": "Bearer my-token",
        "X-API-Key": "sk-xxx..."
      },
      "tags": ["remote", "api"]
    }
  ],
  "log_level": "INFO"
}
```

### サーバー設定フィールド

| フィールド | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `name` | string | ✅ | サーバー識別名 |
| `url` | string | ⚠️ | MCPサーバーのHTTP URL（`command` と排他） |
| `command` | string | ⚠️ | 実行コマンド（`url` と排他） |
| `args` | string[] | - | コマンドライン引数（1行1引数） |
| `env` | object | - | 環境変数（`KEY: value` 形式） |
| `headers` | object | - | HTTPリクエストヘッダー（URLモードのみ有効） |
| `tags` | string[] | - | 分類用タグ |
| `disabled` | bool | - | `true` で接続を無効化 |

設定ファイル + DB は `MCP_HUB_DATA_DIR` で指定したディレクトリ（デフォルト: `data`）に保存される。
`MCP_HUB_RESEED=1` で起動時にDBをクリアし、設定ファイルから再シードする。

環境変数:
| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `MCP_HUB_PORT` | `26263` | リスンポート |
| `MCP_HUB_HOST` | `0.0.0.0` | バインドホスト |
| `MCP_HUB_DATA_DIR` | `data` | 設定 + DB の保存ディレクトリ |
| `MCP_HUB_LOG` | `text` | `json` でJSON構造化ログ |
| `MCP_HUB_RESEED` | — | `1` でDBクリア+設定から再シード |
| `MCP_HUB_API_KEY` | — | 管理APIの認証キー（未設定時は認証無効） |

### 管理API認証

`MCP_HUB_API_KEY` を設定すると、`/admin/api/*` へのリクエストに `X-API-Key` ヘッダーが必須になる。ヘルスチェックエンドポイント (`/admin/api/health`) は認証免除。

```bash
# 認証なし — 401
curl http://localhost:26263/admin/api/servers

# 認証あり — 200
curl http://localhost:26263/admin/api/servers -H "X-API-Key: your-secret"
```

## タグフィルタリング

MCPエンドポイントはクエリパラメータまたはHTTPヘッダーでタグフィルタリングに対応している。

**クエリパラメータ:**
```
/mcp?tags=web,local
```

**HTTPヘッダー:**
```
X-MCP-Hub-Tags: web,local
```

指定されたタグを持つサーバーのツールのみが透過的に公開される。タグ未指定の場合は全サーバーのツールが利用可能。管理UIの接続情報パネルからコピーして使用できる。|

## Docker

```bash
docker build -t mcp-hub .
docker run -p 26263:26263 mcp-hub
```

## API

- `/mcp` — MCPエンドポイント (streamable-http)
- `/admin/` — 管理UI
- `/admin/api/health` — ヘルスチェック
- `/admin/api/servers` — サーバー一覧・管理API
- `/admin/api/servers/{name}/connection` — サーバー接続情報
- `/admin/api/metrics` — Hubメトリクス

### `GET /admin/api/servers` パラメータ

| パラメータ | 型 | デフォルト | 説明 |
|-----------|-----|-----------|------|
| `include_tools` | bool | `true` | ツール一覧を含めるか。`false` でレスポンスサイズ ~90%削減 |

## Metrics

`GET /admin/api/metrics` でHubの運用メトリクスを取得できる。

```json
{
  "uptime_seconds": 3600.0,
  "servers_registered": 5,
  "servers_active": 3,
  "total_tools": 42,
  "tool_calls_total": 128,
  "tool_call_errors": 2
}
```

## 内部リソース

FastMCPのResource機構で `hub://servers` が利用可能。接続サーバーのJSONスナップショットを返す。

```json
// MCPクライアントからのアクセス例
{
  "name": "fetch",
  "disabled": false,
  "tags": ["web"],
  "status": "connected",
  "tool_count": 5
}
```

## meta_mode ベンチマーク

`meta_mode` は Progressive Discovery を有効にする設定。通常モード（全ツールを直接公開）とメタモード（3ツール: `search_tools`, `execute_tool`, `list_upstream_tools`）を比較。

### ツール選択成功率

各プロトコルの実サーバーで全ツールを `search_tools` → `execute_tool` の2ホップ（Meta ON）と
直接名前空間呼び出し（Meta OFF）の両方で呼び出し、3イテレーションの成功数を計測。

| プロトコル | サーバー | ツール数 | Meta ON | Meta OFF |
|-----------|--------|:-----:|:---:|:---:|
| stdio | filesystem | 14 | 9/9 (100%) | 9/9 (100%) |
| stdio | sequential-thinking | 1 | 3/3 (100%) | 3/3 (100%) |
| Streamable HTTP | exa (web_search, web_fetch) | 2 | 3/3 (100%) | 3/3 (100%) |
| SSE | sse-echo (テストサーバー) | 1 | 3/3 (100%) | 3/3 (100%) |
| Streamable HTTP (202 async) | async-mcp (テストサーバー) | 1 | 3/3 (100%) | 3/3 (100%) |
| **合計** | | **19** | **21/21 (100%)** | **21/21 (100%)** |

> **全プロトコル (stdio/SSE/Streamable HTTP) × 全サーバーで Meta ON/OFF 両方とも 100% 成功。**
> Meta ON と Meta OFF は内部で同じ `pm.call_tool()` を使用している。

### ツール定義サイズ比較

| モード | メタツール数 | tools/list サイズ | 成功率 |
|--------|:---------:|:-----------------:|:------:|
| 通常モード (N=19) | — | 19ツール分の全スキーマ | 100% |
| メタモード | 3 | 3ツールのみ | 100% |

**結論**: どちらのモードも全プロトコルで 100% 成功。メタモードはツール定義サイズを大幅に削減しつつ同等の成功率を達成。**デフォルトでON**。

## セキュリティ

### 入力検証

管理者API経由で登録されるサーバー設定は自動検証される:

- **コマンド**: `$()`（サブシェル実行）、`;`, `|`, `` ` ``（シェルメタ文字）をブロック。`${VAR}` テンプレートは許可
- **URL**: `http`/`https` スキームのみ許可（file://, gopher:// 等は拒否）
- **環境変数**: `PATH`, `LD_PRELOAD`, `LD_LIBRARY_PATH` 等の危険な変数上書きをブロック
- **HTTPヘッダー**: CRLF/制御文字をブロック。キーは最大256文字、値は最大8192文字

### バージョンガード

非互換な FastMCP >=3.5.0 での起動を `sys.exit(1)` で拒否する。

## 制限事項

- **Progress通知の転送未対応**: FastMCPの`ProxyProvider`が`onprogress`コールバックを露出していないため、子MCPサーバーの進捗通知（`notifications/progress`）はクライアントに転送されない。詳細は [`docs/progress-forwarding.md`](docs/progress-forwarding.md) 参照。
