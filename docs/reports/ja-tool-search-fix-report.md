# 日本語ツール検索の修正 — 実装レポート

- 日付: 2026-09-26 / 実装: ヘルタ人形 #011 / 計画: `docs/plans/ja-tool-search-fix.md`
- 状態: **実装・単体テスト・実機 before/after 完了**（コミットは未実施＝GATE 後に本体）

## 1. 変更ファイル一覧

| パス | 変更概要 |
|---|---|
| `src/mcp_hub/meta_provider.py` | CJK トークナイザ、モデルプロファイル（prefix/floor）、カスタムモデル登録 |
| `src/mcp_hub/config.py` | `DEFAULT_EMBEDDING_MODEL` を多言語 e5-small に変更 |
| `src/mcp_hub/admin_router.py` | エラーメッセージ内の既定モデル例を更新 |
| `src/mcp_hub/static/index.html` | 埋め込みモデル入力の placeholder 文字列のみ更新（挙動変更なし） |
| `tests/test_tool_index.py` | tokenizer / モデルプロファイル / 日本語検索テスト追加、既定モデルテスト更新 |
| `docs/configuration.md` | 既定モデル・多言語・prefix の説明 |
| `docs/architecture.md` | 検索パイプライン（正規化 / bigram / RRF / prefix / floor）更新 |
| `docs/api-reference.md` | 設定レスポンス例の既定モデル更新 |

diff: 8 files changed, 349 insertions(+), 27 deletions(-)（`docs/reports/` を除く）。

## 2. STEP 0 — 環境とモデル実証（実測）

- `uv sync --extra embeddings` → `+ fastembed==0.8.0`（uv.lock の pin 通り）。onnxruntime 1.27.0 他 17 パッケージ導入。
- 標準サポートモデル: **30 件**。`intfloat/multilingual-e5-small` は**標準リストに無い**ことを確認。
  - 標準には `intfloat/multilingual-e5-large` / `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` が存在。
- `TextEmbedding.add_custom_model` 存在確認 = `True`。登録後 `in list now: True`。
- 登録コードで実際に埋め込みが計算できた（`onnx/model.onnx` DL 済み、`/tmp/fastembed_cache` 465MB）。
- **フォールバックは不要**：第1候補 e5-small の登録・DL・embed がすべて成功したため `paraphrase-multilingual-MiniLM-L12-v2` には切り替えていない。

## 3. 床（semantic floor）の校正 — 実測と根拠

PoC 実測（`query: ` / `passage: ` プレフィックスあり、実ツール相当の文書 16 件）:

| クエリ | 関連トップ | 値 | 対応ツール無しケース |
|---|---|---|---|
| ファイルを読む | filesystem/read_file | 0.8463 | — |
| 株価を取得 | j-quants/get_stock_price | 0.8342 | — |
| 動画を検索 | youtube/search_videos | 0.8464 | — |
| webで検索 | brave/brave_web_search | 0.8455 | — |
| ページをクリック | puppeteer/puppeteer_click | 0.8177 | — |
| think step by step | sequential/sequentialthinking | 0.8406 | — |
| スクリーンショットを撮る | puppeteer/puppeteer_screenshot | 0.7961 | — |
| ディレクトリ一覧 | filesystem/list_directory | 0.7994 | — |
| 天気 | （該当なし） | — | top-1 **0.7755** |
| 照明 | （該当なし） | — | top-1 **0.7838** |

- 全 query×doc 分布: min **0.7010** / p25 0.7595 / median 0.7768 / p75 0.7914 / max 0.8464。
- **重要な限界（誠実な記録）**: 関連クエリの top-1（0.796〜0.846）と、対応ツールが存在しないクエリの top-1（0.776〜0.784）はほぼ重なる。**どの絶対閾値でも関連/無関係を分離できない。**
- 採用値: E5 プロファイルの `semantic_floor = 0.75`。
  - 根拠: 実測最小 0.7010 より上に置き、**「退化/失敗した埋め込み」を落とす低位の床**としてのみ機能させる。
  - 0.30 が E5 では「事実上の全通過」で無意味だったのは正しいが、**0.75 でも分離力は無い**。実際に分離しているのは RRF の順位（`1/(60+rank)`）である。この限界は `meta_provider.py` のプロファイル定義コメントにも明記。
  - 未定義モデル（従来 MiniLM 等）は従来互換の `0.30` のまま（既存テストで回帰確認）。

