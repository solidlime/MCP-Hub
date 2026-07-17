"""Input validation for upstream server configuration."""
import re
from urllib.parse import urlparse

# Blocked env vars (prevents hijacking the host)
BLOCKED_ENV_VARS = frozenset({
    "PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
    "PYTHONPATH", "PYTHONHOME", "HOME", "SHELL",
    "MCP_HUB_API_KEY", "MCP_HUB_DATA_DIR",
})

# RFC 3986: scheme is case-insensitive
ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

# Pattern: $() subshell execution (BLOCKED)
_DOLLAR_SUBSHELL = re.compile(r"\$\(.*\)")

# Pattern: ${VAR} or ${VAR:-default} (ALLOWED — env var template)
_DOLLAR_TEMPLATE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-.*?)?\}")

# Forbidden characters in command: shell metacharacters
_FORBIDDEN_COMMAND_CHARS = frozenset(";&|`<>")

MAX_COMMAND_LENGTH = 512
MAX_URL_LENGTH = 2048
MAX_ARGS_COUNT = 50
MAX_ARG_LENGTH = 1024
MAX_HEADER_KEY_LENGTH = 256
MAX_HEADER_VALUE_LENGTH = 8192


class ValidationError(ValueError):
    """Raised when server config validation fails."""
    pass


def validate_command(command: str) -> str:
    """Validate a command string. Allows env var templates (${VAR}).

    Blocks: $(), ;, &, |, `, <, >
    Allows: ${VAR}, ${VAR:-default}
    """
    if not command or not isinstance(command, str):
        raise ValidationError("Command must be a non-empty string")
    if len(command) > MAX_COMMAND_LENGTH:
        raise ValidationError(f"Command too long (max {MAX_COMMAND_LENGTH} chars)")

    # Block subshell execution $(...)
    if _DOLLAR_SUBSHELL.search(command):
        raise ValidationError("Command contains subshell execution: $()")

    # Check forbidden shell metacharacters
    for ch in command:
        if ch in _FORBIDDEN_COMMAND_CHARS:
            raise ValidationError(f"Command contains forbidden character: '{ch}'")

    return command


def validate_args(args: list[str]) -> list[str]:
    """Validate argument list. Env var templates are allowed."""
    if not isinstance(args, list):
        raise ValidationError("Args must be a list")
    if len(args) > MAX_ARGS_COUNT:
        raise ValidationError(f"Too many args (max {MAX_ARGS_COUNT})")
    for i, arg in enumerate(args):
        if not isinstance(arg, str):
            raise ValidationError(f"Arg {i} must be a string")
        if len(arg) > MAX_ARG_LENGTH:
            raise ValidationError(f"Arg {i} too long (max {MAX_ARG_LENGTH} chars)")
    return args


def validate_url(url: str) -> str:
    """Validate upstream server URL. Only http/https. RFC 3986 case-insensitive."""
    if not url or not isinstance(url, str):
        raise ValidationError("URL must be a non-empty string")
    if len(url) > MAX_URL_LENGTH:
        raise ValidationError(f"URL too long (max {MAX_URL_LENGTH} chars)")
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise ValidationError(f"Invalid URL: {e}") from e
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        raise ValidationError(
            f"URL scheme '{parsed.scheme}' not allowed. Use http:// or https://"
        )
    return url


def validate_env(env: dict) -> dict:
    """Validate environment variables. Block dangerous overrides."""
    if not isinstance(env, dict):
        raise ValidationError("Env must be a dict")
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValidationError(f"Env key/value must be strings: {key}")
        if len(key) > 256:
            raise ValidationError(f"Env key too long: {key}")
        if len(value) > 4096:
            raise ValidationError(f"Env value too long for {key}")
        if key.upper() in BLOCKED_ENV_VARS or key in BLOCKED_ENV_VARS:
            raise ValidationError(f"Env variable '{key}' is blocked")
    return env


def validate_headers(headers: dict) -> dict[str, str]:
    """Validate custom HTTP headers for remote server connections.

    Allows only dict[str, str] with reasonable size limits.
    """
    if not isinstance(headers, dict):
        raise ValidationError("Headers must be a dict")
    result: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str):
            raise ValidationError(f"Header key must be a string: {key}")
        if not isinstance(value, str):
            raise ValidationError(f"Header value must be a string for key: {key}")
        if re.search(r'[\r\n\x00-\x1f]', key):
            raise ValidationError(f"Header key contains control characters: {key[:50]}")
        if len(key) > MAX_HEADER_KEY_LENGTH:
            raise ValidationError(f"Header key too long (max {MAX_HEADER_KEY_LENGTH} chars): {key[:50]}...")
        if len(value) > MAX_HEADER_VALUE_LENGTH:
            raise ValidationError(f"Header value too long for key '{key}' (max {MAX_HEADER_VALUE_LENGTH} chars)")
        result[key] = value
    return result


def validate_server_config(name: str, config: dict) -> dict:
    """Validate a complete server config (both register and patch)."""
    if not name or not isinstance(name, str):
        raise ValidationError("Server name must be a non-empty string")
    if len(name) > 128:
        raise ValidationError("Server name too long (max 128 chars)")
    if not re.match(r'^[a-zA-Z0-9_.-]+$', name):
        raise ValidationError("Server name contains invalid characters")
    if not isinstance(config, dict):
        raise ValidationError("Config must be a dict")

    has_url = bool(config.get("url"))
    has_command = bool(config.get("command"))
    if not has_url and not has_command:
        raise ValidationError("Either 'url' or 'command' is required")

    if has_url:
        config["url"] = validate_url(config["url"])
    if has_command:
        config["command"] = validate_command(config["command"])
        if "args" in config:
            config["args"] = validate_args(config["args"])
        if "env" in config:
            config["env"] = validate_env(config["env"])
    if "tags" in config:
        tags = config["tags"]
        if not isinstance(tags, list):
            raise ValidationError("Tags must be a list")
        for tag in tags:
            if not isinstance(tag, str) or len(tag) > 64:
                raise ValidationError(f"Invalid tag: {tag}")
    if "headers" in config:
        config["headers"] = validate_headers(config["headers"])
    return config
