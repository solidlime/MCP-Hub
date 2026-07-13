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

logger = logging.getLogger(__name__)

MCP_HUB_TAGS_HEADER = "X-MCP-Hub-Tags"


class ToolIndex:
    """BM25 search over all proxied tools. Thread-safe via asyncio.Lock.

    Indexes tool metadata and inputSchema content with field-aware weighting.
    Uses token duplication to simulate BM25F since rank_bm25 is a plain BM25
    implementation without native field weights.

    Indexed fields (with simulated weights):
        Tool name:      ×5  (highest — exact match is the strongest signal)
        Server name:    ×3  (disambiguates same-named tools across servers)
        Description:    ×2  (natural language description)
        Enum values:    ×2  (concrete values are highly specific)
        Parameter name: ×1  (included for schema-aware search)
        Parameter desc: ×1  (natural language, useful for semantic matching)
        Parameter type: ×1  (weak signal — many tools share common types)
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._documents: list[dict] = []  # [{server, name, description, inputSchema}, ...]
        self._bm25: BM25Okapi | None = None
        self._corpus: list[list[str]] = []

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
        """
        async with self._lock:
            self._documents = documents
            self._corpus = [self._build_doc_tokens(d) for d in documents]
            self._bm25 = BM25Okapi(self._corpus) if self._corpus else None
        logger.info("ToolIndex rebuilt: %d tools indexed", len(documents))

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        """Search tools by keyword. Returns list of {server, name, description, inputSchema, score}.

        inputSchema is included so the LLM can proceed directly to execute_tool without
        a separate get_tool_schema call.

        Read-only — does not modify shared state, safe without lock.
        """
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
        """Search across all upstream server tools by keyword or capability.

        Returns matching tools with their full inputSchema — you can use execute_tool
        directly with the returned server/name and the inputSchema parameters.
        No need for a separate get_tool_schema call.

        Args:
            query: Natural language description of what you want to do (e.g. "read files", "search web")
            top_k: Max results to return (default 10)
        """
        results = self._index.search(query, top_k)
        if not results:
            return json.dumps({"message": "No matching tools found", "hint": "Try broader keywords or check server connections."}, ensure_ascii=False, indent=2)
        return json.dumps({"results": results}, ensure_ascii=False, indent=2)

    async def get_tool_schema(self, server: str, tool_name: str) -> str:
        """Get the full input schema for a specific tool on a specific server.

        ALWAYS call this after search_tools to learn the exact parameters required
        before calling execute_tool.

        Args:
            server: Server name from search_tools results
            tool_name: Tool name from search_tools results
        """
        schema = self._index.get_schema(server, tool_name)
        if schema is None:
            return json.dumps({"error": f"Tool '{tool_name}' not found on server '{server}'"})
        return json.dumps(schema, ensure_ascii=False, indent=2)

    async def execute_tool(self, server: str, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Execute a tool on any upstream server.

        Args:
            server: Server name from search_tools results
            tool_name: Tool name from search_tools results
            arguments: Tool parameters (use get_tool_schema first to learn the format)

        Exceptions propagate to FastMCP for proper JSON-RPC error reporting.
        """
        return await self._execute_tool(server, tool_name, arguments)


def create_meta_app(
    proxy_manager,  # ProxyManager instance
) -> FastMCP:
    """Create a FastMCP app with meta-tools."""
    mcp = FastMCP("MCP Hub Meta")
    index = ToolIndex()

    # Build initial index from all connected proxy tools
    async def rebuild_index():
        all_tools = []
        for server_name, proxy in proxy_manager.get_connected_servers().items():
            try:
                tools = await proxy.list_tools()
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
        """Search across all upstream server tools by keyword or capability.
        
        Returns matching tools with their full inputSchema — use execute_tool
        directly with the returned server/name and inputSchema parameters.
        Always search first before trying to use any tool.
        
        Args:
            query: Natural language description of what you want to do
            top_k: Maximum number of results to return (default 10)
        """
        return await meta.search_tools(query, top_k)

    @mcp.tool()
    async def get_tool_schema(server: str, tool_name: str) -> str:
        """Get the full input schema for a specific tool on a specific server.
        
        Always call this after search_tools to learn the exact parameters needed
        before calling execute_tool.
        
        Args:
            server: Server name from search_tools results
            tool_name: Tool name from search_tools results
        """
        return await meta.get_tool_schema(server, tool_name)

    @mcp.tool()
    async def execute_tool(server: str, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Execute a tool on any upstream server.
        
        Args:
            server: Server name from search_tools results
            tool_name: Tool name from search_tools results (use get_tool_schema first)
            arguments: Tool parameters matching the tool's input schema
        """
        return await meta.execute_tool(server, tool_name, arguments)

    # Rebuild index after server changes
    mcp._index = index
    mcp._meta = meta
    mcp.rebuild_index = rebuild_index

    return mcp
