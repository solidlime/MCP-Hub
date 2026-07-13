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

## meta_mode ベンチマーク

`meta_mode` は Progressive Discovery を有効にする設定。通常モード（15ツールを直接公開）と比較して、ツール選択の精度が向上する。

### 比較結果 (tencent/hy3:free, 7ケース×3回)

| テストケース | meta_mode OFF | meta_mode ON |
|---|---|---|
| brave_web_search（Web検索） | 3/3 (100%) | 3/3 (100%) |
| puppeteer_navigate（ページ遷移） | 3/3 (100%) | 3/3 (100%) |
| brave_local_search（ローカル検索） | 3/3 (100%) | 3/3 (100%) |
| puppeteer_screenshot（スクリーンショット） | 3/3 (100%) | 3/3 (100%) |
| sequentialthinking（段階的思考） | 1/3 (33%) | 0/3 (0%) |
| puppeteer_click（クリック操作） | 0/3 (0%) | 3/3 (100%) |
| web_search_exa（Exa検索） | 0/3 (0%) | 3/3 (100%) |
| **総合** | **13/21 (62%)** | **18/21 (86%)** |

**結論**: `meta_mode ON` ではツール数が3つに圧縮されることで、LLMのツール選択精度が 62% → 86% に向上（+24pt）。特に類似ツールの多いカテゴリ（ブラウザ操作系、検索系）で効果が顕著。**デフォルトでON**。問題がある場合は管理UIの設定パネルまたは `PATCH /admin/api/settings` で OFF に切り替え可能。

ベンチマークの再実行:
```bash
export OPENROUTER_API_KEY='sk-or-v1-...'
python .benchmarks/meta_mode_benchmark.py
```

## 制限事項

- **Progress通知の転送未対応**: FastMCPの`ProxyProvider`が`onprogress`コールバックを露出していないため、子MCPサーバーの進捗通知（`notifications/progress`）はクライアントに転送されない。詳細は [`docs/progress-forwarding.md`](docs/progress-forwarding.md) 参照。
