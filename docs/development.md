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
| `fastmcp` | `>=3.4.0,<4.0.0` | ✅ | MCP サーバーフレームワーク（プロキシ基盤） |
| `fastapi` | `>=0.115.0` | ✅ | REST API フレームワーク |
| `uvicorn[standard]` | `>=0.30.0` | ✅ | ASGI サーバー |
| `rank_bm25` | `>=0.2.2` | ✅ | BM25 キーワード検索 |
| `packaging` | `>=24` | ✅ | バージョン比較ユーティリティ |
| `fastembed` | `>=0.4.0` | ❌（任意） | セマンティック検索用埋め込みモデル |

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
