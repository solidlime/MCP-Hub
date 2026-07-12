"""
Configuration file loader for MCP Hub.
Reads hub.config.json, applies env expansion.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .env_expand import expand_env_vars

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATHS = [
    "hub.config.json",
    "~/.config/mcp-hub/config.json",
]


@dataclass
class HubConfig:
    servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    version: int = 1
    log_level: str = "info"


def _resolve_path(path: str) -> Path:
    """Resolve ~ and relative paths."""
    return Path(path).expanduser().resolve()


def load_config(config_path: str | None = None) -> HubConfig:
    """Load and parse hub.config.json.

    Priority:
    1. Explicit config_path argument
    2. MCP_HUB_CONFIG env var
    3. Default paths (hub.config.json, ~/.config/mcp-hub/config.json)
    """
    path = config_path or os.environ.get("MCP_HUB_CONFIG")
    if path:
        resolved = _resolve_path(path)
        if not resolved.exists():
            logger.warning("Config file not found: %s", resolved)
            return HubConfig()
        return _parse_config(resolved)

    for default_path in DEFAULT_CONFIG_PATHS:
        resolved = _resolve_path(default_path)
        if resolved.exists():
            logger.info("Using config: %s", resolved)
            return _parse_config(resolved)

    logger.info("No config file found. Starting with empty server list.")
    return HubConfig()


def _parse_config(filepath: Path) -> HubConfig:
    """Parse and validate a config file."""
    try:
        raw = json.loads(filepath.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid config JSON in {filepath}: {e}") from e

    version = raw.get("version", 1)
    if not isinstance(version, int) or version < 1:
        raise ValueError(f"Unsupported config version: {version}")

    log_level = raw.get("log_level", "info")
    raw_servers = raw.get("mcpServers", raw.get("servers", {}))

    if not isinstance(raw_servers, dict):
        raise ValueError(f"mcpServers must be a dict, got {type(raw_servers)}")

    servers: dict[str, dict] = {}
    for name, cfg in raw_servers.items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("disabled"):
            logger.info("Skipping disabled server '%s'", name)
            continue
        try:
            servers[name] = expand_env_vars(cfg)
        except ValueError as e:
            logger.warning("Skipping server '%s': %s", name, e)

    return HubConfig(
        servers=servers,
        version=version,
        log_level=log_level,
    )
