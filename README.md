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

環境変数:
| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `MCP_HUB_PORT` | `26263` | リスンポート |
| `MCP_HUB_HOST` | `0.0.0.0` | バインドホスト |
| `MCP_HUB_DB_PATH` | `data/hub.db` | SQLite DBパス |

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
