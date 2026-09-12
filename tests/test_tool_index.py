"""
ToolIndex unit tests — embedding-based search with BM25 fallback.
"""

import numpy as np
import pytest
from mcp_hub.config import DEFAULT_EMBEDDING_MODEL
from mcp_hub.meta_provider import ToolIndex, resolve_embedding_model, _HAS_FASTEMBED


class _FixedEmbedder:
    """Fake embedder returning a fixed query vector for every embed() call.

    Doc embeddings are injected directly onto the index; only the query
    embedding path calls this (mirrors the _Boom pattern in test_fix6.py).
    """

    def __init__(self, query_vec):
        self._query_vec = query_vec

    def embed(self, texts):
        return [self._query_vec for _ in texts]


@pytest.fixture
def sample_docs():
    return [
        {"name": "fetch_url", "description": "Fetch a URL and return markdown content", "server": "fetch", "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}}},
        {"name": "brave_web_search", "description": "Search the web using Brave Search API", "server": "brave-search", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
        {"name": "puppeteer_screenshot", "description": "Take a screenshot of a web page", "server": "puppeteer", "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}}},
        {"name": "file_read", "description": "Read file contents from disk", "server": "filesystem", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
        {"name": "file_write", "description": "Write content to a file on disk", "server": "filesystem", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}},
    ]


@pytest.fixture
async def index(sample_docs):
    idx = ToolIndex()
    await idx.rebuild(sample_docs)
    return idx


class TestToolIndex:
    """ToolIndex search unit tests (embedding-based with BM25 fallback)."""

    async def test_rebuild_empty(self):
        """Empty doc list — search returns empty."""
        idx = ToolIndex()
        await idx.rebuild([])
        assert idx.search("anything") == []

    async def test_search_exact_match(self, index):
        """Query exact tool name returns the matching tool in top 3 results.
        With embeddings, ordering differs from BM25 — exact token overlap
        is not guaranteed to produce #1 rank. But the tool should still
        appear in the top results."""
        results = index.search("brave_web_search")
        assert len(results) >= 1
        top_names = {r["name"] for r in results[:3]}
        assert "brave_web_search" in top_names
        assert "score" in results[0]

    async def test_search_by_keyword(self, index):
        """Keyword 'web' matches tools with 'web' in name or description."""
        results = index.search("web", top_k=5)
        assert len(results) >= 2
        names = {r["name"] for r in results}
        assert "brave_web_search" in names  # 'web' in name
        assert "puppeteer_screenshot" in names  # 'web page' in description

    async def test_search_file_keyword(self, index):
        """Keyword 'file' matches file_read and file_write."""
        results = index.search("file", top_k=3)
        assert len(results) >= 2
        names = {r["name"] for r in results}
        assert "file_read" in names
        assert "file_write" in names

    async def test_top_k_limit(self, index):
        """top_k=2 returns at most 2 results."""
        results = index.search("tool", top_k=2)
        assert len(results) <= 2

    async def test_search_no_match(self, index):
        """Nonsense query returns no results.

        Intentional contract change: weak-positive semantic hits below
        _SEMANTIC_FLOOR are discarded and BM25 finds no lexical match, so an
        empty result set is now valid. The old "embeddings always return
        something" contract is gone."""
        results = index.search("zzz_xyzzy_nonexistent_12345")
        assert results == []


class TestIdentifierPromotion:
    """Identifier match promotion: a tool whose name shares a token with the
    query must never be buried by ranking or top_k truncation. Promoted
    entries rank by shared token count (desc); score = shared token count."""

    @pytest.fixture
    async def memory_corpus_index(self):
        """memory_* tools (partial name overlap) + util filler docs.
        Insertion order deliberately differs from match strength."""
        docs = [
            {
                "name": name,
                "description": f"{name} operation on the memory store",
                "server": "memory",
                "inputSchema": {"type": "object", "properties": {}},
            }
            for name in ["memory_stats", "memory_delete", "memory_read", "memory_create", "get_context"]
        ]
        docs += [
            {
                "name": f"util_{i}",
                "description": f"Create generic helper utility number {i}",
                "server": "util",
                "inputSchema": {"type": "object", "properties": {}},
            }
            for i in range(12)
        ]
        idx = ToolIndex()
        await idx.rebuild(docs)
        idx._use_embeddings = False  # env-independent: exercise the BM25 path
        return idx

    async def test_search_identifier_match_ranked_by_shared_tokens(self, memory_corpus_index):
        """A selective exact name match outranks loose partial hits regardless
        of document order; promoted score is the summed IDF of shared name
        tokens (no fixed token count). Loose hits may arrive via promotion or
        the BM25 tail."""
        results = memory_corpus_index.search("create memory record memory_create", top_k=10)
        names = [r["name"] for r in results]
        assert names[0] == "memory_create"
        assert names.index("memory_create") < names.index("memory_delete")
        assert names.index("memory_create") < names.index("memory_stats")
        scores = {r["name"]: r["score"] for r in results}
        assert scores["memory_create"] > scores["memory_delete"]
        assert scores["memory_create"] > scores["memory_stats"]

    async def test_search_identifier_match_not_buried_by_top_k(self, index):
        """A hostile underlying ranking that puts the name-matched tool beyond
        top_k cannot hide it: promotion prepends it."""
        idx = index
        idx._use_embeddings = False  # env-independent: force the BM25 path
        target = next(d for d in idx._documents if d["name"] == "brave_web_search")
        assert not any(
            d["name"] != target["name"]
            and set(ToolIndex._tokenize(d["name"])) & set(ToolIndex._tokenize(target["name"]))
            for d in idx._documents
        ), "fixture: target name tokens must not overlap other tool names"

        # Simulate a hostile underlying ranking that ranks the exact name
        # match outside the top_k window.
        def fake_bm25(query, top_k):
            others = [d for d in idx._documents if d["name"] != "brave_web_search"]
            ranked = others + [target]
            return [
                {**d, "score": round(0.5, 4)} for d in ranked[:top_k]
            ]

        idx._bm25_search = fake_bm25
        results = idx.search("brave_web_search", top_k=3)
        names = [r["name"] for r in results]
        assert "brave_web_search" in names
        # Promotion places it first, and dedupe keeps a single entry
        assert results[0]["name"] == "brave_web_search"
        assert names.count("brave_web_search") == 1

    async def test_search_exact_overflows_top_k_sorted_by_strength(self):
        """Identifier matches that exceed top_k follow summed IDF (desc), not
        insertion order. The generic "file" token alone (df > 25% of names) no
        longer promotes, so the file_i tools stay unranked."""
        docs = [
            {
                "name": f"file_{i}",
                "description": f"File utility number {i}",
                "server": "filesystem",
                "inputSchema": {"type": "object", "properties": {}},
            }
            for i in range(1, 12)
        ]
        docs += [
            {
                "name": "read_write",
                "description": "Read and write helper",
                "server": "filesystem",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "file_read_write",
                "description": "Combined file read/write",
                "server": "filesystem",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]
        idx = ToolIndex()
        await idx.rebuild(docs)
        idx._use_embeddings = False
        # Insertion order puts file_1 first; IDF ranking must win.
        results = idx.search("file read write", top_k=5)
        assert len(results) <= 5
        names = [r["name"] for r in results]
        assert names[0] == "file_read_write"  # selective: file + read + write
        assert names[1] == "read_write"        # selective: read + write
        assert results[0]["score"] > results[1]["score"]
        # "file" alone is generic → file_N are not promoted or BM25-surfaced.
        assert "file_1" not in names


class TestSearchPrecision:
    """Precision rework: IDF-gated promotion, semantic/BM25 floors, RRF fusion."""

    async def test_generic_token_does_not_promote(self):
        """A token shared by many names (generic "get") must not promote a
        tool to the front; a keyword-relevant tool wins."""
        docs = [
            {"name": "get_user", "description": "", "server": "s", "inputSchema": {}, "tags": []},
            {"name": "get_post", "description": "", "server": "s", "inputSchema": {}, "tags": []},
            {"name": "get_comment", "description": "", "server": "s", "inputSchema": {}, "tags": []},
            {"name": "account_details", "description": "", "server": "s", "inputSchema": {}, "tags": []},
            {"name": "delete_file", "description": "", "server": "s", "inputSchema": {}, "tags": []},
            {"name": "list_items", "description": "", "server": "s", "inputSchema": {}, "tags": []},
        ]
        idx = ToolIndex()
        await idx.rebuild(docs)
        idx._use_embeddings = False
        results = idx.search("get account details", top_k=10)
        assert results[0]["name"] == "account_details"
        assert not results[0]["name"].startswith("get_")

    async def test_rrf_fusion_prefers_tool_in_both_lists(self):
        """A tool present in both semantic and BM25 candidate lists outranks
        tools present in only one list (Reciprocal Rank Fusion)."""
        docs = [
            {"name": "alpha_gadget", "description": "a", "server": "s", "inputSchema": {}, "tags": []},
            {"name": "beta_semantic", "description": "b", "server": "s", "inputSchema": {}, "tags": []},
            {"name": "gamma_gadget", "description": "c", "server": "s", "inputSchema": {}, "tags": []},
            {"name": "delta_other", "description": "d", "server": "s", "inputSchema": {}, "tags": []},
            {"name": "epsilon_thing", "description": "e", "server": "s", "inputSchema": {}, "tags": []},
            {"name": "zeta_tool", "description": "f", "server": "s", "inputSchema": {}, "tags": []},
        ]
        idx = ToolIndex()
        await idx.rebuild(docs)
        # Query (1,0); alpha cos 0.9, beta cos 0.8, the rest 0 (below floor).
        idx._embeddings = np.array(
            [[0.9, 0.4358899], [0.8, 0.6], [0.0, 1.0], [0.0, 1.0], [0.0, 1.0], [0.0, 1.0]],
            dtype=np.float32,
        )
        idx._embedder = _FixedEmbedder([1.0, 0.0])
        idx._use_embeddings = True
        # BM25 lexical hits: alpha_gadget + gamma_gadget (query "gadget").
        results = idx.search("gadget", top_k=10)
        names = [r["name"] for r in results]
        assert names[0] == "alpha_gadget"  # in BOTH lists
        assert "beta_semantic" in names    # semantic-only
        assert "gamma_gadget" in names     # BM25-only
        scores = {r["name"]: r["score"] for r in results}
        assert scores["alpha_gadget"] > scores["beta_semantic"]
        assert scores["alpha_gadget"] > scores["gamma_gadget"]

    async def test_semantic_floor_discards_weak_hits(self):
        """Semantic candidates below _SEMANTIC_FLOOR are dropped: with a weak
        embedder and a lexically non-matching query, results are empty."""
        docs = [
            {"name": f"tool_{i}", "description": "", "server": "s", "inputSchema": {}, "tags": []}
            for i in range(3)
        ]
        idx = ToolIndex()
        await idx.rebuild(docs)
        idx._embeddings = np.array([[0.05, 0.9987492]] * 3, dtype=np.float32)
        idx._embedder = _FixedEmbedder([1.0, 0.0])  # cos ≈ 0.05 for every doc
        idx._use_embeddings = True
        assert idx.search("zzz_semanticonly", top_k=10) == []

    async def test_bm25_relative_floor_trims_tail(self):
        """Only BM25 hits within _BM25_REL_FLOOR of the best survive: three
        docs share a query token but only the dominant one is returned."""
        docs = [{"name": "primary", "description": "alpha beta", "server": "s", "inputSchema": {}, "tags": []}]
        docs += [
            {"name": f"low_{i}", "description": "alpha " + "filler " * 20, "server": "s", "inputSchema": {}, "tags": []}
            for i in range(2)
        ]
        docs += [
            {"name": f"other_{i}", "description": "unrelated text", "server": "s", "inputSchema": {}, "tags": []}
            for i in range(5)
        ]
        idx = ToolIndex()
        await idx.rebuild(docs)
        idx._use_embeddings = False
        results = idx.search("alpha beta", top_k=10)
        names = [r["name"] for r in results]
        # primary + low_0 + low_1 all contain a query token; the relative floor
        # keeps only the dominant hit.
        assert len(results) == 1
        assert names == ["primary"]


class TestTokenizer:
    """Code-aware tokenizer unit tests."""

    def test_snake_case_preserved(self):
        """snake_case identifiers stay intact AND are split."""
        tokens = ToolIndex._tokenize("brave_web_search")
        assert "brave_web_search" in tokens  # original preserved
        assert "brave" in tokens
        assert "web" in tokens
        assert "search" in tokens

    def test_camel_case_split(self):
        """camelCase tokens are split while preserving original."""
        tokens = ToolIndex._tokenize("getHTTPResponse")
        assert "gethttpresponse" in tokens  # original lowered
        assert "get" in tokens
        assert "http" in tokens
        assert "response" in tokens

    def test_digit_boundary(self):
        """Digit boundaries are split."""
        tokens = ToolIndex._tokenize("parse2Things")
        assert "parse2things" in tokens  # original
        assert "parse" in tokens
        assert "2" in tokens
        assert "things" in tokens

    def test_natural_language(self):
        """Natural language text splits on whitespace."""
        tokens = ToolIndex._tokenize("Search the web using Brave API")
        assert "search" in tokens
        assert "web" in tokens
        assert "brave" in tokens
        assert "api" in tokens

    def test_dedup(self):
        """Duplicate tokens are removed."""
        # "file file_read file" → many "file" tokens, but only one kept
        tokens = ToolIndex._tokenize("file file_read file")
        count = sum(1 for t in tokens if t == "file")
        assert count == 1

    def test_empty_input(self):
        """Empty string returns empty list."""
        assert ToolIndex._tokenize("") == []
        assert ToolIndex._tokenize("   ") == []

    def test_list_type_coerced(self):
        """v2 SDK list-form fields (e.g. type: ['string', 'null']) tokenize."""
        tokens = ToolIndex._tokenize(["string", "null"])
        assert "string" in tokens
        assert "null" in tokens

    def test_build_doc_tokens_v2_list_type(self):
        """_build_doc_tokens survives v2 list-form param types."""
        doc = {
            "name": "x", "server": "s", "description": "",
            "inputSchema": {"properties": {"q": {"type": ["string", "null"]}}},
        }
        tokens = ToolIndex._build_doc_tokens(doc)
        assert "string" in tokens


class TestSchemaAwareSearch:
    """Search tests that verify inputSchema inclusion."""

    @pytest.fixture
    def schema_rich_docs(self):
        return [
            {
                "name": "create_issue",
                "description": "Create a GitHub issue",
                "server": "github",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repository name", "enum": ["owner/repo"]},
                        "title": {"type": "string"},
                        "labels": {"type": "array", "enum": ["bug", "feature", "docs"]},
                    }
                }
            },
            {
                "name": "search_code",
                "description": "Search code on GitHub",
                "server": "github",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "language": {"type": "string", "enum": ["python", "javascript", "rust"]},
                    }
                }
            },
            {
                "name": "read_file",
                "description": "Read file from disk",
                "server": "filesystem",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                    }
                }
            },
        ]

    @pytest.fixture
    async def schema_index(self, schema_rich_docs):
        idx = ToolIndex()
        await idx.rebuild(schema_rich_docs)
        return idx

    async def test_search_by_param_name(self, schema_index):
        """Searching 'repo' finds create_issue (has 'repo' parameter)."""
        results = schema_index.search("repo")
        assert len(results) >= 1
        top_names = {r["name"] for r in results[:3]}
        assert "create_issue" in top_names

    async def test_search_by_enum_value(self, schema_index):
        """Searching 'bug' finds create_issue (has 'bug' enum)."""
        results = schema_index.search("bug")
        assert len(results) >= 1
        top_names = {r["name"] for r in results[:3]}
        assert "create_issue" in top_names

    async def test_search_by_enum_language(self, schema_index):
        """Searching 'python' finds search_code (has 'python' enum)."""
        results = schema_index.search("python")
        assert len(results) >= 1
        top_names = {r["name"] for r in results[:3]}
        assert "search_code" in top_names

    async def test_search_by_param_description(self, schema_index):
        """Searching 'query' matches param description."""
        results = schema_index.search("search query")
        assert len(results) >= 1
        top_names = {r["name"] for r in results[:3]}
        assert "search_code" in top_names

    async def test_inputschema_in_search_results(self, schema_index):
        """Search results include inputSchema for 2-hop flow."""
        results = schema_index.search("create")
        assert len(results) >= 1
        assert "inputSchema" in results[0]
        assert isinstance(results[0]["inputSchema"], dict)
        # Should have at least one property
        props = results[0]["inputSchema"].get("properties", {})
        assert len(props) >= 1

    async def test_tool_name_stronger_than_param(self, schema_index, schema_rich_docs):
        """Searching for exact tool name 'create_issue' returns that tool at top.
        With embeddings, exact tool name matches strongly because the tool name
        appears in the doc text."""
        results = schema_index.search("create_issue")
        assert len(results) >= 1
        assert results[0]["name"] == "create_issue"


