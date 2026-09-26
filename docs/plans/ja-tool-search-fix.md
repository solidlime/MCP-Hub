# 日本語ツール検索の修正計画

- 日付: 2026-09-26 / 対象: MCP-Hub / project: mcp-hub
- 起案: ヘルタ（オーケストレーター） / 実装: #011 / 調査: #042（librarian）・#009（Explore）

## 1. 背景（確定した根因）

| # | 層 | 事実 | 参照 |
|---|----|------|------|
| ① | 埋め込み | デフォルトが英語専用 `sentence-transformers/all-MiniLM-L6-v2`（NAS live も同値、`use_embeddings=true`） | `src/mcp_hub/config.py:15` |
| ② | 字句 | トークナイザがラテン限定 `re.findall(r"[a-zA-Z0-9]+", word)` → 日本語トークン0 | `src/mcp_hub/meta_provider.py:226` |
| ③ | 融合 | BM25+semantic を RRF(k=60) 合成（`score = Σ 1/(60+rank)`）。床 `_SEMANTIC_FLOOR=0.30` をデタラメ埋め込みが通ると `1/61 = 0.016` が上位に出る | `src/mcp_hub/meta_provider.py:124-127, 417-462, 493` |

実測: NAS/ローカルとも「天気」→ No matching tools、「照明」→ 無関係な 0.016。0.032 は RRF 2 項分。

## 2. ゴール / 非ゴール

- ゴール: 日本語クエリで正しいツールが返る（正規化 + 多言語セマンティック）。英字の既存挙動に回帰なし。
- 非ゴール: `emit()` dedup 撤去（要否は別途。今回は備考コメントのみ）、bge-m3 / e5-large / jina-v3 採用、API/UI 変更、Docker 変更。

## 3. 設計

### 3.1 多言語埋め込み

**第1候補: `intfloat/multilingual-e5-small`**（384dim / MIT / JMTEB 総合 67.71）

- fastembed 0.8.0（`uv.lock:461-462`）の**標準サポート外**。`TextEmbedding.add_custom_model(model="intfloat/multilingual-e5-small", pooling=PoolingType.MEAN, normalization=True, sources=ModelSource(hf="intfloat/multilingual-e5-small"), dim=384, model_file="onnx/model.onnx")` を **`_SUPPORTED_MODELS_CACHE` 生成（`src/mcp_hub/meta_provider.py:82`）より前に 1 回だけ**実行する（登録順序を誤ると `resolve_embedding_model()` が既定へフォールバック）。
- **必須プレフィックス**: クエリ `"query: "` / 文書 `"passage: "`。fastembed は自動付与しない。文書側 = `src/mcp_hub/meta_provider.py:318-322`、クエリ側 = `:497-501`。定数化しモデルプロファイル経由で適用（片側だけ直す事故防止）。
- **床の再校正**: E5 の余弦類似度は概ね 0.7〜1.0 に分布 → `_SEMANTIC_FLOOR`（`src/mcp_hub/meta_provider.py:124`）は実測で再校正し、値と根拠を実装レポートに記録。テストは fixed embedder で決定化する。
- モデルプロファイルは小さな dict で表現（例: `{model_id: {query_prefix, passage_prefix, semantic_floor}}`、未定義は `{None, None, 0.30}`）。過剰な抽象化はしない。
- `DEFAULT_EMBEDDING_MODEL`（`src/mcp_hub/config.py:15`）を採用モデルへ変更。dim は 384 のままなので `_embeddings` 形状は不変。

