"""FIX-5: 起動時レジリエンス (PORT/破損JSON/型検証/bootstrap)。"""

import builtins
import json
import subprocess

from mcp_hub import bootstrap
from mcp_hub.config import load_config
from mcp_hub.main import _get_port
from mcp_hub.store import JsonStore


class TestPortFallback:
    def test_garbage_port_falls_back(self, monkeypatch):
        monkeypatch.setenv("MCP_HUB_PORT", "garbage")
        assert _get_port() == 26263

    def test_out_of_range_port_falls_back(self, monkeypatch):
        monkeypatch.setenv("MCP_HUB_PORT", "99999")
        assert _get_port() == 26263

    def test_valid_port_passes_through(self, monkeypatch):
        monkeypatch.setenv("MCP_HUB_PORT", "8080")
        assert _get_port() == 8080


class TestConfigTypeValidation:
    def test_non_string_log_level_falls_back(self, tmp_path):
        p = tmp_path / "hub.config.json"
        p.write_text(json.dumps({"version": 1, "log_level": 123, "mcpServers": {}}))
        assert load_config(str(p)).log_level == "info"

    def test_non_bool_use_embeddings_falls_back(self, tmp_path):
        p = tmp_path / "hub.config.json"
        p.write_text(
            json.dumps({"version": 1, "use_embeddings": "yes", "mcpServers": {}})
        )
        assert load_config(str(p)).use_embeddings is True


class TestCorruptStore:
    def test_corrupt_json_returns_defaults_and_backs_up(self, tmp_path):
        (tmp_path / "hub.config.json").write_text("{broken!!!")
        store = JsonStore(data_dir=str(tmp_path))
        assert store._do_read() == {"version": 1, "log_level": "info", "mcpServers": {}}
        assert (tmp_path / "hub.config.corrupt.bak").exists()


class TestBootstrapFallback:
    def test_fastembed_install_failure_does_not_raise(self, monkeypatch, tmp_path):
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "fastembed":
                raise ImportError("no fastembed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        def boom(*args, **kwargs):
            raise subprocess.CalledProcessError(1, "uv")

        monkeypatch.setattr(subprocess, "run", boom)
        monkeypatch.setattr(bootstrap, "EXTRAS_DIR", str(tmp_path))
        bootstrap._ensure_fastembed()  # raises ならテスト失敗

    def test_non_https_url_refused(self):
        import pytest

        with pytest.raises(ValueError, match="non-https"):
            bootstrap._download_and_extract("http://evil.example/x.tar.gz", "/tmp")
