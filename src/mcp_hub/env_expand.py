"""
Environment variable expansion utility.
Supports Claude Code / hatago compatible syntax:
  - ${VAR}          : Required variable (raises if undefined)
  - ${VAR:-default} : Variable with default fallback
"""

import os
import re
from typing import Any

# Matches ${VAR} or ${VAR:-default}
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_string(value: str) -> str:
    """Expand a single string value."""

    def _replacer(match: re.Match) -> str:
        var_name = match.group(1)
        default = match.group(2)
        env_val = os.environ.get(var_name)
        if env_val is not None:
            return env_val
        if default is not None:
            return default
        raise ValueError(
            f"Environment variable '{var_name}' is not defined and no default provided"
        )

    return _ENV_PATTERN.sub(_replacer, value)


def expand_env_vars(obj: Any) -> Any:
    """Recursively expand env var placeholders in any structure.

    Strings: expand ${VAR} placeholders.
    Dicts: recursively expand values.
    Lists: recursively expand items.
    Other types: returned as-is.
    """
    if isinstance(obj, str):
        return _expand_string(obj)
    if isinstance(obj, dict):
        return {k: expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env_vars(item) for item in obj]
    return obj
