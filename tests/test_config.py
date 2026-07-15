import json
import pytest
from mcp_hub.config import load_config


class TestLoadConfig:
    def test_loads_valid_config(self, tmp_path):
        config_file = tmp_path / "hub.config.json"
        config_file.write_text(json.dumps({
            "version": 1,
            "mcpServers": {
                "test": {"command": "echo", "args": ["hello"]}
            }
        }))
        config = load_config(str(config_file))
        assert "test" in config.servers
        assert config.servers["test"]["command"] == "echo"

    def test_keeps_env_vars_raw(self, tmp_path):
        """load_config() はテンプレートを展開せず、そのまま保持する。
        展開は proxy_manager._create_proxy() で行われる。"""
        config_file = tmp_path / "hub.config.json"
        config_file.write_text(json.dumps({
            "version": 1,
            "mcpServers": {
                "api": {"url": "https://${MY_KEY}.example.com"}
            }
        }))
        config = load_config(str(config_file))
        assert config.servers["api"]["url"] == "https://${MY_KEY}.example.com"

    def test_missing_file_returns_empty(self, tmp_path):
        config_file = tmp_path / "hub.config.json"
        assert not config_file.exists()
        config = load_config(str(config_file))
        assert not config_file.exists()  # file creation is store.py's job
        assert len(config.servers) == 0  # empty HubConfig when missing

    def test_invalid_json_raises(self, tmp_path):
        config_file = tmp_path / "bad.json"
        config_file.write_text("{invalid json!!!")
        with pytest.raises(ValueError, match="Invalid config"):
            load_config(str(config_file))

    def test_disabled_servers_are_skipped(self, tmp_path):
        config_file = tmp_path / "hub.config.json"
        config_file.write_text(json.dumps({
            "version": 1,
            "mcpServers": {
                "active": {"command": "echo", "args": ["hello"]},
                "off": {"command": "echo", "args": ["nope"], "disabled": True},
            }
        }))
        config = load_config(str(config_file))
        assert "active" in config.servers
        assert "off" not in config.servers

    def test_servers_retain_tags_field(self, tmp_path):
        config_file = tmp_path / "hub.config.json"
        config_file.write_text(json.dumps({
            "version": 1,
            "mcpServers": {
                "fetch": {"command": "npx", "args": ["-y", "server-fetch"], "tags": ["web"]},
            }
        }))
        config = load_config(str(config_file))
        assert config.servers["fetch"]["tags"] == ["web"]
