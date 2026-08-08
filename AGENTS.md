# MCP-Hub プロジェクト固有ルール

## テスト実行（必須）
全体テストは **`scripts/run-tests.sh` で実行すること**（`pytest` 直接実行は禁止）:
- 理由: 単一プロセスで全テストを実行すると fastembed モデル（~670MB/回）がプロセス内に蓄積し、
  低メモリ環境（7GB 級）ではスワップ枯渇 → ハング、OOM キラーが他プロセス（opencode 含む）を巻き込む。
- このスクリプトはテストファイルごとに別プロセスで実行し、`systemd-run` の cgroup メモリ制限
  （デフォルト 3G、`TEST_MEMLIMIT` で変更可）で OOM の波及を防ぐ。
- 単一ファイルのみ実行する場合も同様に `TEST_FILE=tests/test_xxx.py ./scripts/run-tests.sh`。

## WebUI変更時のブラウザ確認（必須）
`src/mcp_hub/static/index.html` またはCSS/JSの変更後は、`webapp-testing` スキル（Python Playwright ツールキット）を使って実ブラウザで以下を確認すること：
- コンソールにエラーがないこと
- サーバーカードが正しくレンダリングされていること
- モーダル・トグル・タグ操作が正常に動作すること

### 検証ホスト（重要）
ブラウザの `localhost` は「ブラウザが動いているマシン」を指す。使用するブラウザで接続先が変わる：
- **ローカル Playwright**（`webapp-testing` スキル、実行マシン上でブラウザ起動）→ `http://localhost:26263` でOK
- **リモート puppeteer MCP**（NAS 上で動くブラウザ）→ `localhost` は **NAS 自身**を指すため、
  開発サーバーへは **必ず `http://ubuntu.tail1e4bd.ts.net:26263/`** を使うこと。
  ⛔ `http://localhost:26263` は NAS 上の本番インスタンス（古いコード）を指すため、開発検証での使用禁止。
- 本番インスタンス（NAS）の確認だけが目的なら、リモートブラウザで `http://localhost:26263` を使ってもよい。

## Docker確認（必須）
`Dockerfile` / `docker-compose.yml` / `docker-entrypoint.sh` 変更時は `docker compose up --build` して以下を確認：
- HEALTHCHECKが通ること
- `docker compose logs` にERRORがないこと
- 管理API `/admin/api/health` が `{"status":"ok"}` を返すこと
