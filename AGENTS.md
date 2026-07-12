# MCP-Hub プロジェクト固有ルール

## WebUI変更時のブラウザ確認（必須）
`src/mcp_hub/static/index.html` またはCSS/JSの変更後は、`browser-testing-with-devtools` スキルを使って実ブラウザで以下を確認すること：
- コンソールにエラーがないこと
- サーバーカードが正しくレンダリングされていること
- モーダル・トグル・タグ操作が正常に動作すること

## Docker確認（必須）
`Dockerfile` / `docker-compose.yml` / `docker-entrypoint.sh` 変更時は `docker compose up --build` して以下を確認：
- HEALTHCHECKが通ること
- `docker compose logs` にERRORがないこと
- 管理API `/admin/api/health` が `{"status":"ok"}` を返すこと
