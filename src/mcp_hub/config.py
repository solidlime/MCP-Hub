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

DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


@dataclass
class HubConfig:
    servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    version: int = 1
    log_level: str = "info"
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    use_embeddings: bool = True


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
    if not isinstance(log_level, str) or not log_level:
        logger.warning("Invalid log_level=%r — falling back to 'info'", log_level)
        log_level = "info"
    embedding_model = raw.get("embedding_model", DEFAULT_EMBEDDING_MODEL)
    if not isinstance(embedding_model, str) or not embedding_model:
        logger.warning(
            "Invalid embedding_model=%r — falling back to default", embedding_model
        )
        embedding_model = DEFAULT_EMBEDDING_MODEL
    use_embeddings = raw.get("use_embeddings", True)
    if not isinstance(use_embeddings, bool):
        logger.warning(
            "Invalid use_embeddings=%r — falling back to True", use_embeddings
        )
        use_embeddings = True
    raw_servers = raw.get("mcpServers", raw.get("servers", {}))

    if not isinstance(raw_servers, dict):
        raise ValueError(f"mcpServers must be a dict, got {type(raw_servers)}")

    servers: dict[str, dict] = {}
    for name, cfg in raw_servers.items():
        if not isinstance(cfg, dict):
            continue
        if not isinstance(name, str) or not name.strip():
            logger.warning("Skipping server with invalid empty name: %r", name)
            continue
        if (
            "/" in name
            or "\\" in name
            or any(ord(c) < 0x20 or ord(c) == 0x7F for c in name)
        ):
            logger.warning("Skipping server with invalid name: %r", name)
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
        use_embeddings=use_embeddings,
    )



