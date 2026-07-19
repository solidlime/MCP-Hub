"""
Configuration file loader for MCP Hub.
Reads {MCP_HUB_DATA_DIR}/hub.config.json.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class HubConfig:
    servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    version: int = 1
    log_level: str = "info"
    embedding_model: str = "cl-nagoya/ruri-v3-30m"


def _data_dir() -> str:
    return os.environ.get("MCP_HUB_DATA_DIR", "data")


def _config_path(explicit_path: str | None = None) -> Path:
    if explicit_path:
        return Path(explicit_path).expanduser().resolve()
    return (Path(_data_dir()) / "hub.config.json").expanduser().resolve()


def load_config(config_path: str | None = None) -> HubConfig:
    """{MCP_HUB_DATA_DIR}/hub.config.json を読み込む。

    ファイルが存在しない場合は空の HubConfig を返す。
    ファイルの作成は store.py:JsonStore.init() が担当する。
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
    embedding_model = raw.get("embedding_model", "cl-nagoya/ruri-v3-30m")
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
        embedding_model=embedding_model,
    )