class TestResolveEmbeddingModel:
    """resolve_embedding_model の純粋関数テスト（fastembed 不要）。"""

    def test_unsupported_model_falls_back(self):
        """非対応モデルは DEFAULT_EMBEDDING_MODEL にフォールバックする。"""
        supported = {"sentence-transformers/all-minilm-l6-v2"}
        result = resolve_embedding_model("cl-nagoya/ruri-v3-30m", supported)
        assert result == DEFAULT_EMBEDDING_MODEL

    def test_supported_model_kept(self):
        """対応モデルはそのまま返される。"""
        supported = {"sentence-transformers/all-minilm-l6-v2"}
        model = "sentence-transformers/all-MiniLM-L6-v2"
        assert resolve_embedding_model(model, supported) == model

    def test_support_unknown_returns_as_is(self):
        """fastembed 不在などでサポート状況が不明 (None) ならそのまま返す。"""
        model = "any/model-name"
        assert resolve_embedding_model(model, None) == model

    def test_supported_matching_is_case_insensitive(self):
        """対応判定は大文字小文字を無視する。"""
        supported = {"sentence-transformers/all-minilm-l6-v2"}
        model = "SENTENCE-TRANSFORMERS/ALL-MINILM-L6-V2"
        assert resolve_embedding_model(model, supported) == model