**フォールバック: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`**（標準サポート / 0.22GB / apache-2.0 / 384dim / プレフィックス不要）

- STEP 0（下記）で e5-small の登録・実証が失敗した場合に切替。切替理由と実測を記録。フォールバック時も床は実測再校正。

### 3.2 CJK トークナイザ（`src/mcp_hub/meta_provider.py:189-244 _tokenize`）

- 正規化: **NFKC → ひらがな→カタカナ（U+3041–U+3096 を +0x60）→ casefold → 既存 ASCII 経路（切り出し/stemming/stopwords）温存**。
- CJK 連続区間 `[\u3041-\u3096\u30a1-\u30fa\u30fc\u3400-\u9fff\uf900-\ufaff\u3005]+` に対し **bigram（記号・空白で窓を切る）+ unigram** を追加。日本語ストップワードは入れない。
- ⚠ #042 の参考スニペットは未実行で `読み込み` の期待トークン数が自己矛盾（6 vs 7）——参考のみ。**テストを先に書き、期待トークン列を明示**してから実装。
- `_name_tokens` / name-match promotion（`:365` 付近）は既存テストで回帰確認。
- 備考: `emit()`（`:213-217`）の dedup により BM25 の tf は実質二値化される。今回の修正範囲外。必要なら後続で検討（TODO コメントを残すかは実装判断）。

### 3.3 テスト（追加/更新）

- tokenizer 単体: カタカナ/ひらがな統一、半角カナ、全角英数（NFKC）、句読点境界、casefold、混在文、空文字 → 期待トークン列を明示。
- 検索統合: 日本語説明を持つ fixture サーバーで「天気」「照明」「ふぁいる」等がヒット。英字クエリの回帰。
- モデル解決: デフォルトが多言語モデル / プレフィックスが embed 入力に付く（`tests/test_tool_index.py` の fixed embedder 期待値を更新）/ 床未満除外。
- fastembed 不在環境で skip もしくは BM25-only に degrade して壊れない（`tests/test_fix6.py` の系譜）。

### 3.4 ドキュメント同期

- `docs/configuration.md`（embedding_model デフォルトと多言語化の説明）
- `docs/architecture.md`（検索パイプライン: 正規化・bigram・RRF・プレフィックス）
- `README.md`（埋め込みモデル記述がある箇所のみ最小限）

本計画書も docs として同時にコミットする。

## 4. 実装手順（#011 向け）

1. **STEP 0（環境と実証）**: `uv sync --extra embeddings`（または uv pip、lock の fastembed 0.8.0 を尊重）で `.venv` に導入 → `python -c "from fastembed import TextEmbedding; print([m['model'] for m in TextEmbedding.list_supported_models()])"` と `add_custom_model` の存在確認 → e5-small の登録・ダウンロード・embed の PoC。失敗時はフォールバックモデルに切替（理由記録）。
2. **STEP 1**: tokenizer テスト先行 → 実装。
3. **STEP 2**: モデルプロファイル / プレフィックス / 床 → 実装・実測校正。
4. **STEP 3**: config デフォルト & docs 同期。
5. **STEP 4**: `scripts/run-tests.sh`（フル。個別は `TEST_FILE=`）、`uv run ruff check src/ tests/`、`uv run ruff format --check`、`uv run mypy src/ --ignore-missing-imports`。
6. **STEP 5**: ローカル実機確認（§5）。
7. 提出物: acceptance-report（変更ファイル / 実行コマンド / テスト結果 / 校正値 / 未確認事項 / 残リスク）。**コミットはしない**（GATE 後に本体が行う）。

## 5. ローカル実機確認（before / after）

- 旧スモークサーバーが `/tmp/hub-smoke/hub.pid` に残っていれば **PID 指定で kill** してから起動する。
- 起動: `cd /root/workspace/MCP-Hub && setsid .venv/bin/python -m mcp_hub.main >/tmp/hub-smoke/hub.log 2>&1 & echo $! >/tmp/hub-smoke/hub.pid` → `curl -sf http://127.0.0.1:26263/admin/api/health` をポーリング（非同期起動・ログは末尾のみ）。
- 検証: `fastmcp.Client`（`.venv` 内）で `http://127.0.0.1:26263/mcp` の `search_tools` を直叩き。
  - before（確立済み）: 「天気」「照明」→ No matching tools。「file」→ filesystem.read_file。
  - after: ローカルカタログ（filesystem / sequential-thinking / puppeteer / brave-search / youtube / j-quants-doc-mcp）に対応する日本語クエリでヒット確認（例: j-quants 系の日本語説明、「ファイル」→ filesystem を semantic で）。
- 終了時は PID 指定で kill（clean state）。ログ末尾のみ確認。

## 6. GATE（本体）

`ruff check src/ tests/` pass / `ruff format --check` pass / `mypy src/ --ignore-missing-imports` pass / `scripts/run-tests.sh` 失敗 0 / ドキュメント同期 / 禁止操作なし。

- カバレッジ・契約テスト・監査は本 repo にツールが無いため N/A（CI と同一コマンド `ci.yml:36,70,72` で担保）。
- #003 レビュー PASS 後にコミット（≤100 行/commit、`fix:` / `feat:` / `docs:`）。
- その後 nous へ RECORD（`project:mcp-hub` + `task_state`）。

## 7. NAS デプロイ（後フェーズ）

- NAS は別マシン。反映方法（Docker / uv / 直接起動）を確認して手順化。
- `embedding_model` が保存済みなら admin API PATCH（`/admin/api/settings/embedding-model`）が必要。初回はモデルダウンロード（~0.5GB）とメモリ実測、`/admin/api/settings/embedding-model` で確認。

## 8. リスクと根拠

- **fastembed バージョン差**: `src/mcp_hub/bootstrap.py:99` が無指定インストール → 実行環境で版が異なり得る。capability 検出（`add_custom_model` 有無）でフォールバック。
- **e5-small の ONNX 実在**は fastembed 公式 README の例に依拠（STEP 0 で実証）。
- **床の値**は実測依存。テストは fixed embedder で決定化。
- **メモリ**: 本機は 7GB 級。テストは `scripts/run-tests.sh` の 3G cgroup 隔離で実行（直 pytest 禁止）。

## 9. 出典（抜粋）

- fastembed Supported Models / README（`add_custom_model` 例） / v0.8.0 ソース（prefix 非付与、mean pooling 警告）
- intfloat/multilingual-e5-small HF model card（prefix 必須・ja 対応・MIT）、JMTEB leaderboard
- Lucene `CJKAnalyzer` / `CJKBigramFilter`、Elasticsearch `cjk_bigram`、Unicode UAX #15（NFKC）、`rank_bm25`（負 IDF 床）
- 詳細と URL 一覧は #042 調査メモ（セッション記録）参照
