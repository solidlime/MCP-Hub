# MCP Hub

複数の MCP サーバーをまとめて、ひとつのエンドポイントで AI アシスタントに提供するプロキシツールです。

## 何ができるの？

MCP Hub を使うと、`filesystem`、`brave-search`、`github` などの MCP サーバーを一箇所に登録し、AI アシスタント（Claude Desktop や VS Code Copilot など）から **ひとつの接続先** でまとめて使えるようになります。

- 🔌 **サーバーを束ねる**: stdio 起動のサーバーも、HTTP で動くリモートサーバーも、まとめて管理
- 🏷️ **タグでフィルタ**: 用途別にタグ付けして、必要なツールだけを公開
- 🖥️ **管理画面付き**: ブラウザからサーバーの追加・編集（名前変更含む）・状態確認ができる
- 📦 **モジュール後付けインストール**: WebUI から pip / uv / npm を指定してモジュールを追加可能（シェル実行なしの構造化 API でバリデーション済み）
- 🔍 **Progressive Discovery**: ツールが増えすぎても賢く検索（デフォルト有効）
- ⭐ **フル公開ツール**: Meta モードでも特定ツールだけ通常公開（`full_info_tools`、WebUI のトグルで切替）
- 📋 **ツールログ**: ツール呼び出し・サーバー接続イベントを WebUI のログタブで確認（機密情報はマスク）
- 🛡️ **セキュリティハードニング**: 入力バリデーション・SSRF/シェルインジェクション対策・HostOriginGuard・管理API認証（詳細は [セキュリティ](docs/security.md)）

## クイックスタート

要件: Python 3.12+（内部で `fastmcp>=4.0,<5.0` を使用）

```bash
pip install mcp-hub
python -m mcp_hub.main
```

起動したら http://localhost:26263/admin/ にアクセスして管理画面を開きます。

## サーバーを追加する

`data/hub.config.json`（自動生成されます）に使いたい MCP サーバーを書くだけです。

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    },
    "brave-search": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-brave-search"],
      "env": {
        "BRAVE_API_KEY": "あなたのAPIキー"
      }
    }
  }
}
```

リモートの MCP サーバーにつなぐ場合：

```json
{
  "mcpServers": {
    "remote-tools": {
      "url": "http://nas:8080/mcp",
      "headers": {
        "Authorization": "Bearer my-token"
      }
    }
  }
}
```

設定を変更したら Hub を再起動すれば反映されます。管理画面からも追加・編集できます。

## AI アシスタントから使う

AI アシスタントの設定に Hub のエンドポイントを指定します。

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "mcp-hub": {
      "url": "http://localhost:26263/mcp"
    }
  }
}
```

特定のタグが付いたサーバーのツールだけ使いたい場合は：

```json
{
  "mcpServers": {
    "mcp-hub": {
      "url": "http://localhost:26263/mcp?tags=web,local"
    }
  }
}
```

## Docker で動かす

```bash
docker build -t mcp-hub .
docker run -p 26263:26263 -v $(pwd)/data:/app/data mcp-hub
```

`docker compose up` でも起動できます（`ghcr.io/solidlime/mcp-hub:latest` イメージ使用・PUID/PGID 自動検出対応）。compose ファイルは docker.sock をマウントするため、実行環境の信頼性に注意してください。

## パフォーマンス

MCP Hub の Progressive Discovery（メタモード）は、全プロトコルで **100% のツール呼び出し成功率** を達成しながら、AI に送るツール定義を大幅に削減します。

### ツール呼び出し成功率（プロトコル別）

| プロトコル | サーバー | ツール数 | Meta ON | Meta OFF |
|-----------|--------|:---:|:---:|:---:|
| stdio | filesystem | 14 | 100% (9/9) | 100% (9/9) |
| stdio | sequential-thinking | 1 | 100% (3/3) | 100% (3/3) |
| Streamable HTTP | exa (web_search, web_fetch) | 2 | 100% (3/3) | 100% (3/3) |
| SSE | sse-echo | 1 | 100% (3/3) | 100% (3/3) |
| Streamable HTTP (202 async) | async-mcp | 1 | 100% (3/3) | 100% (3/3) |
| **合計** | | **19** | **100% (21/21)** | **100% (21/21)** |