def test_default_embedding_model_is_supported():
    """デフォルト埋め込みモデルは fastembed 対応の軽量モデル。"""
    assert DEFAULT_EMBEDDING_MODEL == "sentence-transformers/all-MiniLM-L6-v2"


class TestTagsInIndex:
    """Feature A: サーバータグを検索インデックスに注入する。"""

    @pytest.fixture
    async def tagged_index(self):
        docs = [
            {
                "name": "deploy_app",
                "description": "Ship the artifact to the cluster",
                "server": "infra",
                "inputSchema": {"type": "object", "properties": {}},
                "tags": ["kubernetes", "release"],
            },
            {
                "name": "cook_pasta",
                "description": "Boil water and drain",
                "server": "kitchen",
                "inputSchema": {"type": "object", "properties": {}},
                "tags": [],
            },
        ]
        idx = ToolIndex()
        await idx.rebuild(docs)
        idx._use_embeddings = False  # env-independent: exercise the BM25 path
        return idx

    async def test_bm25_hits_tag_only_keyword(self, tagged_index):
        """説明に無くタグにのみ含まれる語句で BM25 ヒットする。"""
        results = tagged_index.search("kubernetes")
        names = [r["name"] for r in results]
        assert "deploy_app" in names
        assert "cook_pasta" not in names

    async def test_all_result_paths_carry_tags(self, tagged_index):
        """検索結果の全経路（promotion / bm25）で tags キーが同一形状で出る。"""
        # promotion 経路（name トークン一致）+ bm25 本体
        results = tagged_index.search("deploy_app")
        assert results, "promotion 経路の結果が空"
        for r in results:
            assert "tags" in r
        by_name = {r["name"]: r for r in results}
        assert by_name["deploy_app"]["tags"] == ["kubernetes", "release"]
        # bm25 本体のみ（説明一致、tags 無し doc）
        results2 = tagged_index.search("boil water")
        assert results2
        for r in results2:
            assert "tags" in r
        assert {r["name"] for r in results2} == {"cook_pasta"}
        assert results2[0]["tags"] == []

    async def test_doc_without_tags_key_gets_empty_list(self):
        """tags キー自体が無い doc でも結果の tags は []（形状統一）。"""
        idx = ToolIndex()
        await idx.rebuild([
            {"name": "legacy_tool", "description": "Old doc without tags",
             "server": "legacy", "inputSchema": {}},
        ])
        idx._use_embeddings = False
        results = idx.search("legacy_tool")
        assert results
        for r in results:
            assert r["tags"] == []

    async def test_tf_fallback_carry_tags(self):
        """小コーパス TF フォールバック経路でも tags が出る。"""
        docs = [
            {"name": f"tool_{i}", "description": "generic helper", "server": "s",
             "inputSchema": {}, "tags": ["shared"] if i == 0 else []}
            for i in range(3)
        ]
        idx = ToolIndex()
        await idx.rebuild(docs)
        idx._use_embeddings = False
        # 全 query 語が全 doc に出現 → BM25 IDF 全て負 → TF フォールバック発動
        results = idx.search("generic helper")
        assert results, "TF フォールバックが発動していない"
        for r in results:
            assert "tags" in r
        assert {r["name"] for r in results} == {"tool_0", "tool_1", "tool_2"}


