"""
Progressive Discovery meta-tools with BM25 search.
Exposes 3 tools instead of all child server tools.
"""

import asyncio
import json
import logging
import re
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from rank_bm25 import BM25Okapi

try:
    from fastembed import TextEmbedding
    import numpy as np
    _HAS_FASTEMBED = True
except ImportError:
    _HAS_FASTEMBED = False

logger = logging.getLogger(__name__)

MCP_HUB_TAGS_HEADER = "X-MCP-Hub-Tags"


class ToolIndex:
    """Embedding-based semantic search over proxied tools, with BM25 fallback.

    Primary search uses fastembed (BAAI/bge-small-en-v1.5) for dense retrieval.
    Falls back to BM25Okapi when fastembed is not available.
    Thread-safe via asyncio.Lock.

    Doc text for embedding: f"{server}/{name}: {description}"
    BM25 uses token duplication to simulate BM25F field weights.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._documents: list[dict] = []  # [{server, name, description, inputSchema}, ...]
        self._bm25: BM25Okapi | None = None
        self._corpus: list[list[str]] = []
        self._embedder: "TextEmbedding | None" = None  # type: ignore[name-defined]
        self._embeddings: "np.ndarray | None" = None  # type: ignore[name-defined]
        self._use_embeddings: bool = _HAS_FASTEMBED

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
        """
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
        _add(doc["name"], copies=5)               # Tool name: ×5
        _add(doc["server"], copies=3)              # Server name: ×3
        _add(doc.get("description", ""), copies=2) # Description: ×2

        # InputSchema fields — included at ×1 (baseline)
        schema = doc.get("inputSchema", {})
        if isinstance(schema, dict):
            for param_name, param_info in schema.get("properties", {}).items():
                _add(param_name, copies=1)  # Parameter name

                if isinstance(param_info, dict):
                    _add(param_info.get("type", ""), copies=1)      # Type
                    _add(param_info.get("description", ""), copies=1)  # Param desc

                    # Enum values are highly specific → ×2
                    for ev in param_info.get("enum", []):
                        if isinstance(ev, str):
                            _add(ev, copies=2)

        return tokens

    # ── Build + Search ────────────────────────────────────────────

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

            # Compute embeddings if fastembed is available
            if self._use_embeddings and documents:
                try:
                    if self._embedder is None:  # type: ignore[truthiness-function]
                        self._embedder = TextEmbedding("BAAI/bge-small-en-v1.5")  # type: ignore[name-defined]
                    doc_texts = [
                        f"{d['server']}/{d['name']}: {d.get('description', '')}"
                        for d in documents
                    ]
                    gen = self._embedder.embed(doc_texts)
                    self._embeddings = np.array(list(gen), dtype=np.float32)  # type: ignore[name-defined]
                except Exception:
                    logger.warning("Embedding failed, falling back to BM25", exc_info=True)
                    self._embeddings = None
                    self._use_embeddings = False
            else:
                self._embeddings = None

        logger.info("ToolIndex rebuilt: %d tools indexed", len(documents))

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        """Search tools by keyword or semantic query.

        Uses embedding-based semantic search when fastembed is available,
        otherwise falls back to BM25 keyword search.

        Returns list of {server, name, description, inputSchema, score}.

        inputSchema is included so the LLM can proceed directly to execute_tool without
        a separate get_tool_schema call.

        Read-only — does not modify shared state, safe without lock.
        """
        if not self._documents:
            return []
        if self._use_embeddings and self._embeddings is not None:
            return self._semantic_search(query, top_k)
        else:
            return self._bm25_search(query, top_k)

    def _semantic_search(self, query: str, top_k: int) -> list[dict]:
        """Dense retrieval via embedding cosine similarity."""
        query_vec = np.array(  # type: ignore[name-defined]
            list(self._embedder.embed([query])), dtype=np.float32  # type: ignore[union-attr]
        ).squeeze(0)
        # L2-normalize query (bge-small produces normalized docs already)
        norm = np.linalg.norm(query_vec)  # type: ignore[name-defined]
        if norm > 0:
            query_vec = query_vec / norm
        # Dot product = cosine similarity (both vectors L2-normalized)
        scores = self._embeddings @ query_vec  # type: ignore[name-defined]
        ranked = sorted(
            range(len(scores)), key=lambda i: scores[i], reverse=True
        )
        docs = self._documents
        results = []
        for idx in ranked[:top_k]:
            score = float(scores[idx])
            doc = docs[idx]
            results.append({
                "server": doc["server"],
                "name": doc["name"],
                "description": doc.get("description", ""),
                "inputSchema": doc.get("inputSchema", {}),
                "score": round(score, 4),
            })
        return results

    def _bm25_search(self, query: str, top_k: int) -> list[dict]:
        """BM25 keyword search (fallback when fastembed unavailable)."""
        if not self._bm25 or not self._corpus:
            return []
        tokens = self._tokenize(query)
        scores = self._bm25.get_scores(tokens)
        docs = self._documents
        ranked = sorted(
            range(len(scores)), key=lambda i: scores[i], reverse=True
        )
        results = []
        for idx in ranked[:top_k]:
            if scores[idx] <= 0:
                break
            doc = docs[idx]
            results.append({
                "server": doc["server"],
                "name": doc["name"],
                "description": doc.get("description", ""),
                "inputSchema": doc.get("inputSchema", {}),
                "score": round(float(scores[idx]), 4),
            })
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
    ):
        self._index = tool_index
        self._execute_tool = execute_tool_fn

    async def search_tools(self, query: str, top_k: int = 10) -> str:
        """Search upstream tools. Always call FIRST before execute_tool.

        Args:
            query: What you want to do (e.g. "read files", "search web")
            top_k: Max results (default 10)
        """
        results = self._index.search(query, top_k)
        if not results:
            return json.dumps({"message": "No matching tools found", "hint": "Try broader keywords or check server connections."}, ensure_ascii=False, indent=2)
        return json.dumps({"results": results}, ensure_ascii=False, indent=2)

    async def execute_tool(self, server: str, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Execute a tool discovered via search_tools.

        Args:
            server: From search_tools results
            tool_name: From search_tools results
            arguments: Use inputSchema from search_tools results
        """
        # Verify tool exists in index before dispatching
        if self._index.get_schema(server, tool_name) is None:
            return json.dumps({
                "error": f"Tool '{tool_name}' not found on server '{server}'.",
                "hint": "Use search_tools first to discover available tools on this server."
            }, ensure_ascii=False, indent=2)
        return await self._execute_tool(server, tool_name, arguments)

    async def list_upstream_tools(self) -> str:
        """List all upstream tools grouped by server. Use for orientation, then search_tools."""
        by_server = self._index.get_tools_by_server()
        if not by_server:
            return json.dumps({"message": "No upstream tools available. Add servers via admin API."}, ensure_ascii=False, indent=2)
        total = sum(len(tools) for tools in by_server.values())
        return json.dumps({
            "total_tools": total,
            "tools_by_server": by_server,
        }, ensure_ascii=False, indent=2)


class MetaApp:
    """Wrapper exposing FastMCP app with clean attribute interface."""

    def __init__(self, mcp: FastMCP, index: ToolIndex, meta: MetaTools, rebuild_fn):
        self.mcp = mcp
        self.index = index
        self.meta_tools = meta
        self.rebuild_index = rebuild_fn


def create_meta_app(
    proxy_manager,  # ProxyManager instance
) -> MetaApp:
    """Create a MetaApp with meta-tools."""
    mcp = FastMCP("MCP Hub Meta")
    index = ToolIndex()

    # Build initial index from all connected proxy tools
    async def rebuild_index():
        all_tools = []
        for server_name, proxy in proxy_manager.get_connected_servers().items():
            try:
                tools = await asyncio.wait_for(proxy.list_tools(), timeout=30.0)
                for t in tools:
                    all_tools.append({
                        "server": server_name,
                        "name": t.name,
                        "description": t.description or "",
                        "inputSchema": getattr(t, "parameters", {}),
                    })
            except Exception:
                logger.warning("Failed to list tools for %s", server_name)
        await index.rebuild(all_tools)

    meta = MetaTools(
        tool_index=index,
        execute_tool_fn=lambda s, t, a: proxy_manager.call_tool(s, t, a),
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
    async def execute_tool(server: str, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Execute a tool discovered via search_tools.

        Args:
            server: From search_tools results
            tool_name: From search_tools results
            arguments: Use inputSchema from search_tools results
        """
        return await meta.execute_tool(server, tool_name, arguments)

    @mcp.tool()
    async def list_upstream_tools() -> str:
        """List all upstream tools grouped by server. Use for orientation, then search_tools."""
        return await meta.list_upstream_tools()

    return MetaApp(mcp=mcp, index=index, meta=meta, rebuild_fn=rebuild_index)