> stdio / SSE / Streamable HTTP の全プロトコルで Meta ON/OFF 両方とも 100% 成功。
>
> この表は LLM 実呼び出しベンチマークで再現できます: `OPENROUTER_API_KEY=sk-or-... python scripts/benchmark_toolcall.py --trials 3`（LLM: `deepseek/deepseek-v4-flash-0731`、Meta ON/OFF 各3試行、exa は `EXA_API_KEY` 設定時のみ計測）。

### ツール定義サイズ

| モード | 公開ツール数 | ツール定義サイズ | 
|--------|:---------:|:-------------:|
| 通常モード（全ツール直接公開） | 19 | 19ツール分の全スキーマ |
| メタモード | 3 | 3ツールのみ |

**5台のサーバー・84ツールの場合、約15,500トークン → 約500トークンに削減。** AI のコンテキストウィンドウ消費を約97%カットします。

## 環境変数

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `MCP_HUB_PORT` | `26263` | 待ち受けポート |
| `MCP_HUB_HOST` | `0.0.0.0` | バインドアドレス |
| `MCP_HUB_DATA_DIR` | `data` | 設定・DBの保存先 |
| `MCP_HUB_API_KEY` | （なし） | 設定すると管理APIに認証がかかる |
| `MCP_HUB_RESEED` | （なし） | `1` でDBクリア＋設定から再シード |
| `MCP_HUB_LOG` | `text` | `json` で構造化ログ出力 |
| `MCP_HUB_HEALTH_INTERVAL` | `60` | ヘルスチェック間隔（秒。`0` 以下で無効化） |
| `MCP_HUB_HEALTH_TIMEOUT` | `25` | ヘルスチェックのタイムアウト（秒。20秒以上を推奨） |
| `MCP_HUB_HEALTH_MAX_FAILURES` | `3` | 連続失敗の許容回数 |
| `MCP_HUB_RETRY_MAX` | `3` | サーバー接続の最大リトライ回数 |
| `MCP_HUB_RETRY_DELAY` | `1.0` | リトライ間隔のベース遅延（秒。指数バックオフ） |
| `MCP_HUB_LIST_TOOLS_TIMEOUT` | `10.0` | `list_tools` 集約のタイムアウト（秒） |
| `MCP_HUB_LIST_TOOLS_RETRY_DELAY` | `0.3` | `list_tools` リトライの遅延（秒） |
| `MCP_HUB_CALL_TOOL_TIMEOUT` | `30` | ツール呼び出しのタイムアウト（秒） |
| `MCP_HUB_CLIENT_TIMEOUT` | `180.0` | アップストリーム読み取りタイムアウト（秒。WebUI設定が優先） |
| `MCP_HUB_CONNECT_TIMEOUT` | `30.0` | 起動時接続確認のタイムアウト（秒。WebUI設定が優先） |
| `MCP_HUB_MAX_CONCURRENT_CALLS` | `50` | 同時ツール呼び出しの最大数 |
| `MCP_HUB_RECOVERY_COOLDOWN` | `300.0` | 死んだサーバーへの再接続試行の最小間隔（秒） |
| `MCP_HUB_EMBEDDING` | `1` | `0` でセマンティック検索を強制無効化 |
| `MCP_HUB_SESSION_IDLE_TIMEOUT` | （なし） | 上流セッションのアイドル有効期限（秒。未設定で期限なし） |

詳細は [設定リファレンス](docs/configuration.md) を参照してください。

## ドキュメント

詳しい設定や開発者向け情報は `docs/` ディレクトリを参照してください。

| ドキュメント | 内容 |
|-------------|------|
| [設定リファレンス](docs/configuration.md) | 全設定項目・環境変数・タグフィルタの詳細 |
| [API リファレンス](docs/api-reference.md) | MCP エンドポイント・管理 REST API 一覧 |
| [アーキテクチャ](docs/architecture.md) | 内部設計・Progressive Discovery・制限事項 |
| [セキュリティ](docs/security.md) | 入力検証・認証・セキュリティモデル |
| [開発ガイド](docs/development.md) | 開発環境構築・テスト・貢献方法 |

## ライセンス

MIT
