"""
Progressive Discovery meta-tools with BM25 search.
Exposes 3 tools instead of all child server tools.
"""

import asyncio
import json
import logging
import math
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import FastMCP
from rank_bm25 import BM25Okapi

from .config import DEFAULT_EMBEDDING_MODEL
from .proxy_manager import ProxyManager as _ProxyManager
from .state import request_tags
from .state import tags_match as _tags_match

try:
    from fastembed import TextEmbedding
    import numpy as np

    _HAS_FASTEMBED = True
except ImportError:
    _HAS_FASTEMBED = False

logger = logging.getLogger(__name__)

MCP_HUB_TAGS_HEADER = "X-MCP-Hub-Tags"

_SUPPORTED_MODELS_CACHE: set[str] | None = None


def _supported_embedding_models() -> set[str] | None:
    """Lowercased set of model names supported by installed fastembed, or None if fastembed is unavailable/broken."""
    global _SUPPORTED_MODELS_CACHE
    if _SUPPORTED_MODELS_CACHE is None:
        try:
            if _HAS_FASTEMBED:
                # list_supported_models() は dict のリストを返す（各要素に "model" キー）
                _SUPPORTED_MODELS_CACHE = {
                    m["model"].lower() for m in TextEmbedding.list_supported_models()
                }  # type: ignore[name-defined]
            else:
                _SUPPORTED_MODELS_CACHE = None
        except Exception:
            logger.debug("Could not query fastembed supported models", exc_info=True)
            _SUPPORTED_MODELS_CACHE = None
    return _SUPPORTED_MODELS_CACHE


def resolve_embedding_model(model: str, supported: set[str] | None) -> str:
    """Return *model* if it is supported (or support is unknown); otherwise fall back to DEFAULT_EMBEDDING_MODEL with a warning."""
    if supported is not None and model.lower() not in supported:
        logger.warning(
            "Embedding model '%s' is not supported by fastembed; falling back to '%s'",
            model,
            DEFAULT_EMBEDDING_MODEL,
        )
        return DEFAULT_EMBEDDING_MODEL
    return model


# Search tunables (see ToolIndex.search for the contract these enforce).
_SEMANTIC_FLOOR = 0.30  # min cosine to keep a semantic candidate
_BM25_REL_FLOOR = 0.25  # keep BM25 hits within 25% of the best hit
_RRF_K = 60  # Reciprocal Rank Fusion dampening constant
_RRF_CANDIDATES = 20  # per-ranker candidate depth feeding RRF


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

    @property
    def use_embeddings(self) -> bool:
        """現在の効値（設定×fastembed 可否×env ハードキル）。admin GET はこれ-reported."""
        return self._use_embeddings

    def set_use_embeddings(self, enabled: bool) -> None:
        """ランタイムで埋め込み ON/OFF を切り替える。

        env の MCP_HUB_EMBEDDING=0 は設定より優先（ハードキル）。
        ここはフラグだけ変え、再構築は呼び出し側（rebuild_index）の責務。
        ON 化直後 _embeddings が None でも search() のガード
        （_use_embeddings and _embeddings is not None）で BM25 に落ちるため安全。
        """
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
        """
        if isinstance(text, list):
            text = " ".join(str(t) for t in text)
        elif not isinstance(text, str):
            text = str(text)
        seen: set[str] = set()
        out: list[str] = []

        def emit(token: str) -> None:
            token = token.strip().lower()
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
        _add(doc.get("description", ""), copies=2)  # Description: ×2
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
            self._embedder = TextEmbedding(self._embedding_model)  # type: ignore[name-defined]
        gen = self._embedder.embed(doc_texts)
        return np.array(list(gen), dtype=np.float32)  # type: ignore[name-defined]

    async def rebuild(self, documents: list[dict]) -> None:
        """Rebuild index from pre-built tool documents.

        Each document: {server, name, description, inputSchema}.
        Caller is responsible for building the document list.

        When fastembed is available, also computes dense embeddings
        for semantic search. Falls back to BM25 otherwise.
        """
        async with self._lock:
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
                        f"{d['server']}/{d['name']} [{', '.join(d.get('tags', []))}]: {d.get('description', '')}"
                        for d in documents
                    ]
                    # Run CPU-bound embedding in a thread; event loop stays responsive.
                    # Search falls back to BM25 until embeddings are ready.
                    self._embeddings = await asyncio.to_thread(
                        self._embed_docs_blocking, doc_texts
                    )
                except Exception:
                    logger.warning(
                        "Embedding failed, falling back to BM25", exc_info=True
                    )
                    self._embeddings = None
                    self._use_embeddings = False
            else:
                self._embeddings = None

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
          (cosine ≥ ``_SEMANTIC_FLOOR``) and BM25 candidates (score ≥
          ``_BM25_REL_FLOOR`` × best). Each fused entry's ``score`` is its RRF
          score, rounded to 4 decimals. Without embeddings (unavailable,
          embed() raised, or ``set_use_embeddings(False)``) the tail is
          BM25-only.
        - Floors and RRF knobs are tunable module constants
          (``_SEMANTIC_FLOOR``, ``_BM25_REL_FLOOR``, ``_RRF_K``,
          ``_RRF_CANDIDATES``).

        Known limitation: the tokenizer is ASCII-only and the default embedder
        is English-centric, so Japanese queries may return few or empty
        results. Multilingual support is deferred.

        Returns list of {server, name, description, tags, inputSchema, score}.

        inputSchema is included so the LLM can proceed directly to execute_tool without
        a separate get_tool_schema call.

        Read-only — does not modify shared state, safe without lock.
        """
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
        try:
            query_vec = np.array(  # type: ignore[name-defined]
                list(self._embedder.embed([query])),  # type: ignore[union-attr]
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
        for idx in ranked[:top_k]:
            score = float(scores[idx])
            if score < _SEMANTIC_FLOOR:
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
                    "description": doc.get("description", ""),
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
        list_all_tools_fn: Callable[[], Awaitable[dict]] | None = None,
        list_server_tools_fn: Callable[[str], Awaitable[list]] | None = None,
        list_servers_fn: Callable[[], list[str]] | None = None,
    ):
        self._index = tool_index
        self._execute_tool = execute_tool_fn
        self._get_server_tags = get_server_tags or (lambda _: [])

        async def _noop_all() -> dict:
            return {}

        async def _noop_server(name: str) -> list:
            return []

        # Live-proxy accessors: listing and execution must NOT depend on the
        # (possibly stale / partially-rebuilt) index.
        self._list_all_tools = list_all_tools_fn or _noop_all
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
            results = self._filter_search_results(results, allowed)
        if not results:
            return json.dumps(
                {
                    "message": "No matching tools found",
                    "hint": "Try broader keywords or check server connections.",
                },
                ensure_ascii=False,
                indent=2,
            )
        return json.dumps({"results": results}, ensure_ascii=False, indent=2)

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
                indent=2,
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
            return json.dumps(
                {
                    "error": f"Server '{server}' not found.",
                    "hint": "Call search_tools first and copy the exact 'server' "
                    "value from its results (case-insensitive match was tried "
                    "and found no unique server).",
                    "available_servers": sorted(live),
                },
                ensure_ascii=False,
                indent=2,
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
                    indent=2,
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
                indent=2,
            )
        return await self._execute_tool(server, tool_name, arguments)

    async def list_upstream_tools(self) -> str:
        """List all upstream tools grouped by server. Use for orientation, then search_tools.

        Reads from the live proxy manager (tag-filtered via request_tags),
        NOT the index — servers that failed to rebuild still appear here.
        """
        by_server = await self._list_all_tools()
        if not by_server:
            return json.dumps(
                {"message": "No upstream tools available. Add servers via admin API."},
                ensure_ascii=False,
                indent=2,
            )
        tools_by_server = {
            srv: [t["name"] for t in tools] for srv, tools in by_server.items()
        }
        total = sum(len(t) for t in tools_by_server.values())
        return json.dumps(
            {
                "total_tools": total,
                "tools_by_server": tools_by_server,
            },
            ensure_ascii=False,
            indent=2,
        )


