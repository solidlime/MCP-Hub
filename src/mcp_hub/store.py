"""
JSON-file-based persistence for MCP server configuration.
Replaces the SQLite-backed SqliteStore with a single hub.config.json file.
"""

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class JsonStore:
    """JSON ファイルを使った MCP サーバー設定の永続化。
    
    テンプレート値 (${VAR}, ${VAR:-default}) は展開せずにそのまま保存。
    展開は proxy_manager._create_proxy() で行う。
    """

    def __init__(self, data_dir: str | None = None):
        self._path = Path(
            data_dir or os.environ.get("MCP_HUB_DATA_DIR", "data")
        ) / "hub.config.json"
        self._lock = asyncio.Lock()
        self._data: dict = {}

    async def _read(self) -> dict:
        """Read config from file (via thread to not block event loop)."""

        def _do():
            if not self._path.exists():
                return {"version": 1, "log_level": "info", "mcpServers": {}}
            return json.loads(self._path.read_text(encoding="utf-8"))

        return await asyncio.to_thread(_do)

    async def _write(self, data: dict) -> None:
        """Atomic write: temp file + os.replace, protected by asyncio.Lock."""
        async with self._lock:

            def _do():
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self._path.parent,
                    delete=False,
                    suffix=".tmp",
                )
                try:
                    json.dump(data, tmp, indent=2, ensure_ascii=False)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                    tmp.close()
                    os.replace(tmp.name, self._path)
                except Exception:
                    tmp.close()
                    os.unlink(tmp.name)
                    raise

            await asyncio.to_thread(_do)
            self._data = data

    async def init(self, seed_servers: dict | None = None) -> None:
        """Ensure config file exists. Copy from bundled default if missing."""
        if not self._path.exists():
            bundled = self._find_bundled_default()
            if bundled and bundled.exists():
                logger.info("Copying default config from %s → %s", bundled, self._path)
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text(bundled.read_text(encoding="utf-8"))
            else:
                logger.info("No default config found, creating empty")
                await self._write({"version": 1, "log_level": "info", "mcpServers": {}})

        self._data = await self._read()

        # MCP_HUB_RESEED support
        if os.environ.get("MCP_HUB_RESEED") == "1":
            logger.info("MCP_HUB_RESEED=1: wiping servers for re-seed")
            if seed_servers:
                self._data["mcpServers"] = dict(seed_servers)
            else:
                bundled = self._find_bundled_default()
                if bundled and bundled.exists():
                    bundled_data = json.loads(bundled.read_text(encoding="utf-8"))
                    self._data["mcpServers"] = bundled_data.get("mcpServers", {})
                else:
                    self._data["mcpServers"] = {}
            await self._write(self._data)

        count = len(self._data.get("mcpServers", {}))
        logger.info("JsonStore initialized: %d servers at %s", count, self._path)

    def _find_bundled_default(self) -> Path | None:
        """Locate bundled hub.config.json shipped with the package."""
        # 1. Sibling of data dir's parent — Docker: /opt/mcp-hub/data → /opt/mcp-hub/hub.config.json
        candidate = self._path.parent.parent / "hub.config.json"
        if candidate.exists():
            return candidate
        # 2. Current working directory — local dev / explicit Docker cwd
        candidate = Path.cwd() / "hub.config.json"
        if candidate.exists():
            return candidate
        return None

    async def list_servers(self) -> list[dict]:
        """Return all servers in {name, config} format."""
        self._data = await self._read()
        result = [
            {"name": name, "config": dict(cfg)}
            for name, cfg in self._data.get("mcpServers", {}).items()
        ]
        return sorted(result, key=lambda s: s["name"])

    async def get_server(self, name: str) -> dict | None:
        self._data = await self._read()
        cfg = self._data.get("mcpServers", {}).get(name)
        if cfg is None:
            return None
        return {"name": name, "config": dict(cfg)}

    async def _add_or_update(self, name: str, config: dict) -> None:
        """Internal: add or update a server in JSON, then atomic write."""
        self._data = await self._read()
        servers = self._data.setdefault("mcpServers", {})
        servers[name] = dict(config)
        await self._write(self._data)

    async def add_server(self, name: str, config: dict) -> None:
        await self._add_or_update(name, config)

    async def update_server(self, name: str, config: dict) -> bool:
        self._data = await self._read()
        if name not in self._data.get("mcpServers", {}):
            return False
        await self._add_or_update(name, config)
        return True

    async def remove_server(self, name: str) -> bool:
        self._data = await self._read()
        servers = self._data.get("mcpServers", {})
        if name not in servers:
            return False
        del servers[name]
        await self._write(self._data)
        return True
