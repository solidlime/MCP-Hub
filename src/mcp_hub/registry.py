"""
MCP サーバー登録の永続化レイヤー。
テーブル: servers (name TEXT PRIMARY KEY, config_json TEXT, created_at TEXT)
"""

import json
import logging
import os
from datetime import UTC, datetime

import aiosqlite

logger = logging.getLogger(__name__)


class SqliteStore:
    """SQLite を使った MCP サーバー登録の永続化。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.path.join(
            os.environ.get("MCP_HUB_DATA_DIR", "data"), "hub.db"
        )

    async def init(self, seed_servers: dict[str, dict] | None = None) -> None:
        """テーブル作成 + 初回起動時に config からシード。"""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS servers (
                    name TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            await db.commit()

        # MCP_HUB_RESEED=1 で全削除して再シード（移行用）
        if os.environ.get("MCP_HUB_RESEED") == "1":
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DELETE FROM servers")
                await db.commit()
            logger.info("MCP_HUB_RESEED=1: wiped existing servers for re-seed")

        # サーバーが1件もなければデフォルト一覧を登録
        existing = await self.list_servers()
        if not existing and seed_servers:
            logger.info("No servers found. Seeding %d servers from config...", len(seed_servers))
            for name, config in seed_servers.items():
                try:
                    await self.add_server(name, config)
                    logger.info("  Seeded: %s", name)
                except Exception:
                    logger.warning("  Failed to seed %s (skipping)", name, exc_info=True)
        elif existing:
            logger.info(
                "Found %d existing server(s). Skipping seed (user data preserved).",
                len(existing),
            )
        elif not seed_servers:
            logger.info("No servers in config or DB. Starting empty.")

    async def list_servers(self) -> list[dict]:
        """全サーバー取得。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall("SELECT name, config_json, created_at FROM servers ORDER BY created_at")
            return [
                {
                    "name": row["name"],
                    "config": json.loads(row["config_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    async def add_server(self, name: str, config: dict) -> None:
        """サーバー登録。同名なら上書き。"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO servers (name, config_json, created_at) VALUES (?, ?, ?)",
                (name, json.dumps(config), datetime.now(UTC).isoformat()),
            )
            await db.commit()

    async def update_server(self, name: str, config: dict) -> bool:
        """サーバー設定更新（created_at は維持）。"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE servers SET config_json = ? WHERE name = ?",
                (json.dumps(config), name),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def remove_server(self, name: str) -> bool:
        """削除。存在すれば True。"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM servers WHERE name = ?", (name,))
            await db.commit()
            return cursor.rowcount > 0

    async def get_server(self, name: str) -> dict | None:
        """単一取得。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT name, config_json, created_at FROM servers WHERE name = ?",
                (name,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return {
                "name": row["name"],
                "config": json.loads(row["config_json"]),
                "created_at": row["created_at"],
            }
