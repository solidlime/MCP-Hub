# MCP Context Pressure Research

> 2026-07-12 | @librarian #042 調査

## 問題

MCP-Hubは全サーバーの全ツールを素通しで公開している。5サーバー・84ツールで約15,500トークン（200Kウィンドウの8%）、100ツール超で25%超。ツール定義は1ツールあたり500〜1,400トークン。

## 業界の対応状況

| 戦略 | 削減率 | 代表プロジェクト |
|------|--------|-----------------|
| Progressive Discovery（メタツール） | 99% | mcp-guardian, harshal-mcp-proxy |
| Schema Compression | 85-95% | clear-your-tools, mcp-fusion |
| Response Shielding | 98% | mcp_trunc_proxy |

ほぼ全てのMCPゲートウェイがProgressive Discoveryを採用している。3個のメタツール（`search_tools`, `get_tool_schema`, `execute_tool`）だけを公開し、全バックエンドツールはBM25検索経由で間接的に呼び出す。

## トレードオフ

**Progressive Discoveryのリスク**: ツールが直接見えないとLLMのツール呼び出し成功率が下がる可能性がある。search→get_schema→executeの3ステップが必要で、各ステップでエラーや曖昧さが入り込む。

**対策案**:
- ツール数が少ないうち（<30）は従来の全公開モード
- 閾値を超えたら自動でメタツールモードに切り替え
- ユーザーが明示的にモードを選べるようにする（`X-MCP-Hub-Mode: meta` ヘッダーなど）
- よく使うツールをキャッシュ/ピン留めして直接公開

## クライアント側のハードリミット

| クライアント | リミット |
|-------------|---------|
| Cursor | 40ツール（Dynamic Toolsで回避可） |
| VS Code / Copilot | 128ツール |
| Windsurf | 100ツール |
| Claude Desktop/Code | ソフトリミット（200Kウィンドウ次第） |

## 実装方針（フェーズ分け）

### Phase 1 (p0): Progressive Discovery Meta-Tools
- BM25検索エンジン（`rank_bm25`、依存ゼロ）
- `search_tools` / `get_tool_schema` / `execute_tool` の3メタツール
- 設定またはヘッダで切替可能
- コード量: ~300行（新規 `meta_provider.py` + proxy_manager修正）

### Phase 2 (p1): Description Compression + Tool Allowlist
- ツール単位の `allowed_tools` / `blocked_tools`
- description override（長すぎる説明を短縮）
- ツール名prefix自動付与（サーバー名_ツール名）

### Phase 3 (p2): Response Shielding
- 巨大レスポンスの自動トリミング
- Artifact store + 取得ツール注入

### Phase 4 (p3): Server Lazy Loading
- カタログスナップショット（ツール名+説明のみ）
- オンデマンドプロセス起動
- アイドル監視・自動切断

## 2026-07-15 実装済み最適化（Production Hardening）

### ツールコールスループット改善

| 最適化 | 内容 | 効果 |
|--------|------|------|
| `include_tools` パラメータ | `GET /admin/api/servers?include_tools=false` でツール一覧取得をスキップ | N サーバー × T ツール分のネットワーク呼び出し削減（3サーバー22ツールで ~2秒 → ~5ms） |
| meta_mode キャッシュ | MCPDispatcher が `hub.config.json` のディスク読み取りをキャッシュ | 毎リクエストの I/O 排除、~0.5ms → ~0.01ms |
| Store 二重読み取り排除 | `update_server` の TOCTOU 修正により `_read_locked` の冗長呼び出しを削除 | 更新処理の I/O 33%削減 |
| 読み取りロック直列化 | `asyncio.Lock` で同時書き込みの破損を防止（単一 JSON ファイルのため競合は軽微） | 安全性確保。100 req/s 未満ではスループット低下無視可 |

### コンテキスト削減率

`include_tools=false` 時のレスポンスサイズ比較（3サーバー構成時）:

| モード | レスポンスサイズ | 削減率 |
|--------|-----------------|--------|
| `include_tools=true`（デフォルト） | ~12KB（全22ツールの description + schema） | — |
| `include_tools=false` | ~1.2KB（サーバー名+設定のみ） | **90%** |

クライアントがツール一覧を必要としない管理画⾯やダッシュボードでは、90%のレスポンス削減が得られる。WebUI の初期描画と定期ポーリングの両方で有効。

### レイテンシ内訳（`/servers` エンドポイント、3サーバー・22ツール）

| フェーズ | `include_tools=true` | `include_tools=false` |
|----------|---------------------|----------------------|
| Store 読み取り | ~0.5ms | ~0.5ms |
| プロキシ状態取得 | ~1ms | ~1ms |
| ツール一覧取得（N並列） | ~2,000ms | — |
| JSON シリアライズ | ~5ms | ~1ms |
| **合計** | **~2,007ms** | **~2.5ms** |

### FastMCP バージョンガード

`>=3.5.0` の FastMCP との非互換を検出し、`sys.exit(1)` で起動を拒否する。`packaging>=24` が依存に追加された。

## 参照

- MCP Client Best Practices: https://modelcontextprotocol.io/docs/develop/clients/client-best-practices
- SEP-1928 Progressive Disclosure: https://github.com/modelcontextprotocol/modelcontextprotocol/pull/1928
- mcp-guardian: https://github.com/S1LV3RJ1NX/mcp-guardian
- harshal-mcp-proxy: https://github.com/HarshalRathore/harshal-mcp-proxy
