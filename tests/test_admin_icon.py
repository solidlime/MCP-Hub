"""管理画面のアイコン配信の回帰テスト。"""

import pytest
from httpx import ASGITransport, AsyncClient

from src.mcp_hub.main import create_app


@pytest.fixture
async def client():
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/icon.svg", "/admin/icon.svg"])
async def test_admin_icon_served(client, path):
    resp = await client.get(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/svg+xml"
    assert resp.content.startswith(b"<svg")


@pytest.mark.asyncio
async def test_index_links_icon(client):
    html = (await client.get("/admin/")).text
    assert 'link rel="icon" type="image/svg+xml" href="icon.svg"' in html
    assert '<img class="icon" src="icon.svg"' in html
