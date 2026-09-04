# MCP SDK v2 移行デザイン (B完全移行) — 2026-09-04 (rev2, oracle BLOCK対応)

## 背景
- 現状 `fastmcp==3.4.4` + `mcp==1.28.1` (`pyproject.toml:7 fastmcp>=3.4.0,<4.0.0`)。
- `mcp 2.0.0 (2026-07-28)` で `McpError→MCPError` 等により `mcp-server-fetch` がクラッシュ (#4560)。本番は `uvx --with "mcp<2.0.0"` 回避で稼働。
- `fastmcp 3.4.x` は `mcp>=1.24.0,<2.0` ピンのため v2対応は `fastmcp 4.x` 必須。
- ユーザー判断: `4.0.x + mcp2最新`、`v1+v2両対応`、`フル検証 + D:\Desktop\hub.config.json`、`B完全移行`。Fetch/grokの `--with` は一旦残し最期に外す (現状まだ必要)。

## 決定事項
- D1: `fastmcp>=4.0,<5.0` + `mcp>=2.0,<3.0` + `httpx2>=2.5.0排他` + `pydantic>=2.12` + `starlette>=1.0.1` + `opentelemetry-api>=1.28` + `mcp-types` exact-pin。
- D2: v1維持は `Client(mode="auto")` fallbackに委譲。Hub側分岐なし。証拠にv1上流混ぜテストを必須化 (下記Va)。
- D3: `--with` は緑化後に外否判定。fetch 2026-08-18 `mcp>=1.29,<2` pin戻し済みのため冗長化中だが mcp2完全対応は別PR中のため断定しない。
- D4: B完全移行。ただし `mcp_camelcase_compat` / `local_provider.remove_tool` / `add_transform(ToolTransform)` は src使用ゼロのため対象外に格下げ (YAGNI, #12)。

## §1 アーキ方針 (修正版)
- 起点 `pyproject.toml:7` → `>=4.0,<5.0`。`main.py:142-145 Tested against <4.0.0` → `<5.0.0` へ、`150-157 >=4.0.0警告` → `>=5.0.0警告` へ反転。`264-267 pinned to <3.5.0` 旧コメントも修正 (#9)。
- providersリセット (`proxy_manager.py:890-900 mcp.providers=[local_provider]`) の置換先に `providers=[...]` 直接代入を名乗らない。公開は `FastMCP(providers=[...])` ctor と `add_provider(provider, namespace="")` のみ。`unmount/remove_provider` は不在、`_server_instances` はshutdown terminate専用私的APIのため触らない。差替が必要なら再構築 + バージョンガードshimに隔離 (#2)。
- `create_proxy(target)` 単純置換禁止。現行の接続済み単一 `Client` を `client_factory` で返す再利用 (handshake回避+即raise+header移植温存) は `ProxyProvider(client_factory, cache_ttl)` + `ProxyClient` で継続。per-request freshは `ProxyClient.new()` (`_proxy_rc_ref` リセット) 経由、長期pinのみ `StatefulProxyClient._caches`。`Client.new()` はfresh既定 (#3)。
- `forward_incoming_headers` は transport属性直設定を廃止し `TransportOptions.forward_incoming_headers` + `PROXY_TRANSPORT_OPTIONS(True)` + `_get_forwardable_http_headers()` に移行。Cookie除外 (#4843, `get_http_headers(include={"cookie"})` がescape hatch)、消毒 (#4770)、`mcp-*` strip (#4853) を正規として受容 (#8)。
- bridge恒久依存なし。ただし `mcp_camelcase_compat` は src不存在のため言及削除 (#12)。

## §2 コンポーネント別 (Hub対応表)
- proxy/roots (#1): `proxy_manager.py:21 _proxy_providers` import + `:35 _orig_default_roots` 退避 + `:38-45 _resilient_default_roots(RuntimeError→[])` + `:48` 猿パッチ。4.xで `default_proxy_roots_handler` 存続 (proxy.py:1524) が正規転送路、frontendは `Client(roots=...)`、Hub内 `ctx.list_roots()` は削除→guard/tool引数化。方針: ハンドラ存続確認済みのためshim隔離維持、Hub内直接 `list_roots` 呼び出しがあれば削除。
- proxy/mount (#3 補): `FastMCP.mount(server, namespace=None)` が正規live合成。`prefix→namespace` 改名のみ追従。`FastMCPProxy(client_factory,name,provider_error_strategy)` 生成は `ProxyProvider` 形式に寄せ、温存3点 (再利用/error短絡/header) を落とさない。
- tag_filter (#7): `tag_filter.py:116 _server` →置換先 `ProxyTool._backend_name (proxy.py:340, model_copy初回保存)` (+`ProxyPrompt._backend_name`/`ProxyResource._backend_uri`)。`mount(namespace=)` との組合せは要実測のため shim隔離対象に格上げし、実装計画で実測タスク化。
- main (#9): `http_app(path,middleware,json_response,stateless_http,transport,event_store...)` はpath先頭順序に注意。`StreamableHTTPASGIApp(session_manager)` 存続。routes走査 (`main.py:271-274`) はmodern-era形状 (`Route(path, POST/DELETE if stateless)` + auth時 `RequireAuthMiddleware` + `HostOriginGuard`) で誤検出防止。`_mcp_server注入 (:279-282,:297-300)` + `_lifespan_manager()+sm.run() (:313-318)` はlifespan1回 (`_lifespan_proxy exactly once`) 前提で維持可否を実測。
- SM vs patch分離 (#4): SM項= `lenient_session_manager.py:22 handle_request` + `:28 _is_unknown_session` + `:24 _handle_stateless_request` + `:33 _server_instances` のみ。基底 `FastMCPStreamableHTTPSessionManager` は存続 (http.py:38)。`_handle_stateless_request` は不在→ `stateless_http=True` へ、`_cleanup_stale` (main.py:92-96呼出) は不在→ `session_idle_timeout=` へ。patch項とは分離記載。
- patch洗替 (#5): `streamable_http_patch.py:27 import httpx` →httpx2 (`:139` 型注釈含む)、`ctx.client.stream/post` httpx2型、`message.root (:127,:152)` はRootModel廃止で消滅→union+TypeAdapter化、`model_dump(by_alias...)` snake_case化、`CONTENT_TYPE_JSON/SSE` 存続、`RequestContext(httpx2.AsyncClient...)` 存続、`_maybe_extract_session_id_from_response(httpx2.Response)` 存続、`_handle_json/_handle_sse` は短縮名誤り→full名、`_send_session_terminated_error` 不在 (404時Session terminated振舞いのみ)、`_handle_unexpected` 関数なし (else分岐のみ)。各々file:line付きで実装計画へ。
- meta_provider (#6): `meta_provider.py:198 doc.get("inputSchema")` + `:434 get_schema` + `:697 getattr(t,"parameters")→"inputSchema"` + docstring `:86,:230,:284` と `full_info.py:66 schema.get("inputSchema")` は内部dictキー統一のため維持/直すかを決定。新旧対応表: Tool新フィールド vs 内部 `inputSchema` キー。FullInfo合成ごと壊すため必須項に格上げ。
- full_info訂正 (#13): `full_info.py:63-67 Tool(name,description,parameters)` + `:85,:90 ToolResult(is_error)` は既にsnake_case済み。直すのは中身 `schema.get("inputSchema")` と `TextContent(type="text")` 存否の確認に書き換え。
- echo/tests/docs (#14): `stdio_echo_server.py:6 @mcp.tool` + `sse_echo_server.py:29 @mcp.tool` は4.x記法 (`@mcp.tool` 括弧・serializer廃止) に具体化。SSEは4.x legacy-only (autoでもhandshake固定) 注記。`tests/test_admin_api.py:306 AnyUrl` はv2 `uri:str` のため修正対象。`architecture.md:308 sys.exit` 矛盾・旧ピン残骸も修正。

## §3 Hub対応表 (SDK変更誌ではなく対応表) (#10)
| SDK変更 | Hub使用箇所 | 判定 |
|---|---|---|
| McpError構築kwargs | src 0件 | 対象外・確認のみ |
| elicit/response_type必須 | src 0件 | 対象外 |
| sample/list_roots/sampling_handler | src 0件 (rootsパッチ除く) | #1のみ対応 |
| on_initialize/set_state/task=True | src 0件 | 対象外 |
| AnyUrl→str | tests/test_admin_api.py:306 のみ | 要修正 |
| RootModel→union | streamable_http_patch.py:127,152 message.root | 要修正 |
| httpx→httpx2 | streamable_http_patch.py:27,139 + RequestContext型のみ。factory/Auth/except/TLS/loggerはphantom | 該当分のみ修正 |
| streamablehttp_client削除 | コード0件 | 対象外 |
| -32002→-32602 | main.py:251 hub://servers唯一 | 影響なし・確認のみ |
| timeout float秒 | src 0件 (Client既定維持) | 対象外 |
| mount prefix→namespace | proxy_manager.py:134,179,320,900 | 要修正 |
| bridge吸収 (camelcase/McpError alias/mcp.types) | src非依存 | 直さず可 |

## §4 検証 (強化版) (#11 + リスク)
- 最優先 importゲート: `python -c "import mcp_hub.main"` (R1: routes/_mcp_server/SM基底のいずれか誤りでimport死ぬため実機より先)。
- (a) v1上流混ぜ auto-fallback証明 (D2証拠)。
- (b) `{server}_{tool}` 命名・`data://api/info` 契約テスト (Namespace transform対策)。
- (c) modern-era LenientSM stateless振舞い (json_response/stateless含む)。
- (d) httpx2下 202ポーリング実流 (async_mcp_server流用)。
- (e) `uv lock --check` + pydantic/starlette/FastAPI floor + mcp-types exact-pin + exclude-newer免除ゲート。
- (f) 実機行列: 列挙数・SSE/StreamableHTTP・Fetch/grok pin有無・meta on/off・tag・full_info (D:\Desktop\hub.config.json)。
- 既存: `scripts/run-tests.sh` 全件 (`pytest` 直禁)、型・lint・format・カバレッジ・監査はGATE通り。UIなしのため実ブラウザ対象外。
- R2 httpx grep全洗替、R3 on_message拡大+_backend_name結合テストで検出。

## 非目標
- 二系統並行なし。tasks extension新規導入なし (未使用確認のみ)。fetch/grok本体改修なし。

## 次ステップ
1. 本rev2の #081 再審 (PASS必須)。
2. `writing-plans` で実装計画化。
3. 実装後 #081 REVIEW→GATE→COMMIT。

## 出典
- #009初回 + BLOCK裏取り (file:line確定分)。
- #042初回 + 4.x正規手段確定 (24ソース、mount/add_provider/create_proxy/Client.new/ProxyClient/roots/forward headers/SM/http_app/lifespan/bridge表、test_upgrade_from_v3.py最終審)。
- #081 BLOCK 14件 (2e8e694審)。