class MetaApp:
    """Wrapper exposing FastMCP app with clean attribute interface."""

    def __init__(self, mcp: FastMCP, index: ToolIndex, meta: MetaTools, rebuild_fn):
        self.mcp = mcp
        self.index = index
        self.meta_tools = meta
        self.rebuild_index = rebuild_fn


def create_meta_app(
    proxy_manager,  # ProxyManager instance
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    use_embeddings: bool = True,
) -> MetaApp:
    """Create a MetaApp with meta-tools."""
    mcp = FastMCP("MCP Hub Meta")
    index = ToolIndex(embedding_model=embedding_model, use_embeddings=use_embeddings)

    # Build initial index from all connected proxy tools
    async def rebuild_index() -> list[str]:
        """Rebuild the tool index from connected proxies.

        Returns the names of servers whose tools could not be fetched
        (e.g. still warming up after recovery) so callers can retry.
        """
        all_tools = []
        failed: list[str] = []
        for server_name, proxy in proxy_manager.get_connected_servers().items():
            try:
                if isinstance(proxy_manager, _ProxyManager):
                    tools = await proxy_manager.list_tools_for_server(
                        server_name, proxy
                    )
                else:
                    tools = await asyncio.wait_for(proxy.list_tools(), timeout=30.0)
                for t in tools:
                    all_tools.append(
                        {
                            "server": server_name,
                            "name": t.name,
                            "description": t.description or "",
                            "tags": list(proxy_manager.server_tags(server_name)),
                            "inputSchema": getattr(t, "parameters", {}),
                        }
                    )
            except Exception:
                logger.warning("Failed to list tools for %s", server_name)
                failed.append(server_name)
        await index.rebuild(all_tools)
        return failed

    # Live-proxy accessors for MetaTools (list/execute bypass the index).
    async def _list_all_tools() -> dict:
        return await proxy_manager.list_tools()  # tags via request_tags

    async def _list_server_tools(server_name: str) -> list:
        proxy = proxy_manager.get_connected_servers().get(server_name)
        if proxy is None:
            return []
        return await proxy_manager.list_tools_for_server(server_name, proxy)

    meta = MetaTools(
        tool_index=index,
        execute_tool_fn=lambda s, t, a: proxy_manager.call_tool(s, t, a),
        get_server_tags=proxy_manager.server_tags,
        list_all_tools_fn=_list_all_tools,
        list_server_tools_fn=_list_server_tools,
        # live 接続名（index の遅延に左右されない）— case-insensitive 解決用
        list_servers_fn=lambda: list(proxy_manager.get_connected_servers()),
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

    @mcp.tool()
    async def list_upstream_tools() -> str:
        """List all upstream tools grouped by server. Use for orientation, then search_tools."""
        return await meta.list_upstream_tools()

    return MetaApp(mcp=mcp, index=index, meta=meta, rebuild_fn=rebuild_index)
