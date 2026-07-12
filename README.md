# MCP Hub

MCPサーバーのプロキシ・レジストリ。複数のMCPサーバーを統合管理し、単一エンドポイントでLLMに公開する。

## 機能

- **サーバー管理**: 複数MCPサーバーの登録・接続・ヘルスチェック
- **ツールプロキシ**: 全サーバーのツールを透過的に集約
- **管理UI**: Webブラウザからサーバー管理 (`/admin/`)
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
      "name": "fetch",
      "url": "http://localhost:8080/mcp",
      "tags": ["web", "fetch"]
    }
  ],
  "log_level": "INFO"
}
```

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