## 4. 実装内容

### 4.1 CJK トークナイザ（`ToolIndex._tokenize`）

- 正規化: `NFKC` → ひらがな(U+3041–U+3096)を +0x60 でカタカナ化 → 既存 ASCII 経路（全体語保持 / 非英数分割 / camelCase / digit 境界）温存。
- CJK 連続区間 `[\u3041-\u3096\u30a1-\u30fa\u30fc\u3400-\u9fff\uf900-\ufaff\u3005]+` に **全体 + bigram + unigram**（`_CJK_RUN`）。記号・空白は窓を切る。
- 日本語ストップワードは入れない。
- **計画からの意図的な逸脱**: 計画は「NFKC → かな統一 → casefold → 既存 ASCII 経路」だが、テキスト全体を先に casefold すると camelCase 分割（`getHTTPResponse` → get/http/response）が壊れる（既存回帰テストが要求）。そのため casefold は `emit()` 内の**トークン単位**に適用した（`str.lower()` → `str.casefold()`）。これにより `Straße`/`STRASSE` が同一トークンに揃う。

期待トークン列（#042 参考スニペットの 6 vs 7 矛盾を解消し、テストで明示）:

```
_tokenize("読み込み") == ["読ミ込ミ","読ミ","ミ込","込ミ","読","ミ","込"]   # 7 個
_tokenize("ファイル") == ["ファイル","ファ","ァイ","イル","フ","ァ","イ","ル"]
_tokenize("ふぁいる") == _tokenize("ファイル")   # かな統一
_tokenize("ﾃﾞｰﾀ")   == ["データ","デー","ータ","デ","ー","タ"]   # 半角カナ NFKC
_tokenize("ＦＩＬＥ") == ["file"]                          # 全角英数 NFKC
```

### 4.2 モデルプロファイル（prefix / floor）

- `_MODEL_PROFILES[model] = {query_prefix, passage_prefix, semantic_floor}`。未定義は `_DEFAULT_PROFILE = {None, None, 0.30}`。
- 文書側 prefix は `_embed_docs_blocking`、クエリ側は `_semantic_search` に**両方とも**プロファイル経由で適用（片側だけ直す事故防止）。
- `ToolIndex.__init__` で解決済みモデルから `self._profile` を確定。床判定もプロファイル値を使用。

### 4.3 カスタムモデル登録

- `_register_custom_models()` を **モジュール import 時に 1 回**実行（`_SUPPORTED_MODELS_CACHE` 生成より前）。
- 旧 fastembed（`add_custom_model` 無し）では例外を握ってログのみ。その場合 `resolve_embedding_model` は既定へ、embed 失敗時は BM25 にフォールバック。

### 4.4 config

- `DEFAULT_EMBEDDING_MODEL = "intfloat/multilingual-e5-small"`（dim は 384 のままなので `_embeddings` 形状は不変）。
- `MCP_HUB_EMBEDDING` の既存挙動（ハードキル）は変更なし。

## 5. 実機確認（before / after）

手順: 旧スモーク（`/tmp/hub-smoke/hub.pid` の PID 6396）は既に defunct（ゾンビ）でポート未使用を確認。ポート 26263 が空いた状態から、
`MCP_HUB_DATA_DIR=/tmp/hub-smoke/data setsid .venv/bin/python -m mcp_hub.main >/tmp/hub-smoke/hub.log 2>&1 &` で起動し、
`curl -sf http://127.0.0.1:26263/admin/api/health` をポーリング、`fastmcp.Client`（4.0.2）で `search_tools` を直叩き。
`MCP_HUB_DATA_DIR` を一時ディレクトリに向けたのは、リポジトリの `data/hub.config.json`（16 サーバー、ネットワーク依存）ではなく、
ルート `hub.config.json` のローカル 5 サーバーを seed させるため（**リポジトリの設定ファイルは一切変更していない**）。
終了は毎回 PID 指定で kill し、ポートが下りたことを確認済み。

