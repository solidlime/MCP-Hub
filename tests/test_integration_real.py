"""Smoke tests with real ASGI app lifecycle. No mocks."""
import asyncio
import os
import pytest
from httpx import ASGITransport, AsyncClient
from src.mcp_hub.main import create_app


@pytest.mark.integration
@pytest.mark.asyncio
async def test_health_endpoint(tmp_path, monkeypatch):
    """App starts, health endpoint responds."""
    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_HUB_API_KEY", "")

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await asyncio.sleep(0.3)

            resp = await client.get("/admin/api/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_endpoint_mounted(tmp_path, monkeypatch):
    """MCP /mcp/ endpoint is reachable after lifespan startup."""
    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_HUB_API_KEY", "")

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await asyncio.sleep(0.3)

            # Verify the /mcp/ mount exists (trailing slash required)
            resp = await client.get("/mcp/")
            # Mounted endpoint responds — should NOT be 404
            assert resp.status_code != 404, "/mcp/ should be mounted"
