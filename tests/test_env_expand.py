import os
import pytest
from mcp_hub.env_expand import expand_env_vars


class TestExpandString:
    def test_passthrough_no_placeholders(self):
        assert expand_env_vars("hello world") == "hello world"

    def test_expand_simple_var(self, monkeypatch):
        monkeypatch.setenv("FOO", "bar")
        assert expand_env_vars("${FOO}") == "bar"

    def test_expand_var_in_string(self, monkeypatch):
        monkeypatch.setenv("NAME", "world")
        assert expand_env_vars("hello ${NAME}") == "hello world"

    def test_missing_var_raises(self):
        with pytest.raises(ValueError, match="NOT_EXIST"):
            expand_env_vars("${NOT_EXIST}")

    def test_var_with_default_uses_value(self, monkeypatch):
        monkeypatch.setenv("FOO", "bar")
        assert expand_env_vars("${FOO:-default}") == "bar"

    def test_var_with_default_falls_back(self):
        assert expand_env_vars("${NOT_EXIST:-default}") == "default"

    def test_empty_default(self):
        assert expand_env_vars("${NOT_EXIST:-}") == ""

    def test_multiple_vars(self, monkeypatch):
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        assert expand_env_vars("${A} ${B}") == "1 2"

    def test_non_string_passthrough(self):
        assert expand_env_vars(42) == 42
        assert expand_env_vars(None) is None
        assert expand_env_vars(True) is True


class TestExpandDict:
    def test_expand_in_dict(self, monkeypatch):
        monkeypatch.setenv("KEY", "secret")
        config = {"url": "https://${KEY}.example.com", "port": 8080}
        result = expand_env_vars(config)
        assert result["url"] == "https://secret.example.com"
        assert result["port"] == 8080

    def test_expand_nested_dict(self, monkeypatch):
        monkeypatch.setenv("TOKEN", "abc123")
        config = {
            "env": {"AUTH_TOKEN": "${TOKEN}", "DEBUG": "true"},
            "headers": {"Authorization": "Bearer ${TOKEN}"}
        }
        result = expand_env_vars(config)
        assert result["env"]["AUTH_TOKEN"] == "abc123"
        assert result["headers"]["Authorization"] == "Bearer abc123"


class TestExpandList:
    def test_expand_in_list(self, monkeypatch):
        monkeypatch.setenv("PKG", "server-fetch")
        args = ["-y", "@modelcontextprotocol/${PKG}"]
        result = expand_env_vars(args)
        assert result[1] == "@modelcontextprotocol/server-fetch"
