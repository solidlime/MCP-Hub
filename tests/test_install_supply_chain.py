"""install/uninstall 経路の供給網保護（#8）.

許可ホスト / レジストリ固定 / 監査ログ / 失敗時の挙動を検証する。
"""

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient

from mcp_hub import admin_router
from mcp_hub.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_HUB_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_exec(monkeypatch):
    """create_subprocess_exec を差し替えて argv を捕捉する。"""
    state = {"calls": [], "returncode": 0}

    class FakeProc:
        def __init__(self):
            self.returncode = state["returncode"]

        async def communicate(self):
            return (b"out", b"err")

    async def _exec(*argv, **kwargs):
        state["calls"].append(list(argv))
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    return state


@pytest.fixture(autouse=True)
def _extras(tmp_path, monkeypatch):
    monkeypatch.setattr(admin_router, "_EXTRAS_DIR", str(tmp_path / "pip-extras"))
    for env in (
        "MCP_HUB_INSTALL_ALLOWED_HOSTS",
        "MCP_HUB_NPM_REGISTRY",
        "MCP_HUB_PIP_INDEX_URL",
    ):
        monkeypatch.delenv(env, raising=False)


class TestAllowedHosts:
    def test_url_spec_host_allowlist_enforced(self, client, monkeypatch, fake_exec):
        monkeypatch.setenv("MCP_HUB_INSTALL_ALLOWED_HOSTS", "github.com")
        r = client.post(
            "/admin/api/tools/install",
            json={"manager": "pip", "packages": ["https://evil.example/pkg.tar.gz"]},
        )
        assert r.status_code == 400
        assert "allowlist" in r.json()["detail"]
        assert fake_exec["calls"] == []  # 実行されない

    def test_allowlisted_host_passes(self, client, monkeypatch, fake_exec):
        monkeypatch.setenv("MCP_HUB_INSTALL_ALLOWED_HOSTS", "github.com")
        r = client.post(
            "/admin/api/tools/install",
            json={"manager": "pip", "packages": ["git+https://github.com/u/r.git"]},
        )
        assert r.status_code == 200
        assert fake_exec["calls"]

    def test_allowlist_unset_allows_any_https_host(self, client, fake_exec):
        r = client.post(
            "/admin/api/tools/install",
            json={"manager": "pip", "packages": ["https://example.com/pkg.tar.gz"]},
        )
        assert r.status_code == 200

    def test_plain_names_unaffected(self, client, monkeypatch, fake_exec):
        monkeypatch.setenv("MCP_HUB_INSTALL_ALLOWED_HOSTS", "github.com")
        r = client.post(
            "/admin/api/tools/install",
            json={"manager": "pip", "packages": ["yt-dlp"]},
        )
        assert r.status_code == 200


class TestRegistries:
    def test_npm_registry_flag_when_configured(self, client, monkeypatch, fake_exec):
        monkeypatch.setenv("MCP_HUB_NPM_REGISTRY", "https://npm.internal.example")
        r = client.post(
            "/admin/api/tools/install",
            json={"manager": "npm", "packages": ["some-mcp-server"]},
        )
        assert r.status_code == 200
        argv = fake_exec["calls"][0]
        assert argv[argv.index("--registry") + 1] == "https://npm.internal.example"

    def test_npm_registry_default_keeps_argv(self, client, fake_exec):
        r = client.post(
            "/admin/api/tools/install",
            json={"manager": "npm", "packages": ["some-mcp-server"]},
        )
        assert r.status_code == 200
        assert fake_exec["calls"][0] == ["npm", "install", "some-mcp-server"]

    def test_insecure_registry_ignored(self, client, monkeypatch, fake_exec, caplog):
        monkeypatch.setenv("MCP_HUB_NPM_REGISTRY", "http://npm.internal.example")
        with caplog.at_level(logging.WARNING, logger="mcp_hub.admin_router"):
            r = client.post(
                "/admin/api/tools/install",
                json={"manager": "npm", "packages": ["some-mcp-server"]},
            )
        assert r.status_code == 200
        assert "http://npm.internal.example" not in fake_exec["calls"][0]
        assert "insecure" in caplog.text

    def test_pip_index_flag_when_configured(self, client, monkeypatch, fake_exec):
        monkeypatch.setenv("MCP_HUB_PIP_INDEX_URL", "https://mirror.internal/simple")
        r = client.post(
            "/admin/api/tools/install", json={"manager": "pip", "packages": ["yt-dlp"]}
        )
        assert r.status_code == 200
        argv = fake_exec["calls"][0]
        assert argv[argv.index("--index-url") + 1] == "https://mirror.internal/simple"
        assert argv[:3] == ["pip", "install", "--target"]

    def test_pip_index_default_keeps_argv(self, client, fake_exec):
        r = client.post(
            "/admin/api/tools/install", json={"manager": "pip", "packages": ["yt-dlp"]}
        )
        assert r.status_code == 200
        assert "--index-url" not in fake_exec["calls"][0]

    def test_uv_tool_index_flag_when_configured(self, client, monkeypatch, fake_exec):
        monkeypatch.setenv("MCP_HUB_PIP_INDEX_URL", "https://mirror.internal/simple")
        r = client.post(
            "/admin/api/tools/install",
            json={
                "manager": "uv-tool",
                "packages": ["some-mcp-server"],
                "constraints": ["mcp<2"],
            },
        )
        assert r.status_code == 200
        argv = fake_exec["calls"][0]
        assert argv[:4] == ["uv", "tool", "install", "--with"]
        assert argv[argv.index("--index-url") + 1] == "https://mirror.internal/simple"


class TestFailureHandling:
    def test_missing_manager_binary_returns_503(self, client, monkeypatch):
        async def missing_exec(*argv, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", argv[0])

        monkeypatch.setattr(asyncio, "create_subprocess_exec", missing_exec)
        r = client.post(
            "/admin/api/tools/install", json={"manager": "pip", "packages": ["yt-dlp"]}
        )
        assert r.status_code == 503
        assert "pip" in r.json()["detail"]

    def test_nonzero_exit_is_reported_not_raised(self, client, fake_exec):
        fake_exec["returncode"] = 1
        r = client.post(
            "/admin/api/tools/install",
            json={"manager": "pip", "packages": ["does-not-exist==0.0.1"]},
        )
        assert r.status_code == 200
        assert r.json()["success"] is False


class TestAuditLog:
    def test_install_ok_is_audited(self, client, fake_exec, caplog):
        with caplog.at_level(logging.INFO, logger="mcp_hub.admin_router"):
            r = client.post(
                "/admin/api/tools/install",
                json={"manager": "pip", "packages": ["yt-dlp"]},
            )
        assert r.status_code == 200
        assert '"event": "pkg.install"' in caplog.text
        assert '"manager": "pip"' in caplog.text
        assert '"ok": true' in caplog.text

    def test_install_failure_is_audited(self, client, fake_exec, caplog):
        fake_exec["returncode"] = 1
        with caplog.at_level(logging.INFO, logger="mcp_hub.admin_router"):
            client.post(
                "/admin/api/tools/install",
                json={"manager": "npm", "packages": ["broken-pkg"]},
            )
        assert '"event": "pkg.install"' in caplog.text
        assert '"ok": false' in caplog.text

    def test_uninstall_is_audited(self, client, fake_exec, caplog):
        with caplog.at_level(logging.INFO, logger="mcp_hub.admin_router"):
            client.post(
                "/admin/api/tools/uninstall",
                json={"manager": "npm", "packages": ["some-mcp-server"]},
            )
        assert '"event": "pkg.uninstall"' in caplog.text
