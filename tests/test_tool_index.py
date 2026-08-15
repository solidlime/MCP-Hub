"""
ToolIndex unit tests — embedding-based search with BM25 fallback.
"""

import pytest
from mcp_hub.config import DEFAULT_EMBEDDING_MODEL
from mcp_hub.meta_provider import ToolIndex, resolve_embedding_model


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
        """Query for a nonsense string. With embeddings: returns results
        (low similarity). With BM25 fallback: returns empty (correct)."""
        results = index.search("zzz_xyzzy_nonexistent_12345")
        if index._embeddings is not None:
            assert len(results) >= 1  # embeddings always return something
        else:
            assert len(results) == 0  # BM25 correctly finds no match


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
        """Exact name match (3 shared tokens) outranks loose partial hits
        (1 token) regardless of document order; score = shared token count."""
        results = memory_corpus_index.search("create memory record memory_create", top_k=10)
        names = [r["name"] for r in results]
        assert names[0] == "memory_create"
        assert names.index("memory_create") < names.index("memory_delete")
        assert names.index("memory_create") < names.index("memory_stats")
        scores = {r["name"]: r["score"] for r in results}
        assert scores["memory_create"] == 3.0
        assert scores["memory_delete"] == 1.0
        assert scores["memory_stats"] == 1.0

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
        """When identifier matches exceed top_k, the top entries follow shared
        token count (desc), not insertion order."""
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
        # Insertion order puts file_1 first; shared-token ranking must win.
        results = idx.search("file read write", top_k=5)
        assert len(results) <= 5
        names = [r["name"] for r in results]
        assert names[0] == "file_read_write"  # 3 shared tokens
        assert names[1] == "read_write"        # 2 shared tokens
        assert results[0]["score"] > results[1]["score"] > results[2]["score"]


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
        supported = {"sentence-transformers/paraphrase-multilingual-minilm-l12-v2"}
        result = resolve_embedding_model("cl-nagoya/ruri-v3-30m", supported)
        assert result == DEFAULT_EMBEDDING_MODEL

    def test_supported_model_kept(self):
        """対応モデルはそのまま返される。"""
        supported = {"sentence-transformers/paraphrase-multilingual-minilm-l12-v2"}
        model = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
        assert resolve_embedding_model(model, supported) == model

    def test_support_unknown_returns_as_is(self):
        """fastembed 不在などでサポート状況が不明 (None) ならそのまま返す。"""
        model = "any/model-name"
        assert resolve_embedding_model(model, None) == model

    def test_supported_matching_is_case_insensitive(self):
        """対応判定は大文字小文字を無視する。"""
        supported = {"sentence-transformers/paraphrase-multilingual-minilm-l12-v2"}
        model = "SENTENCE-TRANSFORMERS/PARAPHRASE-MULTILINGUAL-MINILM-L12-V2"
        assert resolve_embedding_model(model, supported) == model


def test_default_embedding_model_is_supported_multilingual():
    """デフォルト埋め込みモデルは sentence-transformers の多言語対応モデル。"""
    assert (
        DEFAULT_EMBEDDING_MODEL
        == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
