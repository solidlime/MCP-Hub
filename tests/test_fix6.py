"""FIX-6 regression tests: one compact test per fix (fails before, passes after)."""
import asyncio

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER

from mcp_hub.full_info import FullInfoMiddleware
from mcp_hub.lenient_session_manager import LenientSessionManager
from mcp_hub.masking import mask_args, mask_text
from mcp_hub.meta_provider import ToolIndex
from mcp_hub.proxy_manager import ProxyManager
from mcp_hub.state import app_state
from src.mcp_hub.main import create_app


def _make_pm():
    mcp = type("MCP", (), {
        "mount": lambda self, p, namespace=None: None,
        "local_provider": object(),
        "providers": [],
    })()
    return ProxyManager(mcp, {})


def test_search_embed_failure_falls_back_to_bm25():
    """embed 例外 → 500相当のraiseではなくBM25フォールバックで結果を返す。"""
    idx = ToolIndex()
    docs = [{"server": "s", "name": "mytool", "description": "does things",
             "inputSchema": {}, "tags": []}]
    asyncio.run(idx.rebuild(docs))
    idx._use_embeddings = True
    idx._embeddings = np.ones((1, 8), dtype=np.float32)

    class _Boom:
        def embed(self, texts):
            raise RuntimeError("onnx down")

    idx._embedder = _Boom()
    results = idx.search("mytool")
    assert len(results) >= 1


@pytest.mark.parametrize("raw", ["0", "-5"])
def test_max_concurrent_nonpositive_normalized(monkeypatch, raw):
    """MAX_CONCURRENT 0/負 → セマフォ枯渇ではなく1に正規化。"""
    monkeypatch.setenv("MCP_HUB_MAX_CONCURRENT_CALLS", raw)
    pm = _make_pm()
    assert pm._call_semaphore._value == 1


def test_empty_list_tools_preserves_cached_counts():
    """空結果 → _tool_counts=0上書きではなく既存キャッシュ維持。"""
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    pm = _make_pm()
    proxy = SimpleNamespace(list_tools=AsyncMock(return_value=[]))
    pm._tool_cache = {"srv": (0.0, ["a", "b", "c"])}
    pm._tool_counts = {"srv": 3}
    tools = asyncio.run(pm.list_tools_for_server("srv", proxy))
    assert tools == []
    assert pm._tool_counts == {"srv": 3}
    assert pm._tool_cache == {"srv": (0.0, ["a", "b", "c"])}


def test_masks_generic_secret_token_values():
    """非ヒントキーでも著名トークンパターン (ghp_/AKIA等) はマスク。"""
    token = "ghp_abc123DEF456ghi789JKL0"
    assert token not in mask_args({"q": token})
    assert "AKIAIOSFODNN7EXAMPLE" not in mask_text("key=AKIAIOSFODNN7EXAMPLE end", 500)


def test_full_info_entries_prefers_fresh_registry_read(monkeypatch):
    """registry陳腐参照 → _do_read再読込を優先。"""
    from types import SimpleNamespace

    class _FakeRegistry:
        def __init__(self):
            self._data = {"full_info_tools": ["srv_old"]}

        def _do_read(self):
            return {"full_info_tools": ["srv_new"]}

    monkeypatch.setattr(app_state, "registry", _FakeRegistry())
    mw = FullInfoMiddleware(SimpleNamespace(get_connected_servers=lambda: {}),
                            lambda s, t: None)
    assert mw._full_info_entries() == {"srv_new"}


def test_undecodable_session_id_does_not_raise():
    """不正バイト session-id → 500相当のraiseではなく不明扱い。"""
    sm = LenientSessionManager.__new__(LenientSessionManager)
    sm._server_instances = {}
    scope = {"headers": [(MCP_SESSION_ID_HEADER.encode(), b"\xff\xfe-bad")]}
    assert sm._is_unknown_session(scope) is True


@pytest.mark.asyncio
async def test_health_subpath_requires_auth(monkeypatch):
    """未定義 /health/* は免除せず401、厳密なヘルスは200のまま。"""
    monkeypatch.setenv("MCP_HUB_API_KEY", "secret")
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/admin/api/health/evil")
        assert resp.status_code == 401
        assert (await c.get("/admin/api/health")).status_code == 200
