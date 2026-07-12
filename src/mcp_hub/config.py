"""
Configuration file loader for MCP Hub.
Reads {MCP_HUB_DATA_DIR}/hub.config.json, auto-generates if missing.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .env_expand import expand_env_vars

logger = logging.getLogger(__name__)

# 設定ファイルが存在しない場合に自動生成されるデフォルト構成
DEFAULT_CONFIG = {
    "version": 1,
    "log_level": "info",
    "mcpServers": {
        "fetch": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-fetch"],
            "tags": ["web"],
        },
        "filesystem": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            "tags": ["local"],
        },
        "sequential-thinking": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
            "tags": ["reasoning"],
        },
        "git": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-git", "--repository", "."],
            "tags": ["vcs"],
        },
        "puppeteer": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-puppeteer"],
            "tags": ["browser"],
        },
        "brave-search": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-brave-search"],
            "env": {"BRAVE_API_KEY": "${BRAVE_API_KEY:-}"},
            "tags": ["search", "web"],
        },
    },
}


@dataclass
class HubConfig:
    servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    version: int = 1
    log_level: str = "info"


def _data_dir() -> str:
    return os.environ.get("MCP_HUB_DATA_DIR", "data")


def _config_path(explicit_path: str | None = None) -> Path:
    if explicit_path:
        return Path(explicit_path).expanduser().resolve()
    return (Path(_data_dir()) / "hub.config.json").expanduser().resolve()


def load_config(config_path: str | None = None) -> HubConfig:
    """{MCP_HUB_DATA_DIR}/hub.config.json を読み込む。

    存在しない場合はデフォルト構成で自動生成する。
    """
    path = _config_path(config_path)
    if not path.exists():
        logger.info("Config not found: %s — generating default.", path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Using config: %s", path)
    return _parse_config(path)


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
