"""full_info_tools 設定 API のテスト。"""
import pytest
from fastapi.testclient import TestClient

from mcp_hub.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as c:
        yield c


class TestFullInfoSettingsAPI:
    def test_get_settings_includes_full_info_tools(self, client):
        r = client.get("/admin/api/settings")
        assert r.status_code == 200
        body = r.json()
        assert "full_info_tools" in body
        assert body["full_info_tools"] == []

    def test_patch_sets_full_info_tools(self, client):
        r = client.patch("/admin/api/settings", json={"full_info_tools": ["fetch_fetch"]})
        assert r.status_code == 200
        assert r.json()["full_info_tools"] == ["fetch_fetch"]

        r2 = client.get("/admin/api/settings")
        assert r2.json()["full_info_tools"] == ["fetch_fetch"]

    def test_patch_rejects_non_list(self, client):
        r = client.patch("/admin/api/settings", json={"full_info_tools": "fetch_fetch"})
        assert r.status_code == 422

    def test_patch_rejects_entry_without_underscore(self, client):
        r = client.patch("/admin/api/settings", json={"full_info_tools": ["fetch"]})
        assert r.status_code == 422

    def test_patch_rejects_non_string_entry(self, client):
        r = client.patch("/admin/api/settings", json={"full_info_tools": [123]})
        assert r.status_code == 422

    def test_patch_meta_mode_still_works(self, client):
        r = client.patch("/admin/api/settings", json={"meta_mode": True})
        assert r.status_code == 200
        assert r.json()["meta_mode"] is True