カタログ: filesystem(14) / puppeteer(7) / sequential-thinking(1) / youtube(1) = 23 ツール。
brave-search は `BRAVE_API_KEY` 未設定で接続失敗（`status: error`）。

### before（修正前ソースに一時退避して起動・英語 MiniLM を使用）

```
[BEFORE] '天気' -> {'message': 'No matching tools found', ...}
[BEFORE] '照明' -> {'message': 'No matching tools found', ...}
[BEFORE] 'ファイルを読む' -> {'message': 'No matching tools found', ...}
[BEFORE] 'ファイル' -> {'message': 'No matching tools found', ...}
[BEFORE] 'スクリーンショットを撮る' -> {'message': 'No matching tools found', ...}
[BEFORE] '動画を検索' -> {'message': 'No matching tools found', ...}
[BEFORE] 'web検索' -> {'message': 'No matching tools found', ...}
[BEFORE] 'file' -> [('filesystem/write_file', 0.0325), ('filesystem/read_file', 0.032), ('filesystem/read_text_file', 0.0313)]
```

### after（修正後ソース・多言語 e5-small）

```
[AFTER] '天気' -> [('puppeteer/puppeteer_hover', 0.0164), ('puppeteer/puppeteer_navigate', 0.0161), ('sequential-thinking/sequentialthinking', 0.0159)]
[AFTER] '照明' -> [('puppeteer/puppeteer_hover', 0.0164), ('puppeteer/puppeteer_navigate', 0.0161), ('puppeteer/puppeteer_click', 0.0159)]
[AFTER] 'ファイルを読む' -> [('filesystem/read_text_file', 0.0164), ('filesystem/read_file', 0.0161), ('filesystem/read_media_file', 0.0159)]
[AFTER] 'ファイル' -> [('filesystem/read_media_file', 0.0164), ('filesystem/read_file', 0.0161), ('filesystem/get_file_info', 0.0159)]
[AFTER] 'スクリーンショットを撮る' -> [('puppeteer/puppeteer_screenshot', 0.0164), ('puppeteer/puppeteer_evaluate', 0.0161), ('sequential-thinking/sequentialthinking', 0.0159)]
[AFTER] 'file' -> [('filesystem/read_file', 0.0328), ('filesystem/read_media_file', 0.032), ('filesystem/write_file', 0.0318)]
```

追加確認（after、別クエリセット）:

```
'動画'                 -> [('youtube/download_youtube_url', 0.0164), ...]
'動画を取得'           -> [('youtube/download_youtube_url', 0.0164), ...]
'スクリーンショット'   -> [('puppeteer/puppeteer_screenshot', 0.0164), ...]
'思考'                 -> [('sequential-thinking/sequentialthinking', 0.0164), ...]
'ファイルの中身を表示' -> [('filesystem/get_file_info', 0.0164), ('filesystem/search_files', 0.0161), ('filesystem/read_file', 0.0159), ...]
'ブラウザでページを開く' -> [('puppeteer/puppeteer_click', 0.0164), ('puppeteer/puppeteer_hover', 0.0161), ('puppeteer/puppeteer_navigate', 0.0159), ...]
'youtube'              -> [('youtube/download_youtube_url', 3.1355), ...]   # name-match promotion
'天気予報'             -> [('puppeteer/puppeteer_hover', 0.0164), ...]      # 該当ツール無し
```

**結論**: 日本語クエリで正しいツールが返る（`ファイル`→filesystem, `スクリーンショット`→puppeteer, `動画`→youtube, `思考`→sequential-thinking）。英語クエリ（`file`）は回帰なし。
「天気」「照明」はカタログに該当ツールが存在しないため意味的最近傍のノイズを返す（§3 の閾値限界どおり。原理的にゼロにできない）。

## 6. テスト・lint（実出力）

| コマンド | 結果 |
|---|---|
| `TEST_FILE=tests/test_tool_index.py ./scripts/run-tests.sh` | `60 passed`（RED: `model_profile` ImportError → GREEN を確認） |
| `./scripts/run-tests.sh`（フル） | `ran=30 failed=0` |
| `uv run ruff check src/ tests/` | `[]`（エラーなし） |
| `uv run mypy src/ --ignore-missing-imports` | エラー 0（`main.py:94` の annotation note のみ） |
| `uv run ruff format --check` | **46 files need formatting**（下記・既存） |