class TestUseEmbeddingsSetting:
    """Feature B: use_embeddings ランタイム設定（env 変数はハードキル）。"""

    def test_effective_flag_respects_construction_args(self, monkeypatch):
        monkeypatch.delenv("MCP_HUB_EMBEDDING", raising=False)
        idx = ToolIndex(use_embeddings=True)
        assert idx.use_embeddings is _HAS_FASTEMBED
        idx_off = ToolIndex(use_embeddings=False)
        assert idx_off.use_embeddings is False

    def test_runtime_toggle_off(self):
        """use_embeddings=True で起動した index を set_use_embeddings(False) で止められる。"""
        idx = ToolIndex(use_embeddings=False)
        idx.set_use_embeddings(True)  # fastembed 不在なら False のまま（据 _HAS_FASTEMBED）
        idx.set_use_embeddings(False)
        assert idx.use_embeddings is False

    @pytest.mark.skipif(not _HAS_FASTEMBED, reason="fastembed not installed")
    def test_runtime_toggle_on_without_env_kill(self, monkeypatch):
        monkeypatch.delenv("MCP_HUB_EMBEDDING", raising=False)
        idx = ToolIndex(use_embeddings=False)
        assert idx.use_embeddings is False
        idx.set_use_embeddings(True)
        assert idx.use_embeddings is True

    def test_env_hard_kill_beats_setting(self, monkeypatch):
        """MCP_HUB_EMBEDDING=0 は設定より優先（ハードキル、アーキテクト決定）。"""
        monkeypatch.setenv("MCP_HUB_EMBEDDING", "0")
        idx = ToolIndex(use_embeddings=True)
        assert idx.use_embeddings is False
        idx.set_use_embeddings(True)
        assert idx.use_embeddings is False
