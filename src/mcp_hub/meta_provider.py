"""
Progressive Discovery meta-tools with BM25 search.
Exposes 3 tools instead of all child server tools.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import FastMCP
from rank_bm25 import BM25Okapi

from .config import DEFAULT_EMBEDDING_MODEL
from .embedders import (
    ORT_SUPPORTED_MODELS,
    canonical_ort_model,
    create_embedder,
    ort_model_spec,
)
from .proxy_manager import ProxyManager as _ProxyManager
from .state import request_tags
from .state import tags_match as _tags_match

try:
    from fastembed import TextEmbedding

    _HAS_FASTEMBED = True
except ImportError:
    _HAS_FASTEMBED = False

# numpy は fastembed と別に束縛する（CI は fastembed 無しでも rank_bm25 経由で
# numpy が入り、テストが _FixedEmbedder を注入するため）。fastembed の try 内で
# 一緒に import すると fastembed 欠落時に np も未定義になり NameError で BM25 に
# 落ちて CI だけ失敗する（実際起きた）。
try:
    import numpy as np

    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

logger = logging.getLogger(__name__)

# execute_tool の server-not-found エラーで返す available_servers の上限。
# 全列挙は応答を肥大させるため先頭のみ + 残数を返す（案E）。
_MAX_LISTED_SERVERS = 10

# search_tools の description に焼き込むサーバーカタログの上限。
_CATALOG_MAX_CHARS = 1200
_MAX_CATALOG_TOOL_NAMES = 8
_CATALOG_LINE_MAX_CHARS = 240

# 埋め込みバッチサイズ。fastembed の既定 256 だと 227 件カタログが 1 バッチに
# なり、max_length=512 固定パディングの attention 行列 (227, 12, 512, 512) fp32
# ≈ 2.86GB を一括確保して low-memory 環境で OOM する。8 なら 8×12×512²×4
# ≈ 100MB/バッチ。
_EMBED_BATCH_SIZE = 8

# 文書テキスト（rebuild が _embed_docs_blocking に渡す f"{server}/{name} [tags]: desc"）
# の組み立て形式のバージョン。形式を変えたら必ず bump すること — 忘れると古い
# 形式で作った埋め込みがキャッシュ（キーはモデル名/次元/prefix/TEXT_FMT_VERSION）
# から返り、意味のずれたベクトルで検索が静かに劣化する。
TEXT_FMT_VERSION = 1

# 文書単位埋め込みディスクキャッシュの置き場（A1）。
# MCP_HUB_EMBED_CACHE_DIR で上書き可（テストは tmpdir を指定して隔離する）。
_DEFAULT_EMBED_CACHE_DIR = os.path.join(
    os.path.expanduser("~"), ".cache", "mcp-hub", "embeddings"
)

# 索引テキストに載せるツール説明の上限。長い英語説明は mean pooling を薄め、
# 埋め込みコストも増やす。400 字で切るとサーバー説明（日本語）が効く。
_INDEX_DESC_CHARS = 400

# search_tools / get_schema が表示するツール説明の上限（索引テキストとは別物）。
# 索引は 400 字で切るが、表示は LLM に渡すリッチな情報なので長め。切り詰めた
# 時だけ末尾に "…" を付けて省略を明示する。
_DISPLAY_DESC_CHARS = 600

# search() の 1 回で返す最大件数。LLM が巨大な top_k を渡すと schema 込みの
# 結果 JSON が無制限に肥大しコンテキストを浪費するため、ここで頭打ちにする。
# 75 件列挙のような正当な全量取得はほぼ許す上限として 50。
_MAX_TOP_K = 50

MCP_HUB_TAGS_HEADER = "X-MCP-Hub-Tags"


def _build_catalog(server_entries: list[dict]) -> str:
    """サーバー一覧の一行カタログを組む（search_tools の導線用）。

    各 entry は {"server": str, "description": str, "tools": list[str]}。
    description があればそれを使い、無ければツール名（ソート・先頭8件、
    超過は +N more）にフォールバックする。サーバー名 asc。ツール 0 件は
    "(no tools listed)"。行単位で積み _CATALOG_MAX_CHARS を超えたら
    "... and N more servers" で打ち切る。
    """
    lines: list[str] = []
    for entry in sorted(server_entries, key=lambda e: e["server"]):
        name = entry["server"]
        desc = entry.get("description") or ""
        if desc:
            line = f"- {name}: {desc}"
        else:
            tools = sorted(entry.get("tools") or [])
            if not tools:
                line = f"- {name}: (no tools listed)"
            else:
                listed = ", ".join(tools[:_MAX_CATALOG_TOOL_NAMES])
                extra = len(tools) - _MAX_CATALOG_TOOL_NAMES
                if extra > 0:
                    listed += f" +{extra} more"
                line = f"- {name}: tools: {listed}"
        lines.append(line[:_CATALOG_LINE_MAX_CHARS])

    out = ""
    for i, line in enumerate(lines):
        if len(out) + len(line) + 1 > _CATALOG_MAX_CHARS:
            out += f"\n... and {len(lines) - i} more servers"
            break
        out += line + "\n"
    return out.rstrip("\n")

_SUPPORTED_MODELS_CACHE: dict[str, str] | None = None


def _supported_embedding_models() -> dict[str, str] | None:
    """既知モデルの {lowercase: 登録カノニカル名} マップ。

    fastembed の登録リスト（取得できた場合）に ORT 経路モデル（ruri 等）を重ねる。
    ruri は fastembed 非対応でも Hub にとっては「既知」なので、fastembed 不在の
    環境でも None ではなく ORT 分だけの dict を返す（既定モデルの解決と status 表示
    を fastembed の有無に依存させない）。

    登録名そのもの（大文字混じり）を値に持つ。TextEmbedding() は登録名で引くため、
    resolve_embedding_model() は生の入力ではなくこのカノニカル名を返す必要がある。
    """
    global _SUPPORTED_MODELS_CACHE
    if _SUPPORTED_MODELS_CACHE is None:
        fastembed_models: dict[str, str] = {}
        try:
            if _HAS_FASTEMBED:
                # list_supported_models() は dict のリストを返す（各要素に "model" キー）
                fastembed_models = {
                    m["model"].lower(): m["model"]
                    for m in TextEmbedding.list_supported_models()
                }  # type: ignore[name-defined]
        except Exception:
            logger.debug("Could not query fastembed supported models", exc_info=True)
        # Hub 自身が静的に登録する独自モデル（e5-small 等）。fastembed 不在時は
        # list_supported_models() に出てこないため、ここで既知に含める。含めないと
        # resolve_embedding_model() が明示指定を「未知」と誤判定して既定 ruri へ
        # 書き換え、e5 の prefix/floor プロファイルが失われる（実際 CI で起きた）。
        custom_models = {
            spec["model"].lower(): spec["model"]
            for spec in _CUSTOM_EMBEDDING_MODELS
        }
        _SUPPORTED_MODELS_CACHE = {
            **custom_models,
            **fastembed_models,
            **ORT_SUPPORTED_MODELS,
        }
    return _SUPPORTED_MODELS_CACHE


# 多言語モデル intfloat/multilingual-e5-small は fastembed 0.8.0 の標準リストに
# 無い（確認済み）。add_custom_model で登録しないと resolve_embedding_model() が
# 英語既定へ黙ってフォールバックする。登録はメタデータのみ（DL は embed 時）。
# ruri-v3-30m（既定）はここではなく embedders.ORT_MODELS に登録する ——
# ModernBERT 系は fastembed で読めず、ONNX Runtime 経路で扱うため。
_CUSTOM_EMBEDDING_MODELS: tuple[dict[str, Any], ...] = (
    {
        "model": "intfloat/multilingual-e5-small",
        "dim": 384,
        "model_file": "onnx/model.onnx",
    },
)
_CUSTOM_MODELS_REGISTERED = False


def _register_custom_models() -> None:
    """fastembed 標準外モデルを登録する（モジュール import 時に 1 回）。

    _SUPPORTED_MODELS_CACHE 生成より前に走る必要がある。fastembed 0.4 系には
    add_custom_model が無いため、失敗時はログのみで継続する（その場合
    resolve_embedding_model は既定へフォールバックし、embed 失敗→BM25 に落ちる）。
    """
    global _CUSTOM_MODELS_REGISTERED
    if _CUSTOM_MODELS_REGISTERED or not _HAS_FASTEMBED:
        return
    try:
        from fastembed.common.model_description import PoolingType, ModelSource

        for spec in _CUSTOM_EMBEDDING_MODELS:
            TextEmbedding.add_custom_model(  # type: ignore[name-defined]
                model=spec["model"],
                pooling=PoolingType.MEAN,
                normalization=True,
                sources=ModelSource(hf=spec["model"]),
                dim=spec["dim"],
                model_file=spec["model_file"],
            )
        # 成功後にのみ立てる: 例外時は再試行機会を残す。
        _CUSTOM_MODELS_REGISTERED = True
    except Exception:
        # fastembed はあるのに登録失敗 = 既定モデルがロード不能 = 埋め込み全停止。
        logger.warning("Could not register custom embedding models", exc_info=True)


def resolve_embedding_model(model: str, supported: dict[str, str] | None) -> str:
    """Resolve *model* to its canonical (registered) name.

    TextEmbedding() は登録名でモデルを引くため、大小違いの綴り（例:
    ``INTFLOAT/MULTILINGUAL-E5-SMALL``）をそのまま渡すと KeyError になる。
    ``{lowercase: canonical}`` テーブル経由でカノニカル名へ寄せる。
    ORT 経路モデル（ruri 等）は fastembed の有無に関わらず既知なので先に解決する。
    未知の名前は従来挙動: DEFAULT_EMBEDDING_MODEL に警告付きでフォールバック。
    """
    ort_canonical = canonical_ort_model(model)
    if ort_canonical is not None:
        return ort_canonical
    if supported is None:
        return model
    canonical = supported.get(model.lower())
    if canonical is None:
        logger.warning(
            "Embedding model '%s' is not supported by fastembed; falling back to '%s'",
            model,
            DEFAULT_EMBEDDING_MODEL,
        )
        return DEFAULT_EMBEDDING_MODEL
    return canonical


# Search tunables (see ToolIndex.search for the contract these enforce).
_SEMANTIC_FLOOR = 0.30  # min cosine to keep a semantic candidate (default profile)
_BM25_REL_FLOOR = 0.25  # keep BM25 hits within 25% of the best hit
_RRF_K = 60  # Reciprocal Rank Fusion dampening constant
_RRF_CANDIDATES = 20  # per-ranker candidate depth feeding RRF

# 埋め込みモデルごとのプロファイル。
#   query_prefix / passage_prefix: E5 系は非対称プレフィックスが必須。fastembed は
#     自動付与しないため、片側だけ直す事故を防ぐべくここで一元管理する。
#   semantic_floor: セマンティック候補を残す最小コサイン。
#
# E5 の注意（実測, docs/reports/ja-tool-search-fix-report.md 参照）: コサインは
# 0.70〜0.85 の狭い帯に集中し、関連クエリの top-1 (0.796〜0.846) と、対応ツールが
# 存在しないクエリの top-1 (0.776〜0.784) はほぼ重なる。つまり **どの絶対閾値でも
# 関連/無関係を分離できない**。0.75 は「明らかなゴミ（退化/失敗埋め込み）」を落とす
# 低位の床にすぎず、順位付け（RRF）が分離を担う。
_MODEL_PROFILES: dict[str, dict[str, Any]] = {
    "intfloat/multilingual-e5-small": {
        "query_prefix": "query: ",
        "passage_prefix": "passage: ",
        "semantic_floor": 0.75,
    },
    # ruri-v3（ModernBERT / ONNX Runtime 経路）は非対称プレフィックス必須。
    # floor は既定 0.30 のまま（#001 実測で 0.30 が機能することを確認済み）。
    "cl-nagoya/ruri-v3-30m": {
        "query_prefix": "検索クエリ: ",
        "passage_prefix": "検索文書: ",
        "semantic_floor": _SEMANTIC_FLOOR,
    },
}
_DEFAULT_PROFILE: dict[str, Any] = {
    "query_prefix": None,
    "passage_prefix": None,
    "semantic_floor": _SEMANTIC_FLOOR,
}
# E5 ファミリ（intfloat/ 配下、名前に e5 を含む）のフォールバックプロファイル。
# prefix はファミリ必須。floor はファミリ既定 0.30（明示プロファイル e5-small は 0.75）。
_E5_FAMILY_PROFILE: dict[str, Any] = {
    "query_prefix": "query: ",
    "passage_prefix": "passage: ",
    "semantic_floor": _SEMANTIC_FLOOR,
}


def model_profile(model: str) -> dict[str, Any]:
    """Return the embedding profile (prefixes + semantic floor) for *model*.

    大小文字を無視して引く。明示プロファイルを最優先。未定義でも intfloat/ 配下で
    名前に "e5" を含むモデルは E5 ファミリとして prefix を自動付与する
    （floor はファミリ既定 0.30）。それ以外は従来互換のデフォルト
    （prefix 無し / floor 0.30）を返す。
    """
    key = model.lower()
    if key in _MODEL_PROFILES:
        return _MODEL_PROFILES[key]
    if key.startswith("intfloat/") and "e5" in key:
        return _E5_FAMILY_PROFILE
    return _DEFAULT_PROFILE


# CJK 連続区間（かな・漢字・々）。記号/空白は区間を切る。
# ひらがな→カタカナ変換後は 0x3041-0x3096 は出現しないが防御的に残す。
# BMP のほか漢字拡張B面 (U+20000-U+2FFFF) を含む（𠮟 等のサロゲート外漢字）。
_CJK_RUN = re.compile(
    "[\u3041-\u3096\u30a1-\u30fa\u30fc\u3400-\u9fff\uf900-\ufaff\u3005\U00020000-\U0002ffff]+"
)

# ひらがな (U+3041-U+3096) → カタカナ (+0x60)。「ふぁいる」と「ファイル」を揃える。
_HIRAGANA_TO_KATAKANA = str.maketrans(
    {chr(c): chr(c + 0x60) for c in range(0x3041, 0x3097)}
)


def _embedding_dim(model: str) -> int | None:
    """既知ならモデルの埋め込み次元を返す（未知は None）。

    model_profile() は prefix と floor しか持たないため、キャッシュファイル名の
    分離用の次元はここで別途引く。未知でもキーはモデル名で既に分離されるので、
    次元が取れなくても実害はない（次元不一致は読み込み側で検出して再計算する）。
    """
    for spec in _CUSTOM_EMBEDDING_MODELS:
        if str(spec["model"]).lower() == model.lower():
            return int(spec["dim"])
    ort_spec = ort_model_spec(model)
    if ort_spec is not None:
        return int(ort_spec["dim"])
    if not _HAS_FASTEMBED:
        return None
    try:
        models = TextEmbedding.list_supported_models()  # type: ignore
    except Exception:
        return None
    for m in models:
        if str(m.get("model", "")).lower() == model.lower():
            dim = m.get("dim")
            return int(dim) if dim is not None else None
    return None


class ToolIndex:
    """Embedding-based semantic search over proxied tools, with BM25 fallback.

    Primary search uses fastembed (default: DEFAULT_EMBEDDING_MODEL) for dense retrieval.
    Falls back to BM25Okapi when fastembed is not available.
    Thread-safe via asyncio.Lock.

    Doc text for embedding: f"{server}/{name}: {description}"
    BM25 uses token duplication to simulate BM25F field weights.
    """

    def __init__(
        self,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        use_embeddings: bool = True,
    ):
        self._lock = asyncio.Lock()
        self._documents: list[
            dict
        ] = []  # [{server, name, description, inputSchema, tags}, ...]
        self._bm25: BM25Okapi | None = None
        self._corpus: list[list[str]] = []
        self._name_tokens: list[set[str]] = []
        self._name_idf: dict[str, float] = {}
        self._embedder: "TextEmbedding | None" = None  # type: ignore[name-defined]
        self._embeddings: "np.ndarray | None" = None  # type: ignore[name-defined]
        # Runtime setting (WebUI 編集可、デフォルト ON) に加え、
        # MCP_HUB_EMBEDDING=0 はハードキル: 設定が ON でも埋め込みは常に無効。
        self._use_embeddings: bool = (
            _HAS_FASTEMBED
            and use_embeddings
            and os.environ.get("MCP_HUB_EMBEDDING", "1") != "0"
        )
        self._embedding_model: str = resolve_embedding_model(
            embedding_model, _supported_embedding_models()
        )
        self._profile: dict[str, Any] = model_profile(self._embedding_model)
        # 設定として要求された値（実効値 _use_embeddings と区別する）。embed 失敗で
        # 実効値を恒久降格しても、ユーザー意図はここに残す（A3 の再試行判定に使う）。
        self._embeddings_requested: bool = (
            _HAS_FASTEMBED
            and use_embeddings
            and os.environ.get("MCP_HUB_EMBEDDING", "1") != "0"
        )
        # 直近の embed 失敗の短い要約（embedding_status 用）。成功で None に戻す。
        self._embedding_error: str | None = None
        # A2: 前回 rebuild したコーパス/設定の同定キー。一致したら全体を short-circuit。
        self._index_key: tuple[Any, ...] | None = None
        self._corpus_hash: str | None = None

    @property
    def use_embeddings(self) -> bool:
        """現在の効値（設定×fastembed 可否×env ハードキル）。admin GET はこれ-reported."""
        return self._use_embeddings

    @property
    def embedding_status(self) -> str:
        """埋め込みの実効状態（A3: 設定の意図でなく真実を返す）。

        "active" | "building" | "inactive:no-fastembed" | "inactive:setting" |
        "inactive:no-documents" | "error:<短い要約>" のいずれか。
        embed 失敗で恒久降格（_use_embeddings=False）しても理由を error:* で残す。
        """
        if self._embedding_error:
            return f"error:{self._embedding_error}"
        if not _HAS_FASTEMBED:
            return "inactive:no-fastembed"
        if not self._use_embeddings:
            return "inactive:setting"
        if not self._documents:
            return "inactive:no-documents"
        if self._embeddings is None:
            return "building"
        return "active"

    def set_use_embeddings(self, enabled: bool) -> None:
        """ランタイムで埋め込み ON/OFF を切り替える。

        env の MCP_HUB_EMBEDDING=0 は設定より優先（ハードキル）。
        ここはフラグだけ変え、再構築は呼び出し側（rebuild_index）の責務。
        ON 化直後 _embeddings が None でも search() のガード
        （_use_embeddings and _embeddings is not None）で BM25 に落ちるため安全。
        """
        self._embeddings_requested = enabled
        self._use_embeddings = (
            _HAS_FASTEMBED
            and enabled
            and os.environ.get("MCP_HUB_EMBEDDING", "1") != "0"
        )

    # ── Tokenization ──────────────────────────────────────────────

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Code-aware tokenizer with camelCase and digit boundary splitting.

        Strategy (informed by trusty_search_core / veles-core / arXiv:2605.18561):
        1. Split on whitespace first (handles natural language descriptions)
        2. Always keep the original word lowercased for exact identifier matches
        3. Additionally split on non-alphanumeric boundaries (_, -, ., /, etc.)
        4. Split camelCase/PascalCase within each piece:
           getHTTPResponse → [get, http, response]
           Handle acronyms:   HTTPServer → [http, server]
        5. Split at digit boundaries: parse2Things → [parse, 2, things]
        6. Deduplicate while preserving order

        Snake_case identifiers are preserved intact (step 2) AND also split
        (step 3), giving both exact match and component match capability.

        v2 SDK schemas use list-form fields (e.g. type: ["string", "null"]);
        coerce here so one non-str field can't kill an index rebuild.

        Japanese support (v3): NFKC → ひらがな→カタカナ → 既存 ASCII 経路温存 +
        CJK 連続区間に bigram+unigram。bigram は Lucene CJKAnalyzer 方式で、
        日本語に空白が無くても部分一致を拾えるようにする。

        注: casefold は emit() 内でトークン単位に適用する。テキスト全体を先に
        casefold すると ASCII 側の camelCase 分割（getHTTPResponse）が壊れるため。
        """
        if isinstance(text, list):
            text = " ".join(str(t) for t in text)
        elif not isinstance(text, str):
            text = str(text)
        # 全角英数/半角カナ→ ASCII/全角カナ、ひらがな→カタカナ。
        text = unicodedata.normalize("NFKC", text).translate(_HIRAGANA_TO_KATAKANA)
        seen: set[str] = set()
        out: list[str] = []

        def emit(token: str) -> None:
            token = token.strip().casefold()
            if token and token not in seen:
                seen.add(token)
                out.append(token)

        for word in text.split():
            # Step 2: Keep the original word (preserves snake_case: "file_read" stays intact)
            emit(word)

            # Step 3: Split on non-alnum to get components
            sub_parts = re.findall(r"[a-zA-Z0-9]+", word)
            for part in sub_parts:
                emit(part)

                # Step 4: camelCase/PascalCase splitting
                crunched = re.sub(r"([a-z])([A-Z])", r"\1 \2", part)
                crunched = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", crunched)
                if crunched != part:
                    for camel_piece in crunched.split():
                        emit(camel_piece)

                # Step 5: Digit boundary splitting
                digit_parts = re.split(r"(\d+)", part)
                if len(digit_parts) > 1:
                    for dp in digit_parts:
                        if dp and dp != part:
                            emit(dp)

            # Step 6: CJK runs → 全体 + bigram + unigram（記号/空白で窓を切る）
            for run in _CJK_RUN.findall(word):
                emit(run)
                for i in range(len(run) - 1):
                    emit(run[i : i + 2])
                for ch in run:
                    emit(ch)

        return out

    # ── Document building ─────────────────────────────────────────

    @staticmethod
    def _build_doc_tokens(doc: dict) -> list[str]:
        """Build a weighted token list for a single tool document.

        Uses token duplication to approximate BM25F field weights.
        Heavier weights → more copies → higher term frequency → higher score.
        """
        tokens: list[str] = []

        def _add(text: str, copies: int = 1) -> None:
            field_tokens = ToolIndex._tokenize(text)
            for _ in range(copies):
                tokens.extend(field_tokens)

        # Core fields with explicit weights
        _add(doc["name"], copies=5)  # Tool name: ×5
        _add(doc["server"], copies=3)  # Server name: ×3
        _add(doc.get("index_text") or doc.get("description", ""), copies=2)  # Description: ×2
        for tag in doc.get("tags", []):  # サーバータグ: ×2（説明と同重み）
            _add(tag, copies=2)

        # InputSchema fields — included at ×1 (baseline)
        schema = doc.get("inputSchema", {})
        if isinstance(schema, dict):
            for param_name, param_info in schema.get("properties", {}).items():
                _add(param_name, copies=1)  # Parameter name

                if isinstance(param_info, dict):
                    _add(param_info.get("type", ""), copies=1)  # Type
                    _add(param_info.get("description", ""), copies=1)  # Param desc

                    # Enum values are highly specific → ×2
                    for ev in param_info.get("enum", []):
                        if isinstance(ev, str):
                            _add(ev, copies=2)

        return tokens

    # ── Build + Search ────────────────────────────────────────────

    def _embed_docs_blocking(self, doc_texts: list[str]) -> "np.ndarray":  # type: ignore[name-defined]
        """Synchronous dense embedding computation (runs in a worker thread).

        TextEmbedding init + embed + numpy conversion are CPU-bound and would
        otherwise block the event loop for seconds during every rebuild.
        """
        if self._embedder is None:  # type: ignore[truthiness-function]
            # モデル名でエンジンを選ぶ: ruri 等は OrtEmbedder、他は fastembed。
            self._embedder = create_embedder(self._embedding_model)
        prefix = self._profile["passage_prefix"] or ""
        gen = self._embedder.embed(
            [prefix + t for t in doc_texts], batch_size=_EMBED_BATCH_SIZE
        )
        return np.array(list(gen), dtype=np.float32)  # type: ignore[name-defined]

    # ── Embedding disk cache (A1) ────────────────

    def _cache_dir(self) -> str:
        """キャッシュ置き場。

        掃除方針: ファイル名はモデル名/次元/prefix/TEXT_FMT_VERSION のハッシュなので、
        モデルを差し替えても旧ファイルは読まれないまま溜まるだけ（自動削除はしない
        — 一時的なモデル切替で再 embed が走るのを避けるため）。不要になったら
        MCP_HUB_EMBED_CACHE_DIR 直下の npz を手動で消せばよい。
        """
        return os.environ.get("MCP_HUB_EMBED_CACHE_DIR") or _DEFAULT_EMBED_CACHE_DIR

    def _cache_path(self) -> str:
        """キャッシュ npz のパス。モデル名/次元/passage prefix/TEXT_FMT_VERSION で分離。

        モデルを差し替えても（例: ruri-v3-30m 既定化）キーが衝突しない。
        """
        dim = _embedding_dim(self._embedding_model)
        key = (
            f"{self._embedding_model}|{dim}|"
            f"{self._profile['passage_prefix'] or ''}|{TEXT_FMT_VERSION}"
        )
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]  # noqa: S324
        return os.path.join(self._cache_dir(), digest + ".npz")

    def _load_cache(self) -> dict[str, "np.ndarray"]:
        """{doc_sha: vector} を返す。欠落/破損/読めない時は空 dict（例外を出さない）。"""
        path = self._cache_path()
        try:
            with np.load(path) as data:  # type: ignore
                return {k: np.asarray(data[k], dtype=np.float32) for k in data.files}  # type: ignore
        except Exception:
            logger.debug("Embedding cache unreadable: %s", path, exc_info=True)
            return {}

    def _save_cache(self, vectors: dict[str, "np.ndarray"]) -> None:
        """現コーパス分の {doc_sha: vector} を npz に丸ごと保存する（追記ではない）。

        tmp ファイル + os.replace で atomic に置換する（部分書き込みの npz を
        次回プロセスが読んで壊れるのを防ぐ）。渡された辞書がそのままファイル
        内容になるので、呼び側は「現コーパス全分」を渡す必要がある。
        """
        if not vectors:
            return
        path = self._cache_path()
        tmp: str | None = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".npz.tmp")
            with os.fdopen(fd, "wb") as fh:
                np.savez(fh, **vectors)  # type: ignore[name-defined,arg-type]
            os.replace(tmp, path)
        except Exception:
            logger.debug("Embedding cache write failed: %s", path, exc_info=True)
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    async def _embed_with_cache(self, doc_texts: list[str]) -> "np.ndarray":
        """doc_texts を埋め込む。ディスクキャッシュにある行は再利用し、不足分だけ embed。

        キーは文書テキスト単位の sha1[:16]。返り値は文書順の (n, dim) 行列。
        キャッシュの破損/次元不一致は例外を出さず全件再計算にフォールバックする。
        """
        if not doc_texts:
            return np.zeros((0, 0), dtype=np.float32)  # type: ignore[name-defined]
        shas = [
            hashlib.sha1(t.encode("utf-8")).hexdigest()[:16]  # noqa: S324
            for t in doc_texts
        ]
        cached = self._load_cache()
        per_sha: dict[str, Any] = {}
        shapes: set[tuple[int, ...]] = set()
        for s in shas:
            v = cached.get(s)
            if v is not None:
                per_sha[s] = v
                shapes.add(v.shape)
        if len(shapes) > 1:
            # 次元の混ざったキャッシュは信用しない（全件再計算に回す）。
            per_sha = {}
        missing = [i for i, s in enumerate(shas) if s not in per_sha]
        if missing:
            arr = np.asarray(  # type: ignore[name-defined]
                await asyncio.to_thread(
                    self._embed_docs_blocking, [doc_texts[i] for i in missing]
                ),
                dtype=np.float32,  # type: ignore[name-defined]
            )
            fresh: dict[str, Any] = {}
            for j, i in enumerate(missing):
                fresh[shas[i]] = arr[j]
                per_sha[shas[i]] = arr[j]
            # 現コーパス全分（cached ∪ fresh）を保存する。fresh だけを保存すると
            # 読み込み済みエントリが毎回消え、部分 rebuild のたびにキャッシュが
            # 自壊する（回帰: test_partial_change_does_not_clobber_cache）。
            self._save_cache(per_sha)
        try:
            return np.vstack([per_sha[s] for s in shas])  # type: ignore[name-defined]
        except ValueError:
            # 新規埋め込みとキャッシュの次元が合わない等 → キャッシュ無しで全件再計算。
            return np.asarray(  # type: ignore[name-defined]
                await asyncio.to_thread(self._embed_docs_blocking, doc_texts),
                dtype=np.float32,  # type: ignore[name-defined]
            )

    @staticmethod
    def _corpus_hash_of(documents: list[dict]) -> str:
        """コーパス同定用ハッシュ（server/name/index_text/full_description/tags/inputSchema 正規化 JSON）。"""
        key = [
            {
                "server": d.get("server"),
                "name": d.get("name"),
                # 検索に効くのは index_text（無ければ description にフォールバック）。
                "index_text": d.get("index_text") or d.get("description", ""),
                # full_description（raw 全体）も含める: index_text は raw[:400] に
                # 切り詰められるため、400 字以降だけの編集（表示 description の
                # 400-600 帯 / full_description の 600+ 帯）がハッシュに出ず
                # short-circuit して古い文面を serving する穴があった。
                "full_description": d.get("full_description", ""),
                "tags": d.get("tags", []),
                "inputSchema": d.get("inputSchema", {}),
            }
            for d in documents
        ]
        blob = json.dumps(key, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()  # noqa: S324

    async def rebuild(self, documents: list[dict]) -> None:
        """Rebuild index from pre-built tool documents.

        Each document: {server, name, description, index_text, full_description,
        inputSchema}. Caller is responsible for building the document list. The
        index text uses ``index_text`` when present (callers build it as
        ``サーバー説明 + ツール説明（_INDEX_DESC_CHARS 上限）`` so server-level
        (often Japanese) context enters both BM25 and embedding retrieval) and
        falls back to ``description`` otherwise. ``description`` is the display
        string; ``full_description`` is the untruncated raw tool description
        (``get_schema``).

        When fastembed is available, also computes dense embeddings
        for semantic search. Falls back to BM25 otherwise.

        A2: コーパス（server/name/index_text/full_description/tags/inputSchema
        の正規化 JSON）と埋め込み設定が前回と同一なら全体を short-circuit する
        （BM25 索引も埋め込みも
        作り直さない）。差分がある時は A1 のディスクキャッシュ経由で変わった
        文書だけを埋め込む。
        """
        async with self._lock:
            corpus_hash = self._corpus_hash_of(documents)
            passage_prefix = self._profile["passage_prefix"] or ""
            if (
                corpus_hash,
                self._use_embeddings,
                self._embedding_model,
                passage_prefix,
            ) == self._index_key:
                # コーパスも埋め込み設定も不変 → 再構築を丸ごと省略。
                return

            # 恒久降格後でも、コーパスが変わった時だけ埋め込みを再試行する（不変
            # コーパスでの毎回リトライ＝無限再 embed を防ぐ）。ユーザー OFF / env
            # ハードキル / fastembed 不在は _embeddings_requested と _HAS_FASTEMBED
            # と env で除外される。
            if (
                corpus_hash != self._corpus_hash
                and self._embeddings_requested
                and not self._use_embeddings
                and _HAS_FASTEMBED
                and os.environ.get("MCP_HUB_EMBEDDING", "1") != "0"
            ):
                self._use_embeddings = True
                self._embedding_error = None

            self._documents = documents
            self._corpus = [self._build_doc_tokens(d) for d in documents]
            self._bm25 = BM25Okapi(self._corpus) if self._corpus else None

            # Name-token IDF: drives selective name-match promotion in search().
            self._name_tokens = [set(self._tokenize(d["name"])) for d in documents]
            df: dict[str, int] = {}
            for name_tokens in self._name_tokens:
                for token in name_tokens:
                    df[token] = df.get(token, 0) + 1
            n_docs = len(documents)
            self._name_idf = (
                {t: math.log(n_docs / c) for t, c in df.items()} if n_docs else {}
            )

            # Compute embeddings if fastembed is available
            if self._use_embeddings and documents:
                try:
                    # タグ無しでも括弧付きで均一フォーマット（埋め込みの決定性を担保）
                    doc_texts = [
                        f"{d['server']}/{d['name']} [{', '.join(d.get('tags', []))}]: {d.get('index_text') or d.get('description', '')}"
                        for d in documents
                    ]
                    # Run CPU-bound embedding in a thread; event loop stays responsive.
                    # Search falls back to BM25 until embeddings are ready.
                    # 旧コーパスの行列を残さない: _documents は既に差し替え済なので、
                    # ここで _embeddings を消してから embed する（残すと search() の
                    # semantic 経路が新 doc リストと行ずれする＝誤ヒット）。この窓は
                    # embedding_status も "building" を返し真実になる。
                    self._embeddings = None
                    # A1: 既存のキャッシュ済みベクトルは再利用し、不足行だけ embed する。
                    self._embeddings = await self._embed_with_cache(doc_texts)
                    self._embedding_error = None
                except Exception as exc:
                    logger.warning(
                        "Embedding failed, falling back to BM25", exc_info=True
                    )
                    self._embeddings = None
                    self._use_embeddings = False
                    # A3: 恒久降格の理由を残す（embedding_status/admin で可視化）。
                    self._embedding_error = f"{type(exc).__name__}: {str(exc)[:80]}"
            else:
                self._embeddings = None

            self._corpus_hash = corpus_hash
            self._index_key = (
                corpus_hash,
                self._use_embeddings,
                self._embedding_model,
                passage_prefix,
            )

        logger.info("ToolIndex rebuilt: %d tools indexed", len(documents))

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        """Search tools by keyword or semantic query.

        Contract:
        - Name-match promotion: a tool whose *name* shares at least one
          selective token with the query is promoted to the front. Selective
          means the token is rare across tool names — its IDF must be at least
          log(N / max(1, N // 4)), i.e. it appears in ≤ ~25% of names. Generic
          tokens (e.g. "get", "file") present in many names do not promote.
          Promoted entries are ranked by summed IDF of the shared tokens
          (desc, stable ties); their ``score`` is that IDF sum, rounded to 4
          decimals. Promotion is truncated to top_k first, so an exact name
          match is never buried.
        - Tail ranking with embeddings available is Reciprocal Rank Fusion
          (RRF, k=60) of the floor-filtered semantic candidates
          (cosine ≥ the model profile's ``semantic_floor``) and BM25
          candidates (score ≥ ``_BM25_REL_FLOOR`` × best). Each fused entry's
          ``score`` is its RRF score, rounded to 4 decimals. Without embeddings
          (unavailable, embed() raised, or ``set_use_embeddings(False)``) the
          tail is BM25-only.
        - Floors and RRF knobs are tunable module constants
          (``_BM25_REL_FLOOR``, ``_RRF_K``, ``_RRF_CANDIDATES``); the semantic
          floor and the model's query/passage prefixes come from the per-model
          profile (``model_profile``).

        Japanese: the tokenizer emits CJK bigrams+unigrams and the default
        embedder is multilingual, so Japanese queries hit lexically and/or
        semantically. The indexed ``index_text`` may carry the server
        description prefix + a truncated tool description (_INDEX_DESC_CHARS),
        which raises Japanese hit rates.

        Returns list of {server, name, description, tags, inputSchema, score}.

        inputSchema is included so the LLM can proceed directly to execute_tool without
        a separate get_tool_schema call.

        Read-only — does not modify shared state, safe without lock.

        ``top_k`` is clamped to ``_MAX_TOP_K`` (LLM が巨大な top_k を渡しても
        返り値が肥大しないように)。top_k ≤ _MAX_TOP_K の挙動は不変。
        """
        top_k = min(top_k, _MAX_TOP_K)
        if not self._documents:
            return []
        query_tokens = set(self._tokenize(query))
        n_docs = len(self._documents)
        # A name token promotes only if it is selective (df ≤ ~25% of names).
        min_idf = math.log(n_docs / max(1, n_docs // 4))

        # Identifier matches: rank by summed IDF of shared name tokens (desc).
        # Generic tokens shared by many names have low IDF and are filtered by
        # min_idf, so junk partial hits no longer reach the front.
        exact = []
        for i, doc in enumerate(self._documents):
            shared = self._name_tokens[i] & query_tokens
            if not shared:
                continue
            if max(self._name_idf.get(t, 0.0) for t in shared) < min_idf:
                continue
            weighted = sum(self._name_idf.get(t, 0.0) for t in shared)
            # 明示的 dict 構築: {**doc,...} では doc に余計なキーが
            # あった時に promotion 経路だけ結果形状がずれる。
            exact.append(
                {
                    "server": doc["server"],
                    "name": doc["name"],
                    "description": doc.get("description", ""),
                    "tags": doc.get("tags", []),
                    "inputSchema": doc.get("inputSchema", {}),
                    "score": round(weighted, 4),
                }
            )
        exact.sort(
            key=lambda d: d["score"], reverse=True
        )  # stable: ties keep doc order

        if self._use_embeddings and self._embeddings is not None:
            candidates = max(top_k, _RRF_CANDIDATES)
            results = self._rrf_fuse(
                self._semantic_search(query, candidates),
                self._bm25_search(query, candidates),
            )
        else:
            results = self._bm25_search(query, top_k)

        # exact matches first (truncated to top_k), then underlying results as tail.
        seen: set[tuple[str, str]] = set()
        merged: list[dict] = []
        for item in exact[:top_k] + results:
            key = (item["server"], item["name"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
            if len(merged) >= top_k:
                break
        return merged

    @staticmethod
    def _rrf_fuse(*candidate_lists: list[dict]) -> list[dict]:
        """Reciprocal Rank Fusion of ranked result lists.

        Rank is 1-based within each list; an entry present in multiple lists
        accumulates 1/(_RRF_K + rank) from each. Output ``score`` is the summed
        RRF score (rounded to 4 decimals), ordered descending.
        """
        fused: dict[tuple[str, str], dict] = {}
        for ranked in candidate_lists:
            for rank, item in enumerate(ranked, start=1):
                key = (item["server"], item["name"])
                entry = fused.get(key)
                if entry is None:
                    entry = {
                        "server": item["server"],
                        "name": item["name"],
                        "description": item.get("description", ""),
                        "tags": item.get("tags", []),
                        "inputSchema": item.get("inputSchema", {}),
                        "score": 0.0,
                    }
                    fused[key] = entry
                entry["score"] += 1.0 / (_RRF_K + rank)
        out = sorted(fused.values(), key=lambda d: d["score"], reverse=True)
        for entry in out:
            entry["score"] = round(entry["score"], 4)
        return out

    def _semantic_search(self, query: str, top_k: int) -> list[dict]:
        """Dense retrieval via embedding cosine similarity."""
        if not _HAS_NUMPY:
            # numpy 無しでは意味検索不能 — search() が BM25 にフォールバック
            return []
        prefix = self._profile["query_prefix"] or ""
        try:
            query_vec = np.array(  # type: ignore[name-defined]
                list(self._embedder.embed([prefix + query])),  # type: ignore[union-attr]
                dtype=np.float32,  # type: ignore[union-attr]
            ).squeeze(0)
        except Exception:
            # 再構築時と同等: 警告して空結果→search() が BM25 にフォールバック
            logger.warning("Embedding query failed, falling back to BM25", exc_info=True)
            return []
        # L2-normalize query (bge-small produces normalized docs already)
        norm = np.linalg.norm(query_vec)  # type: ignore[name-defined]
        if norm > 0:
            query_vec = query_vec / norm
        # Dot product = cosine similarity (both vectors L2-normalized)
        scores = self._embeddings @ query_vec  # type: ignore[name-defined,operator]
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        docs = self._documents
        results = []
        floor = self._profile["semantic_floor"]
        for idx in ranked[:top_k]:
            score = float(scores[idx])
            if score < floor:
                break  # Remaining scores are ≤ this (sorted descending) — stop
            doc = docs[idx]
            results.append(
                {
                    "server": doc["server"],
                    "name": doc["name"],
                    "description": doc.get("description", ""),
                    "tags": doc.get("tags", []),
                    "inputSchema": doc.get("inputSchema", {}),
                    "score": round(score, 4),
                }
            )
        return results

    def _bm25_search(self, query: str, top_k: int) -> list[dict]:
        """BM25 keyword search (fallback when fastembed unavailable)."""
        if not self._bm25 or not self._corpus:
            return []
        tokens = self._tokenize(query)
        scores = self._bm25.get_scores(tokens)
        docs = self._documents
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        best = float(scores[ranked[0]]) if ranked else 0.0
        results = []
        for idx in ranked[:top_k]:
            score = float(scores[idx])
            # Keep positive hits within _BM25_REL_FLOOR of the best hit;
            # ranked is descending so the first miss ends the scan.
            if score <= 0 or score < best * _BM25_REL_FLOOR:
                break
            doc = docs[idx]
            results.append(
                {
                    "server": doc["server"],
                    "name": doc["name"],
                    "description": doc.get("description", ""),
                    "tags": doc.get("tags", []),
                    "inputSchema": doc.get("inputSchema", {}),
                    "score": round(score, 4),
                }
            )
        # Small-corpus fallback: when N ≤ 5 and BM25 produces negative IDF
        # (all terms appear in most docs), use simple TF overlap scoring
        if not results and len(self._corpus) <= 5:
            query_set = set(tokens)
            tf_scores = []
            for doc_tokens in self._corpus:
                hits = sum(1 for t in doc_tokens if t in query_set)
                tf_scores.append(hits / max(1, len(doc_tokens)))
            tf_ranked = sorted(
                range(len(tf_scores)), key=lambda i: tf_scores[i], reverse=True
            )
            for idx in tf_ranked[:top_k]:
                if tf_scores[idx] <= 0:
                    break
                doc = docs[idx]
                results.append(
                    {
                        "server": doc["server"],
                        "name": doc["name"],
                        "description": doc.get("description", ""),
                        "tags": doc.get("tags", []),
                        "inputSchema": doc.get("inputSchema", {}),
                        "score": round(tf_scores[idx], 4),
                    }
                )
        return results

    # ── Schema + Server listing ───────────────────────────────────

    def get_schema(self, server: str, tool_name: str) -> dict | None:
        """Get full inputSchema for a tool. Read-only, safe without lock."""
        for doc in self._documents:
            if doc["server"] == server and doc["name"] == tool_name:
                return {
                    "name": doc["name"],
                    "description": doc.get("full_description") or doc.get("description", ""),
                    "server": server,
                    "inputSchema": doc.get("inputSchema", {}),
                }
        return None

    def list_servers(self) -> list[str]:
        """List all indexed server names."""
        return sorted(set(d["server"] for d in self._documents))

    def get_tools_by_server(self) -> dict[str, list[str]]:
        """Return {server_name: [tool_names]} for quick overview. Read-only."""
        result: dict[str, list[str]] = {}
        for doc in self._documents:
            result.setdefault(doc["server"], []).append(doc["name"])
        return result


class MetaTools:
    """Manages meta-tool definitions and execution."""

    def __init__(
        self,
        tool_index: ToolIndex,
        execute_tool_fn: Callable[[str, str, dict], Any],
        get_server_tags: Callable[[str], list[str]] | None = None,
        list_server_tools_fn: Callable[[str], Awaitable[list]] | None = None,
        list_servers_fn: Callable[[], list[str]] | None = None,
        get_server_description: Callable[[str], str] | None = None,
    ):
        self._index = tool_index
        self._execute_tool = execute_tool_fn
        self._get_server_tags = get_server_tags or (lambda _: [])
        self._get_server_description = get_server_description or (lambda _: "")

        async def _noop_server(name: str) -> list:
            return []

        # Live-proxy accessors: listing and execution must NOT depend on the
        # (possibly stale / partially-rebuilt) index.
        self._list_server_tools = list_server_tools_fn or _noop_server
        self._list_servers = list_servers_fn or (lambda: [])

    def _resolve_server_name(self, server: str) -> str:
        """case-insensitive に live サーバー名へ正規化する（ora-1）。

        LLM クライアントは登録名 "exa" に "Exa" を送ることがある（prod ログ）。
        完全一致が先（既存動作を壊さない）。大小違いがユニークに決まる時だけ
        置換し、曖昧（"EXA" と "exa" が両方生接続）なら素通しして既存の
        not-found エラーに落とす。index でなく live 接続一覧を照合源に使う。
        """
        live = self._list_servers()
        if server in live:
            return server
        matches = [s for s in live if s.lower() == server.lower()]
        return matches[0] if len(matches) == 1 else server

    def _get_allowed_servers(self) -> set[str] | None:
        """Return the set of server names allowed by request_tags, or None if no filter."""
        tags = request_tags.get()
        if not tags:
            return None
        allowed: set[str] = set()
        for server_name in self._index.list_servers():
            server_tags = self._get_server_tags(server_name)
            if _tags_match(tags, server_tags):
                allowed.add(server_name)
        return allowed

    def _filter_search_results(
        self, results: list[dict], allowed: set[str]
    ) -> list[dict]:
        """Remove results from servers not in *allowed*."""
        return [r for r in results if r["server"] in allowed]

    async def search_tools(self, query: str, top_k: int = 10) -> str:
        """Search upstream tools. Always call FIRST before execute_tool.

        Args:
            query: What you want to do (e.g. "read files", "search web")
            top_k: Max results (default 10)
        """
        results = self._index.search(query, top_k)
        allowed = self._get_allowed_servers()
        if allowed is not None:
            filtered = self._filter_search_results(results, allowed)
            if results and not filtered:
                # A3: タグフィルタで全件除外されたケース（0件ヒットの主因）を
                # additive な hint で明示する。既存の message キーは不変。
                active = request_tags.get() or []
                return json.dumps(
                    {
                        "message": "No matching tools found",
                        "hint": (
                            f"タグフィルタ {', '.join(map(str, active))} により"
                            "全件除外された可能性があります。より広いタグ"
                            "（またはタグ無し）で再試行してください。"
                        ),
                    },
                    ensure_ascii=False,
                )
            results = filtered
        if not results:
            return json.dumps(
                {
                    "message": "No matching tools found",
                    "hint": "Try broader keywords or check server connections.",
                },
                ensure_ascii=False,
            )
        # 結果に現れたサーバーの一行説明をトップレベルに添える（表示 description から
        # サーバー前置を外したため、LLM がサーバー文脈を得る経路をここに移す）。
        servers: dict[str, str] = {}
        for r in results:
            srv = r["server"]
            if srv not in servers:
                desc = self._get_server_description(srv)
                servers[srv] = desc if isinstance(desc, str) else ""
        return json.dumps({"results": results, "servers": servers}, ensure_ascii=False)

    async def execute_tool(
        self,
        server: str = "",
        tool_name: str = "",
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Execute a tool discovered via search_tools.

        Args:
            server: From search_tools results
            tool_name: From search_tools results
            arguments: Use inputSchema from search_tools results
        """
        # Compat shim: some LLM clients flatten ALL params into `arguments`
        # (prod logs: {"arguments": {"query": ..., "server": "Exa",
        # "tool_name": "web_search_exa"}}), which pydantic rejected before this
        # code ran. Lift those keys ONLY when the top-level value is absent —
        # a correct caller is unaffected, and an upstream tool that genuinely
        # has a "server" parameter keeps it whenever server was passed.
        if arguments is None:
            arguments = {}
        if not server or not tool_name:
            arguments = dict(arguments)
            if not server:
                server = str(arguments.pop("server", "") or "")
            if not tool_name:
                tool_name = str(arguments.pop("tool_name", "") or "")

        if not server or not tool_name:
            return json.dumps(
                {
                    "error": "execute_tool requires both 'server' and 'tool_name'.",
                    "hint": "Call search_tools first and use the server/name values it returns.",
                },
                ensure_ascii=False,
            )

        # Normalize to the registered (live) server name before tag check /
        # execution — "Exa" → "exa" (ora-1).
        server = self._resolve_server_name(server)

        # Existence gate BEFORE the tag check: _get_server_tags() returns []
        # for an unknown server, which the tag check below reported as a
        # misleading "tag filter" error (prod: librarian tripped on this).
        # An absent server must say so, with the live server list attached.
        # (live 未注入 = 検証不能なので素通し。解決失敗＝ここまで素通し済み。)
        live = self._list_servers()
        if live and server not in live:
            # 応答肥大防止: 全列挙せず先頭10件のみ + 残り件数を返す
            ranked = sorted(live)
            shown = ranked[:_MAX_LISTED_SERVERS]
            if len(ranked) > len(shown):
                shown = [*shown, f"... and {len(ranked) - len(shown)} more"]
            return json.dumps(
                {
                    "error": f"Server '{server}' not found.",
                    "hint": "Call search_tools first and copy the exact 'server' "
                    "value from its results (case-insensitive match was tried "
                    "and found no unique server).",
                    "available_servers": shown,
                },
                ensure_ascii=False,
            )

        # Tag check: block execution if server's tags don't match request_tags.
        # Uses live server tags (not the index) so fresh servers work.
        tags = request_tags.get()
        if tags:
            server_tags = self._get_server_tags(server)
            if not _tags_match(tags, server_tags):
                return json.dumps(
                    {
                        "error": f"Server '{server}' is not available with current tag filter.",
                        "hint": "Check your X-MCP-Hub-Tags header or connect without tag filtering.",
                        # 実タグを返すことで「フィルタ不一致」をデバッグ可能に
                        "server_tags": list(server_tags),
                    },
                    ensure_ascii=False,
                )

        # Verify tool exists on the live proxy (index may be stale/missing).
        try:
            tools = await self._list_server_tools(server)
        except Exception:
            logger.debug("list_server_tools failed for %s", server, exc_info=True)
            tools = []
        if not any(getattr(t, "name", None) == tool_name for t in tools):
            return json.dumps(
                {
                    "error": f"Tool '{tool_name}' not found on server '{server}'.",
                    "hint": "Use search_tools first to discover available tools on this server.",
                },
                ensure_ascii=False,
            )
        return await self._execute_tool(server, tool_name, arguments)


class MetaApp:
    """Wrapper exposing FastMCP app with clean attribute interface."""

    def __init__(
        self,
        mcp: FastMCP,
        index: ToolIndex,
        meta: MetaTools,
        rebuild_fn,
        base_descriptions: dict[str, str],
    ):
        self.mcp = mcp
        self.index = index
        self.meta_tools = meta
        self.rebuild_index = rebuild_fn
        # 焼き込み前の素の tool description（毎回ここから再構成して冪等化）
        self.base_descriptions = base_descriptions


async def create_meta_app(
    proxy_manager,  # ProxyManager instance
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    use_embeddings: bool = True,
) -> MetaApp:
    """Create a MetaApp with meta-tools."""
    mcp = FastMCP("MCP Hub Meta")
    index = ToolIndex(embedding_model=embedding_model, use_embeddings=use_embeddings)
    base_descriptions: dict[str, str] = {}

    # search_tools の description を毎回 base から再構成する（idempotent）。
    # catalog が空でも base のみを書き戻す。
    async def _apply_catalog(catalog: str) -> None:
        tool = await mcp.get_tool("search_tools")
        if tool is None:
            return
        base_desc = base_descriptions.get("search_tools", tool.description or "")
        if catalog:
            tool.description = base_desc + "\n\nRegistered servers:\n" + catalog
        else:
            tool.description = base_desc

    # Build initial index from all connected proxy tools
    async def rebuild_index() -> list[str]:
        """Rebuild the tool index from connected proxies.

        Returns the names of servers whose tools could not be fetched
        (e.g. still warming up after recovery) so callers can retry.
        """
        all_tools = []
        failed: list[str] = []
        desc_fn = getattr(proxy_manager, "server_description", None)
        for server_name, proxy in proxy_manager.get_connected_servers().items():
            srv_desc = desc_fn(server_name) if callable(desc_fn) else ""
            if not isinstance(srv_desc, str):
                srv_desc = ""
            srv_desc = srv_desc.strip()
            try:
                if isinstance(proxy_manager, _ProxyManager):
                    tools = await proxy_manager.list_tools_for_server(
                        server_name, proxy
                    )
                else:
                    tools = await asyncio.wait_for(proxy.list_tools(), timeout=30.0)
                for t in tools:
                    raw_desc = (t.description or "").strip()
                    # 索引テキスト: サーバー説明（日本語）を前置し、ツール説明は
                    # _INDEX_DESC_CHARS で切る。BM25 トークンと埋め込みはこれから作る。
                    index_text = f"{srv_desc} {raw_desc[:_INDEX_DESC_CHARS]}".strip()
                    # 表示用: サーバー前置なし。切り詰めた時だけ "…" を付ける。
                    if len(raw_desc) > _DISPLAY_DESC_CHARS:
                        display_desc = raw_desc[:_DISPLAY_DESC_CHARS] + "…"
                    else:
                        display_desc = raw_desc
                    all_tools.append(
                        {
                            "server": server_name,
                            "name": t.name,
                            "description": display_desc,
                            "index_text": index_text,
                            "full_description": raw_desc,
                            "tags": list(proxy_manager.server_tags(server_name)),
                            "inputSchema": getattr(t, "parameters", {}),
                        }
                    )
            except Exception:
                logger.warning("Failed to list tools for %s", server_name)
                failed.append(server_name)
        await index.rebuild(all_tools)

        # per-server grouping → catalog を search_tools の description に焼き込む
        by_server: dict[str, list[str]] = {}
        for t in all_tools:
            by_server.setdefault(t["server"], []).append(t["name"])
        entries = []
        for srv, names in by_server.items():
            desc = desc_fn(srv) if callable(desc_fn) else ""
            entries.append(
                {
                    "server": srv,
                    "description": desc if isinstance(desc, str) else "",
                    "tools": names,
                }
            )
        await _apply_catalog(_build_catalog(entries))
        return failed

    # Live-proxy accessor for MetaTools (execution bypasses the index).
    async def _list_server_tools(server_name: str) -> list:
        proxy = proxy_manager.get_connected_servers().get(server_name)
        if proxy is None:
            return []
        return await proxy_manager.list_tools_for_server(server_name, proxy)

    def _server_description(name: str) -> str:
        fn = getattr(proxy_manager, "server_description", None)
        if not callable(fn):
            return ""
        desc = fn(name)
        return desc if isinstance(desc, str) else ""

    meta = MetaTools(
        tool_index=index,
        execute_tool_fn=lambda s, t, a: proxy_manager.call_tool(s, t, a),
        get_server_tags=proxy_manager.server_tags,
        list_server_tools_fn=_list_server_tools,
        # live 接続名（index の遅延に左右されない）— case-insensitive 解決用
        list_servers_fn=lambda: list(proxy_manager.get_connected_servers()),
        # 結果に現れるサーバーの一行説明（search_tools の servers マップ用）
        get_server_description=_server_description,
    )

    # Register meta tools via FastMCP tool decorator
    @mcp.tool()
    async def search_tools(query: str, top_k: int = 10) -> str:
        """Search upstream tools. Always call FIRST before execute_tool.

        Args:
            query: What you want to do (e.g. "read files", "search web")
            top_k: Max results (default 10)
        """
        return await meta.search_tools(query, top_k)

    @mcp.tool()
    async def execute_tool(
        server: str = "",
        tool_name: str = "",
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Execute a tool discovered via search_tools.

        Args:
            server: From search_tools results
            tool_name: From search_tools results
            arguments: Use inputSchema from search_tools results
        """
        # Defaults are optional (not required) so flattened LLM calls — where
        # server/tool_name ride inside `arguments` — reach the lift shim in
        # MetaTools.execute_tool instead of dying in schema validation.
        return await meta.execute_tool(server, tool_name, arguments)

    # 登録直後の素の description を捕捉（以降の焼き込みはここから再構成）。
    # FastMCP 4.0.2: get_tool() は local provider の stored instance を返し、
    # Tool は frozen ではないので description 代入が反映される。
    _search_tool = await mcp.get_tool("search_tools")
    if _search_tool is not None:
        base_descriptions["search_tools"] = _search_tool.description or ""

    return MetaApp(
        mcp=mcp,
        index=index,
        meta=meta,
        rebuild_fn=rebuild_index,
        base_descriptions=base_descriptions,
    )


# import 時に 1 回だけ: fastembed 標準外モデル（E5-small）を登録する。
# _supported_embedding_models() の初回呼び出しより前に走らせる必要がある。
_register_custom_models()