- TDD: 先にテストを追加し、ソースを一時退避して **RED**（旧トークナイザで `読み込み -> ['読み込み']`＝bigram 無し／`model_profile` ImportError）を確認してから実装、その後 GREEN。
- `ruff format --check` は HEAD 時点でも同 3 ファイルを含む 46 ファイルが未整形（`git show HEAD:...` を退避して確認済み）。CI（`.github/workflows/ci.yml:70-72`）は `ruff check` と `mypy` のみを実行し format 検査はしていない。**リポジトリ全体の整形はスコープ外**のため未実施（テストを通すための改変ではない）。

## 7. 未確認事項・残リスク

- NAS 反映は別フェーズ。保存済み `embedding_model` がある環境では admin PATCH（`/admin/api/settings/embedding-model`）が必要。初回はモデル DL が必要。
- 対応ツールが存在しない日本語クエリ（例: 「天気」「照明」）は多言語埋め込みでも意味的最近傍を返す。閾値では分離できないため原理的にゼロにできない（§3）。
- 旧 fastembed（`add_custom_model` 無し）環境では既定 e5-small をロードできず BM25 に落ちる。`uv.lock` は 0.8.0 を pin しているが `bootstrap.py` は無指定インストールのため実行環境依存の余地が残る（capability 検出で degrade はする）。
- `index.html` の placeholder 変更は grep による文字列確認のみ（ブラウザ実機確認は #011 では不可）。UI の挙動・仕様は変更していない。

## 8. Drive-by Findings

- `src/mcp_hub/meta_provider.py:213-217` — minor — `emit()` の重複除去により BM25 の tf が実質二値化される（`_build_doc_tokens` のフィールド重み ×5/×3/×2 は文書ごとに 1 に潰れる）。今回の修正範囲外。
- `src/mcp_hub/bootstrap.py:99` — minor — `uv pip install fastembed` がバージョン無指定。実行環境で版がずれ得る（マルチリンガル既定モデルの登録可否に影響）。
- `.github/workflows/ci.yml:70-72` — trivial — `ruff format --check` が CI に無く、リポジトリは format 未整形（46 ファイル）。GATE 条件と CI 実態の乖離。

## 9. ポストレビュー修正（#003 の BLOCK 対応 / #011）

#003 敵対的レビュー（`/tmp/mcp-hub-review-003d.md`）の BLOCK/M 項目のうち割当分を修正した。

| 指摘 | 内容 | 対応 | 実測証拠 |
|---|---|---|---|
| B1 | `model_profile()` が大小文字を区別／`resolve_embedding_model()` が生入力を返し `TextEmbedding()` が KeyError→埋め込み恒久停止 | `model_profile()` を `.lower()` 引きに。`_supported_embedding_models()` を `{lower: canonical}` に変更し `resolve_embedding_model()` が登録カノニカル名を返すように | 下記スニペット実出力：`model_profile("INTFLOAT/MULTILINGUAL-E5-SMALL") -> {query_prefix: "query: ", passage_prefix: "passage: ", semantic_floor: 0.75}`、`resolve_embedding_model("INTFLOAT/MULTILINGUAL-E5-SMALL", supported) -> "intfloat/multilingual-e5-small"`（31 モデル登録＝KeyError 経路消滅） |
| B2 | E5 ファミリ（`intfloat/multilingual-e5-large` 等）で prefix を無警告喪失／docs がコードより広い約束 | `intfloat/` 配下かつ名前に `e5` を含む（大小無視）モデルに E5 プロファイルを適用。floor は明示プロファイル優先（e5-small=0.75）、ファミリ既定 0.30。docs を実装に合わせて正確化 | `model_profile("intfloat/multilingual-e5-large") -> {query_prefix: "query: ", semantic_floor: 0.30}`。単体テスト `tests/test_tool_index.py::TestModelProfile::test_e5_family_model_gets_prefixes` 他（実モデル DL なし） |
| B3 | `docs/api-reference.md` の「次回の `rebuild_index()` から反映」は誤り | 「次回のサーバー再起動後に反映（PATCH は保存のみ・rebuild しない）」に訂正。WebUI hint（`static/index.html:1935`「変更は再起動後に反映されます」）と整合 | `admin_router.py:271-282` PATCH は `registry.set_embedding_model()` のみで rebuild 無しをコード確認 |
| B4 | 移行手順が docs に無い（保存済み `embedding_model` が優先され既定変更だけでは直らない） | `docs/configuration.md` に「埋め込みモデルの移行」節を追加（保存値優先／WebUI or PATCH で変更／再起動で反映／初回 ~450MB DL） | `docs/configuration.md` 参照 |
| M1 | カスタムモデル登録失敗が `logger.debug`／`_CUSTOM_MODELS_REGISTERED` を try 前に立てる | `logger.warning` に。フラグは try 内で**成功後に**立てる（例外時に再試行機会を残す） | `meta_provider.py` `_register_custom_models()` |
| M2 | 今回の diff が ruff format の新規違反を追加 | meta_provider の EOF 呼び出し前空行／新規テストの 1 行 dict リテラル 3 箇所のみ整形。既存逸脱は不変 | `ruff format --diff` の内容を HEAD 版と比較し、meta_provider は型注釈変更を除き完全一致、test は完全一致＝**新規逸脱 0** |
| N1 | `_CJK_RUN` が BMP のみ（𠮟 等が字句トークン 0） | 漢字拡張B面 U+20000–U+2FFFF を追加 | `ToolIndex._tokenize("𠮟") == ["𠮟"]`（`test_cjk_extension_b_run`） |
| N5 | `docs/api-reference.md` の PATCH 例が旧既定モデルのまま | 例を `intfloat/multilingual-e5-small` に更新＋「任意の fastembed 対応モデル」を補足 | `docs/api-reference.md` 参照 |

