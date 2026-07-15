import pytest
from httpx import ASGITransport, AsyncClient
from src.mcp_hub.main import create_app


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_health_bypasses_auth(client, monkeypatch):
    monkeypatch.setenv("MCP_HUB_API_KEY", "secret")
    resp = await client.get("/admin/api/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_health_trailing_slash_bypasses_auth(client, monkeypatch):
    """FastAPI redirects /admin/api/health/ -> /admin/api/health (307)."""
    monkeypatch.setenv("MCP_HUB_API_KEY", "secret")
    resp = await client.get("/admin/api/health/", follow_redirects=True)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_no_key_configured_allows_all(client, monkeypatch):
    """No key configured -> all requests pass through auth.

    Without lifespan, /admin/api/servers raises RuntimeError
    because registry isn't initialized. That's expected — what matters
    is the request bypasses auth (no 401).
    """
    monkeypatch.delenv("MCP_HUB_API_KEY", raising=False)
    with pytest.raises((RuntimeError, Exception)) as exc_info:
        await client.get("/admin/api/servers")
    # Verify it's the expected backend error, not auth rejection
    assert "Registry not initialized" in str(exc_info.value)


@pytest.mark.asyncio
async def test_wrong_key_returns_401(client, monkeypatch):
    monkeypatch.setenv("MCP_HUB_API_KEY", "correct-key")
    resp = await client.get("/admin/api/servers", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401
    assert "Invalid" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_missing_key_returns_401(client, monkeypatch):
    monkeypatch.setenv("MCP_HUB_API_KEY", "correct-key")
    resp = await client.get("/admin/api/servers")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_correct_key_allows(client, monkeypatch):
    """Correct key -> auth passes. Backend error expected (no lifespan)."""
    monkeypatch.setenv("MCP_HUB_API_KEY", "correct-key")
    with pytest.raises((RuntimeError, Exception)) as exc_info:
        await client.get(
            "/admin/api/servers",
            headers={"X-API-Key": "correct-key"},
        )
    # Verify it's the expected backend error, not auth rejection
    assert "Registry not initialized" in str(exc_info.value)
