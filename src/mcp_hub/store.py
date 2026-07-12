"""
JSON-file-based persistence for MCP server configuration.
Replaces the SQLite-backed SqliteStore with a single hub.config.json file.
"""

import asyncio
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

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
        """Ensure config file exists. If missing, create from DEFAULT_CONFIG.
        Also auto-migrate from legacy hub.db if present.
        seed_servers is accepted but ignored — JsonStore reads from file directly.
        """
        migrated = False

        # Step 1: Auto-migration from hub.db (BEFORE creating DEFAULT_CONFIG
        # to avoid overwriting defaults with old data without env/fields).
        db_path = self._path.parent / "hub.db"
        if db_path.exists() and not self._path.exists():
            try:
                await self._migrate_from_db(db_path)
                migrated = True
            except Exception:
                logger.warning("Failed to migrate from hub.db", exc_info=True)

        # Step 2: Create default config if still missing
        if not self._path.exists():
            logger.info("Config not found: %s — generating default.", self._path)
            await self._write(DEFAULT_CONFIG)

        self._data = await self._read()

        # Step 3: Augment migrated data with missing defaults (env, etc.)
        if migrated:
            augmented = False
            defaults = DEFAULT_CONFIG["mcpServers"]
            current = self._data.setdefault("mcpServers", {})
            for name, default_cfg in defaults.items():
                if name not in current:
                    current[name] = dict(default_cfg)
                    augmented = True
                elif "env" in default_cfg and "env" not in current[name]:
                    # Preserve old config but add missing env from defaults
                    current[name]["env"] = dict(default_cfg["env"])
                    augmented = True
            if augmented:
                await self._write(self._data)

        # MCP_HUB_RESEED support
        if os.environ.get("MCP_HUB_RESEED") == "1":
            logger.info("MCP_HUB_RESEED=1: wiping servers for re-seed")
            self._data["mcpServers"] = (
                dict(seed_servers) if seed_servers else DEFAULT_CONFIG["mcpServers"]
            )
            await self._write(self._data)

        count = len(self._data.get("mcpServers", {}))
        logger.info("JsonStore initialized: %d servers at %s", count, self._path)

    async def _migrate_from_db(self, db_path: Path) -> None:
        """Migrate existing hub.db data into hub.config.json, then rename db as backup."""
        import aiosqlite

        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT name, config_json, created_at FROM servers"
            )
            if not rows:
                logger.info("hub.db is empty, skipping migration")
                return

            servers = {}
            for row in rows:
                cfg = json.loads(row["config_json"])
                cfg["created_at"] = row["created_at"]
                servers[row["name"]] = cfg

            self._data.setdefault("mcpServers", {}).update(servers)

        await self._write(self._data)
        db_path.rename(db_path.with_suffix(".db.migrated"))
        logger.info(
            "Migrated %d servers from hub.db → hub.config.json", len(servers)
        )

    async def list_servers(self) -> list[dict]:
        """Return all servers in {name, config, created_at} format."""
        self._data = await self._read()
        result = []
        for name, cfg in self._data.get("mcpServers", {}).items():
            created_at = cfg.pop("created_at", datetime.now(UTC).isoformat())
            result.append({"name": name, "config": dict(cfg), "created_at": created_at})
            cfg["created_at"] = created_at
        return sorted(result, key=lambda s: s["created_at"])

    async def get_server(self, name: str) -> dict | None:
        self._data = await self._read()
        cfg = self._data.get("mcpServers", {}).get(name)
        if cfg is None:
            return None
        created_at = cfg.get("created_at", datetime.now(UTC).isoformat())
        return {"name": name, "config": dict(cfg), "created_at": created_at}

    async def _add_or_update(self, name: str, config: dict, is_new: bool = True) -> None:
        """Internal: add or update a server in JSON, then atomic write."""
        self._data = await self._read()
        servers = self._data.setdefault("mcpServers", {})
        config_to_save = dict(config)
        if is_new:
            config_to_save["created_at"] = datetime.now(UTC).isoformat()
        elif name in servers and "created_at" in servers[name]:
            config_to_save["created_at"] = servers[name]["created_at"]
        servers[name] = config_to_save
        await self._write(self._data)

    async def add_server(self, name: str, config: dict) -> None:
        await self._add_or_update(name, config, is_new=True)

    async def update_server(self, name: str, config: dict) -> bool:
        self._data = await self._read()
        if name not in self._data.get("mcpServers", {}):
            return False
        await self._add_or_update(name, config, is_new=False)
        return True

    async def remove_server(self, name: str) -> bool:
        self._data = await self._read()
        servers = self._data.get("mcpServers", {})
        if name not in servers:
            return False
        del servers[name]
        await self._write(self._data)
        return True
