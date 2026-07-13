"""
Progressive Discovery meta-tools with BM25 search.
Exposes 3 tools instead of all child server tools.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from rank_bm25 import BM25Okapi

logger = logging.getLogger(__name__)

MCP_HUB_TAGS_HEADER = "X-MCP-Hub-Tags"


class ToolIndex:
    """BM25 search over all proxied tools. Thread-safe via asyncio.Lock."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._documents: list[dict] = []  # [{server, name, description, inputSchema}, ...]
        self._bm25: BM25Okapi | None = None
        self._corpus: list[list[str]] = []

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return text.lower().split()

    async def rebuild(self, documents: list[dict]) -> None:
        """Rebuild index from pre-built tool documents.
        Each document: {server, name, description, inputSchema}.
        Caller is responsible for building the document list.
        """
        async with self._lock:
            self._documents = documents
            self._corpus = [
                self._tokenize(f"{d['server']} {d['name']} {d.get('description', '')}")
                for d in documents
            ]
            self._bm25 = BM25Okapi(self._corpus) if self._corpus else None
        logger.info("ToolIndex rebuilt: %d tools indexed", len(documents))

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        """Search tools by keyword. Returns list of {server, name, description, score}.
        Read-only — does not modify shared state, safe without lock."""
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
                "score": round(float(scores[idx]), 4),
            })
        return results

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

        Use this FIRST to discover available tools before calling get_tool_schema or execute_tool.

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
        """
        try:
            result = await self._execute_tool(server, tool_name, arguments)
            return result
        except ValueError as e:
            return json.dumps({"error": str(e)})
        except Exception as e:
            return json.dumps({"error": f"Tool execution failed: {e}"})


def create_meta_app(
    proxy_manager,  # ProxyManager instance
) -> FastMCP:
    """Create a FastMCP app with meta-tools."""
    mcp = FastMCP("MCP Hub Meta")
    index = ToolIndex()

    # Build initial index from all connected proxy tools
    async def rebuild_index():
        all_tools = []
        for server_name, proxy in proxy_manager._proxies.items():
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
        return await meta.search_tools(query, top_k)

    @mcp.tool()
    async def get_tool_schema(server: str, tool_name: str) -> str:
        return await meta.get_tool_schema(server, tool_name)

    @mcp.tool()
    async def execute_tool(server: str, tool_name: str, arguments: dict[str, Any]) -> Any:
        return await meta.execute_tool(server, tool_name, arguments)

    # Rebuild index after server changes
    mcp._index = index
    mcp._meta = meta
    mcp.rebuild_index = rebuild_index

    return mcp
