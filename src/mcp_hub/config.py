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

logger = logging.getLogger(__name__)

# 設定ファイルが存在しない場合に自動生成されるデフォルト構成
# 注: @modelcontextprotocol/server-fetch と server-git は未公開のため除外
DEFAULT_CONFIG = {
    "version": 1,
    "log_level": "info",
    "mcpServers": {
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
        "puppeteer": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-puppeteer"],
            "env": {
                "DOCKER_CONTAINER": "true",
                "ALLOW_DANGEROUS": "true",
            },
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

    ファイルが存在しない場合は空の HubConfig を返す。
    ファイルの自動作成は store.py:JsonStore.init() が担当する。
    """
    path = _config_path(config_path)
    if not path.exists():
        logger.info("Config not found: %s — will be created by store.", path)
        return HubConfig()
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
        servers[name] = cfg  # store templates raw; expansion happens in proxy_manager._create_proxy()

    return HubConfig(
        servers=servers,
        version=version,
        log_level=log_level,
    )


def save_config(servers: dict[str, dict], config_path: str | None = None) -> None:
    """現在のサーバー一覧を hub.config.json に書き戻す（hub.db と同期）。"""
    path = _config_path(config_path)
    data = {
        "version": 1,
        "log_level": "info",
        "mcpServers": servers,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Config saved to %s (%d servers)", path, len(servers))
