# MCP SDK v2 移行デザイン (B完全移行) — 2026-09-04

## 背景
- MCP-Hubは現状 `fastmcp==3.4.4` + `mcp==1.28.1` (`pyproject.toml:7 fastmcp>=3.4.0,<4.0.0`)。
- `mcp 2.0.0 (2026-07-28)` で `McpError→MCPError` 改名等により `mcp-server-fetch` がクラッシュ (#4560)。本番は `uvx --with "mcp<2.0.0"` 回避で稼働。
- `fastmcp 3.4.x` は `mcp<2.0` を明示ピン (`fastmcp_slim/pyproject.toml: mcp>=1.24.0,<2.0`) のため、v2対応は `fastmcp 4.x` への上げが必須。
- ユーザー判断 (2026-09-04): いずれ必要なので今対応する。ターゲット `4.0.x + mcp2最新`、互換 `v1+v2両対応必須`、検証 `フル検証 (scripts/run-tests.sh + 実機Hub)`、設定 `D:\Desktop\hub.config.json` 使用、アプローチ `B完全移行`。

## 決定事項
- D1 ターゲット: `fastmcp>=4.0,<5.0` (4.0.1+解決) + `mcp>=2.0,<3.0` (2.1.x最新) + `httpx2>=2.5.0排他` + `pydantic>=2.12` + `starlette>=1.0.1` + `opentelemetry-api>=1.28` + `mcp-types` exact-pin受け入れ。
- D2 互換: v1上流も維持。`Client(mode="auto")` のmodern probe→legacy fallbackに委譲し、Hub側で分岐を持たない。
- D3 回避ピン: `Fetch` (`hub.config.json:61-67`) と `grok` (`187-195`) の `--with "mcp<2.0.0"` は移行直後は残して緑化、最期にfetch側v2対応確認が取れたら外す。現状はまだ必要 (素のuvxだとmcp2を引いてImportError)。2026-08-18にfetch側が `mcp>=1.29,<2` にpinし直し済みのため冗長化しつつあるが、完全mcp2対応は別PR移行中のため断定しない。
- D4 アプローチ: B完全移行 (bridgeに頼らず内部API剥がし込み)。理由: Hubの価値はプロキシの安定であり将来の破壊を今潰す。

## §1 アーキテクチャ全体方針
- 起点は `pyproject.toml:7` の `<4.0.0` 上限撤廃→ `>=4.0,<5.0`。
- 内部API剥がしが核: `providersリセット`、`routes走査→session_manager差し替え`、`_mcp_server注入`、`LenientSM私的API`、`streamable_http_patch猿パッチ群` を4.x公開API (`providers=[...]`、`mount(namespace=)`、`create_proxy(target)`、`local_provider.remove_tool`、`add_transform(ToolTransform(...))`) に置換。置換不可のみバージョンガード付きshimに隔離。
- `mcp_camelcase_compat` 等のbridgeは恒久依存にしない (新記法 `input_schema`/`is_error` 等に直す)。

## §2 コンポーネント別改修
- `proxy_manager.py` (最高リスク): `FastMCPProxy(client_factory,name,provider_error_strategy)` 見直し、`mount(proxy,namespace=name)` (`prefix→namespace`)、`providers=[local_provider]` を公開構成へ、`forward_incoming_headers` を4.x正規手段へ、`Client.__aenter__/close` 再入前提の見直し。`_proxy_providers` 未使用importは削除。
- `main.py`: `__version__` ガードを `>=5.0.0` 警告へ、`add_middleware` 順序 (ToolLog→FullInfo、外側=先add) 維持、`http_app(transport,path)` 追従、`routes→StreamableHTTPASGIApp→session_manager差し替え` と `_mcp_server注入`、`_lifespan_manager()+sm.run()` を4.x lifespan1回前提に合わせる。`main.py:266 <3.5.0` コメント乖離を修正。
- `middleware3種+tag_filter`: `on_call_tool(ctx:MiddlewareContext[CallToolRequestParams],call_next)` 維持確認、`Tool(name,description,parameters)` 合成をsnake_caseへ、`tag_filter:116 _server私的属性` を公開逆引きへ、`on_list_tools/resources/templates/prompts` を `on_message` 拡大 (#4553) と整合。
- `lenient_session_manager` + `streamable_http_patch`: `_handle_post_request`、`_prepare_headers`、`_is_initialization_request`、`_maybe_extract_session_id`、`_handle_json_response/_handle_sse_response/_handle_unexpected_content_type`、`_server_instances`、`_handle_stateless_request` を4.x形状に追従。#081重点レビュー対象。
- `tests/test_servers` + `docs`: echo2種を4.x記法へ、素FastAPI async mock維持、`architecture.md:308 sys.exit` 矛盾・旧ピン残骸 (`2026-08-03-tool-log-dashboard.md:13`) 修正、`architecture/development/security` のピン記述更新。

## §3 データフロー・エラー・検証
- フロー不変: 上流 (stdio/SSE/StreamableHTTP) → `Client(auto)` → `FastMCPProxy` → `mount(namespace=)` → `ToolLog→FullInfo→TagFilter` →下流。`LenientSM` はPOST寛容・GET/DELETE 404維持。
- エラー作法: `McpError(code=,message=)` kwargs、`tool例外→-32603 sanitized` (意図的wire errorのみ `MCPError`)、`resource-not-found -32002→-32602`、`AnyUrl→str`、`RootModel→union+TypeAdapter`、`timeout float秒`、`streamablehttp_client削除→streamable_http_client`、`httpx→httpx2` (factory/Auth/except洗替、TLS truststore、logger改名)。
- 振る舞い取込: `elicit(response_type必須)`、`sample/list_roots/sampling_handler削除→直呼びor guard`、`on_initialize現代で不実行`、`session state不永続`、`templated resources path-screened`、`Background Tasks別pkg+extension` (未使用なら対象外確認のみ)。
- 検証 (フル): `TEST_FILE=... ./scripts/run-tests.sh` で全件 (`pytest` 直実行禁止)、`D:\Desktop\hub.config.json` で実機Hub起動→ツール列挙→Fetch/grok pin外し可否確認、型・lint・format・カバレッジ・契約・監査はGATE条件通り。UI変更なしのため実ブラウザ確認は対象外。完了後に #081 アーキレビュー→実装 (`writing-plans`) へ。

## 非目標 (YAGNI)
- 二系統並行運用 (C) はしない。単一ブランチで完結。
- 未使用extension (`tasks` 等) の新規導入はしない。確認のみ。
- 周辺サーバー本体 (fetch/grok) の改修はしない。Hub側pin操作のみ。

## リスク
- R1 私的API追従漏れで起動不能 (`main`/`proxy`/`patch`) →実機起動を最優先検証に。
- R2 `httpx2` 移行漏れ (`except httpx` dead code) →grepで全洗替。
- R3 middleware `on_message` 拡大でログ爆発・フィルタ素通り (`_server` 撤去時) →結合テストで検出。
- R4 `pydantic/starlette` floor衝突 (`FastAPI<0.133`) → `>=0.133.0` へ。

## 次ステップ
1. 本specの承認→ `writing-plans` で実装計画化。
2. 実装前に #081 事前アーキ判断 (本格レベル)。
3. 実装後に #081 REVIEW (PASS以外BLOCK)、GATE通過後のみCOMMIT。

## 出典
- #009 recon: pyproject/uv.lockピン、src 26箇所import、壊れポイント (mount/FastMCPProxy/providersリセット/routes走査/_mcp_server/lifespan/Tool合成/_server/猿パッチ群)、docs言及、echo依存、コミット 80ca571/35b25bb/cc8bc3e/d7a3894。
- #042 調査 (2026-09-04): mcp v2.0.0 (2026-07-28)・2.0.1/2.1.0/2.1.1並行、移行ガイド https://py.sdk.modelcontextprotocol.io/v2/migration 、fastmcp 3→4 https://gofastmcp.com/getting-started/upgrading/from-fastmcp-3 、対応表 (3.4.x=mcp<2.0/httpx、4.0.x=mcp2/httpx2排他)、#4560 closed (2026-08-18 pin戻し)。
