# 開発ガイド

## 必要条件

- Python >= 3.12
- 任意: Docker（コンテナビルド用）

---

## セットアップ

### ローカル開発環境

```bash
# 依存関係のインストール（開発ツール込み）
pip install -e ".[dev]"

# または、本番依存のみ
pip install -e .
```

**依存パッケージ一覧：**

| パッケージ | バージョン | 必須 | 用途 |
|---|---|---|---|
| `fastmcp` | `>=4.0,<5.0` | ✅ | MCP サーバーフレームワーク（プロキシ基盤） |
| `fastapi` | `>=0.115.0` | ✅ | REST API フレームワーク |
| `uvicorn[standard]` | `>=0.30.0` | ✅ | ASGI サーバー |
| `rank_bm25` | `>=0.2.2` | ✅ | BM25 キーワード検索 |
| `packaging` | `>=24` | ✅ | バージョン比較ユーティリティ |
| `fastembed` | `>=0.4.0` | ❌（任意） | セマンティック検索の埋め込みエンジン（登録モデル用。ORT 経路のモデルでも能力ゲートとして必要） |
| `onnxruntime` | `>=1.17` | ❌（任意） | ONNX Runtime 直叩きの埋め込み（既定 `cl-nagoya/ruri-v3-30m`） |
| `tokenizers` | `>=0.15` | ❌（任意） | ruri のトークナイザ（tokenizer.json） |
| `huggingface_hub` | `>=0.23` | ❌（任意） | ONNX/ トークナイザの取得（`snapshot_download`） |

**開発ツール：**

| パッケージ | 用途 |
|---|---|
| `pytest` | テストフレームワーク |
| `pytest-asyncio` | 非同期テストサポート |
| `httpx` | HTTP テストクライアント |
| `ruff` | リンター兼フォーマッター |
| `mypy` | 静的型チェッカー |

### Docker

```bash
docker build -t mcp-hub .
docker run -p 26263:26263 mcp-hub
```

詳細な Docker 設定は `Dockerfile`、`docker-compose.yml`、`docker-entrypoint.sh` を参照してください。

---

## プロジェクト構造

```
src/mcp_hub/
├── __init__.py
├── admin_router.py   # Admin REST API（/admin/api/*）
├── auth.py           # X-API-Key 認証ミドルウェア
├── bootstrap.py      # 初回起動時のセットアップ（Node.js, uv, fastembed）
├── config.py         # 設定ファイル（hub.config.json）の読み込み
├── env_expand.py     # 環境変数テンプレート展開（${VAR}, ${VAR:-default}）
├── main.py           # エントリーポイント、FastAPI アプリ生成、MCPDispatcher
├── meta_provider.py  # Progressive Discovery（ToolIndex, MetaTools, MetaApp）
├── proxy_manager.py  # 子サーバープロキシの管理（接続/マウント/ヘルスチェック）
├── registry.py       # レジストリ操作（JsonStore のラッパー）
├── state.py          # 共有状態（app_state, request_tags, tags_match）
├── store.py          # JsonStore — hub.config.json への永続化
├── streamable_http_patch.py  # 202 Accepted ポーリング対応パッチ
├── tag_filter.py     # タグベースフィルタリングミドルウェア
├── validators.py     # 入力バリデーション
└── static/
    └── index.html    # Admin Web UI
```

---

## テスト

### テスト実行

```bash
# 全テストを実行
pytest

# 特定のテストファイルを実行
pytest tests/test_admin_api.py

# 詳細出力付き
pytest -v

# 標準出力を表示
pytest -s
```

### テスト構成

- `pytest-asyncio` を使用した非同期テスト
- マーカー `integration` で実プロセスを必要とする遅いテストを分離
- `asyncio_mode = auto`（`pyproject.toml` 設定）

テストファイルは `tests/` ディレクトリに配置されています。

### E2E / 回帰 / 軽負荷シナリオ

`tests/test_e2e_regression.py` が「経路が繋がっていること」を実物寄りで拘束する
（機能単体の契約は各 `test_*.py` 側の責務）。

```bash
# このファイルだけ（推奨・CI と同じ隔離プロセス/メモリ上限）
TEST_FILE=tests/test_e2e_regression.py ./scripts/run-tests.sh

# 直接実行する場合
pytest tests/test_e2e_regression.py -q
```

カバー範囲と期待値:

