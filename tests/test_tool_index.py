"""
ToolIndex unit tests — pure BM25 search, no FastAPI needed.
"""

import pytest
from mcp_hub.meta_provider import ToolIndex


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
    """ToolIndex BM25 search unit tests."""

    async def test_rebuild_empty(self):
        """Empty doc list — search returns empty."""
        idx = ToolIndex()
        await idx.rebuild([])
        assert idx.search("anything") == []

    async def test_search_exact_match(self, index):
        """Query exact tool name returns the matching tool."""
        results = index.search("brave_web_search")
        assert len(results) == 1
        assert results[0]["name"] == "brave_web_search"
        assert results[0]["server"] == "brave-search"
        assert "score" in results[0]

    async def test_search_by_keyword(self, index):
        """Keyword 'web' matches tools with 'web' in name or description."""
        results = index.search("web")
        assert len(results) >= 2
        names = {r["name"] for r in results}
        assert "brave_web_search" in names  # 'web' in name
        assert "puppeteer_screenshot" in names  # 'web page' in description

    async def test_search_file_keyword(self, index):
        """Keyword 'file' matches file_read and file_write."""
        results = index.search("file")
        assert len(results) >= 2
        names = {r["name"] for r in results}
        assert "file_read" in names
        assert "file_write" in names

    async def test_top_k_limit(self, index):
        """top_k=2 returns at most 2 results."""
        results = index.search("tool", top_k=2)
        assert len(results) <= 2

    async def test_search_no_match(self, index):
        """Query with no match returns empty list."""
        results = index.search("nonexistent_tool_xyz")
        assert results == []
