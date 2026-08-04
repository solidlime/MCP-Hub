# MCP Hub

複数の MCP サーバーをまとめて、ひとつのエンドポイントで AI アシスタントに提供するプロキシツールです。

## 何ができるの？

MCP Hub を使うと、`filesystem`、`brave-search`、`github` などの MCP サーバーを一箇所に登録し、AI アシスタント（Claude Desktop や VS Code Copilot など）から **ひとつの接続先** でまとめて使えるようになります。

- 🔌 **サーバーを束ねる**: stdio 起動のサーバーも、HTTP で動くリモートサーバーも、まとめて管理
- 🏷️ **タグでフィルタ**: 用途別にタグ付けして、必要なツールだけを公開
- 🖥️ **管理画面付き**: ブラウザからサーバーの追加・編集・状態確認ができる
- 📦 **モジュール後付けインストール**: WebUI から `pip install` せずに Python モジュールを追加可能
- 🔍 **Progressive Discovery**: ツールが増えすぎても賢く検索（デフォルト有効）
- ⭐ **フル公開ツール**: Meta モードでも特定ツールだけ通常公開（`full_info_tools`、WebUI のトグルで切替）
- 📋 **ツールログ**: ツール呼び出し・サーバー接続イベントを WebUI のログタブで確認（機密情報はマスク）

## クイックスタート

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