| シナリオ | 期待 |
|---|---|
| `/admin/api/health`, `/metrics` | 200 / `servers_registered`・`tool_cache` が数値。`uptime_seconds >= 0` |
| normal / meta 切替 | `PATCH /admin/api/settings {"meta_mode": false}` 後、`/mcp/` の `tools/list` から `search_tools` が消える。戻すと復活 |
| メタ経路 (stdio) | 実サブプロセス `tests/test_servers/stdio_echo_server.py` を登録し `search_tools` → `get_schema` → `execute_tool` が `ECHO: hi` を返す |
| プロトコル選択 | URL に `/sse` → `SSETransport`、通常 URL → `StreamableHttpTransport`、`command` → `StdioTransport` |
| 設定リロード | 正常時 200 + `servers` 差分。設定ファイル消失時 500（全削除として適用しない） |
| 埋め込み | `embedding_rebuild.status` が `idle/running/success/error` のいずれか。`../` 等のモデル名は 422 |
| ツールキャッシュ | `PATCH /admin/api/settings` の `tool_cache` が反映、`DELETE /admin/api/cache` が `cleared` を返す |
| スキーマ差分 | 同一サーバーの 2 回目スナップショットで 1 件記録、`?server=` で絞り込み |
| 発見検索 | `GET /admin/api/discovery/search?query=...&kind=tool` が `results` 配列 |
| per-server 制限 | `PATCH /servers/{name}/limits` 200、`max_concurrent: 0` は 422、未登録名は 404 |
| 認証 | `MCP_HUB_API_KEY` 設定時、ヘッダ無し 401 / 正しい `X-API-Key` 200 / health は常に 200 |
| ドレイン | `app_state.draining = True` で 503 + `Retry-After: 1`、戻すと 200 |
| 軽負荷 | `max_concurrent=2` で 10 並列呼び出しが 20 秒以内に完走（デッドロック検出が目的、性能測定ではない） |

前提と閾値の考え方:

- `conftest.py` が bundled config をサーバー0件に差し替え、`MCP_HUB_EMBEDDING=0`
  で埋め込みモデルを読まない（各テストは `MCP_HUB_DATA_DIR` を tmp に隔離）。
- 閾値は「ハングしない」ことを見る緩い上限のみ。スループットの数値目標は
  環境依存が大きいため CI では拘束しない。
- stdio テストはサブプロセスを起動するため、終了時に `close_all()` で回収する
  （失敗すると孤児プロセスが残るので、停止漏れはバグとして扱う）。

---

## リンター・フォーマッター

### Ruff

```bash
# リントチェック
ruff check .

# 自動修正
ruff check --fix .

# フォーマットチェック
ruff format --check .

# フォーマット適用
ruff format .
```

### mypy（静的型チェック）

```bash
mypy src/
```

---

## Docker 開発

### docker-compose

```bash
# ビルドして起動
docker compose up --build

# ログ確認
docker compose logs -f

# ヘルスチェック
curl http://localhost:26263/admin/api/health
```

### Dockerfile 変更時の確認手順

1. `docker compose up --build` で再ビルド
2. `docker compose logs` に ERROR がないことを確認
3. `HEALTHCHECK` が通っていることを確認
4. `GET /admin/api/health` が `{"status":"ok"}` を返すことを確認

---

## コーディング規約

### 原則

- **単一責任**: 各モジュールは明確な責務を持つ
- **内部 API への依存を明示**: FastMCP のプライベート API を使用する箇所には NOTE コメントを付記
- **エラーハンドリング**: 予期しない例外はログに記録し、可能な限りリカバリを試みる
- **非同期ファースト**: I/O バウンドな処理は `asyncio` を使用

### コミットメッセージ

- `feat:` — 新機能
- `fix:` — バグ修正  
- `chore:` — 雑務（依存関係更新、設定変更等）
- `docs:` — ドキュメントのみの変更

### 変更範囲

- 1 コミットあたりの変更行数は 100 行以内に抑える
- 変更は垂直スライス（小さな機能単位）で行う

---

## 関連ドキュメント

| ドキュメント | 説明 |
|---|---|
| `docs/configuration.md` | 設定ファイル・環境変数リファレンス |
| `docs/api-reference.md` | API エンドポイント一覧 |
| `docs/architecture.md` | アーキテクチャ概要・コンポーネント解説 |
| `docs/security.md` | セキュリティモデル |
| `docs/architecture.md` | アーキテクチャ詳細 |