### B4-2: 埋め込みインデックスのディスク永続化確認（レビュー外・追加依頼）

- **結論：永続化されない（インメモリのみ）。モデル変更で古いインデックスが再利用される経路は存在しないため、キャッシュ削除は不要。**
- 根拠（コード確認）:
  - `ToolIndex._embeddings` への代入は `rebuild()` 内の 3 箇所のみ（`meta_provider.py:469` 計算結果、`:476`/`:479` 例外時 None）。コンストラクタは `None` 初期化のみ。
  - `src/mcp_hub/` 全体で `np.save`/`savez`/`pickle`/`joblib`/`.npy`/`.npz`/`.faiss` 等の埋め込み永続化は **0 件**（grep）。
  - `meta_provider.py` は `open()`/`Path()`/`write_*` を一切呼ばない（埋め込みに限らずファイル I/O 無し）。
  - `main.py:313` が起動時に `await meta_app.rebuild_index()` を実行し、`ToolIndex(embedding_model=...)` は起動時設定で構築 → 毎プロセス再計算。したがってモデルを変えて再起動すれば必ず新モデルで再埋め込みされる（古いベクトルは存在しない）。
- 補足: fastembed 自身は**モデル重み**を HF キャッシュ（`/tmp/fastembed_cache` 等）に永続化する。これはモデル名でキーされ別モデルと混ざらないため、これも削除不要。

### N2（unigram ノイズ）注意書き

- CJK unigram は既知のコストを持つ：クエリ「ファイル」の unigram `ル` が「〜する」系説明（例 `天気予報を取得する` → `スル`）と衝突しうる。順位は RRF/IDF が守るが、字句一致のみでは誤ヒットが混じる前提で扱うこと（意味検索併用時は実質無視できる）。

### ポストレビュー修正の検証（実出力）

```
$ .venv/bin/python -m pytest tests/test_tool_index.py -q
65 passed in 3.34s

$ .venv/bin/python -m ruff check src/ tests/
All checks passed!

$ .venv/bin/python -m mypy src/ --ignore-missing-imports
Success: no issues found in 19 source files
(src/mcp_hub/main.py:94: note: By default the bodies of untyped functions are not checked ... ← 既存 note のみ)

$ ruff format --diff src/mcp_hub/meta_provider.py tests/test_tool_index.py
# HEAD 版と内容比較 → meta_provider は型注釈変更(set[str]→dict[str,str])を除き一致、
# test_tool_index は完全一致 = 新規逸脱 0（残りは HEAD 時点からの既存逸脱のみ）
```
