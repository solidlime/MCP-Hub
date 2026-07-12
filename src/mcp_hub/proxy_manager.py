"""
FastMCP の create_proxy + mount を管理。
動的なサーバー追加/削除に対応。
"""

import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.client.transports.stdio import StdioTransport
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import FastMCPProxy

from .registry import SqliteStore

logger = logging.getLogger(__name__)


class ProxyManager:
    """プロキシサーバーのライフサイクル管理。

    複数の MCP サーバーへのプロキシを保持し、
    メインの FastMCP インスタンスに mount/unmount する。
    """

    def __init__(self, mcp: FastMCP, registry: SqliteStore):
        self.mcp = mcp
        self.registry = registry
        self._proxies: dict[str, FastMCPProxy] = {}
        self._server_configs: dict[str, dict] = {}
        self._status: dict[str, str] = {}

    async def load_all(self) -> None:
        """DB から全サーバーを読み込んでマウント。"""
        servers = await self.registry.list_servers()
        if not servers:
            logger.info("No servers to load from DB")
            return

        for srv in servers:
            name = srv["name"]
            config = srv["config"]
            self._server_configs[name] = config
            if config.get("disabled"):
                logger.info("Skipping disabled server %s", name)
                self._status[name] = "disabled"
                continue
            try:
                proxy = self._create_proxy(name, config)
                self._proxies[name] = proxy
                self.mcp.mount(proxy, namespace=name)
                self._status[name] = "connected"
                logger.info("Loaded server %s", name)
            except Exception:
                logger.exception("Failed to load server %s", name)
                self._status[name] = "error"

    async def register_server(self, name: str, config: dict) -> list[str]:
        """サーバー登録 + DB保存 + マウント。

        Returns:
            プロキシ経由で利用可能なツール名のリスト。
        """
        # DB 保存
        await self.registry.add_server(name, config)
        self._server_configs[name] = config

        if config.get("disabled"):
            logger.info("Server %s registered as disabled", name)
            self._status[name] = "disabled"
            return []

        # プロキシ生成
        proxy = self._create_proxy(name, config)
        self._proxies[name] = proxy

        # マウント (namespace = server_name)
        self.mcp.mount(proxy, namespace=name)
        self._status[name] = "connected"

        # ツール一覧を取得
        try:
            tools = await proxy.list_tools()
            return [t.name for t in tools]
        except Exception:
            logger.warning("Could not list tools for %s (server may not be reachable)", name)
            self._status[name] = "error"
            return []

    async def unregister_server(self, name: str) -> bool:
        """サーバー削除 + アンマウント。"""
        existed = await self.registry.remove_server(name)
        if not existed:
            return False

        self._proxies.pop(name, None)
        self._server_configs.pop(name, None)
        self._status.pop(name, None)

        # 再マウント: providers から全 proxy を除去して再追加
        self._rebuild_mounts()

        return True

    async def refresh_server(self, name: str, config: dict) -> None:
        """プロキシの再生成 + 設定更新。disable 時はアンマウントのみ。"""
        self._server_configs[name] = config
        self._status[name] = "disabled" if config.get("disabled") else "connected"

        # 古い proxy をアンマウント
        old_proxy = self._proxies.pop(name, None)
        if old_proxy:
            self._rebuild_mounts()

        # disabled なら再生成しない
        if config.get("disabled"):
            logger.info("Server %s is disabled, not mounting", name)
            return

        try:
            proxy = self._create_proxy(name, config)
            self._proxies[name] = proxy
            self.mcp.mount(proxy, namespace=name)
            self._status[name] = "connected"
            logger.info("Refreshed server %s", name)
        except Exception:
            logger.exception("Failed to refresh server %s", name)
            self._status[name] = "error"

    def get_all_status(self) -> dict[str, str]:
        """全サーバーのステータス一覧。"""
        return dict(self._status)

    async def list_tools(self, tags: list[str] | None = None) -> dict[str, list[dict]]:
        """全サーバーのツール一覧。オプションの tags フィルター。"""
        from .main import request_tags  # late import to avoid circular dep

        if tags is None:
            tags = request_tags.get(None)

        logger.debug("list_tools called with tags=%s", tags)

        result: dict[str, list[dict]] = {}
        for srv_name, proxy in self._proxies.items():
            # Tag filter (OR logic)
            if tags:
                config = self._server_configs.get(srv_name, {})
                server_tags = config.get("tags", [])
                if not any(t in server_tags for t in tags):
                    continue

            try:
                tools = await proxy.list_tools()
                result[srv_name] = [{"name": t.name, "description": t.description or ""} for t in tools]
            except Exception:
                logger.warning("Failed to list tools for %s", srv_name)
                result[srv_name] = []

        return result

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> Any:
        """ツール実行。"""
        proxy = self._proxies.get(server_name)
        if not proxy:
            raise ValueError(f"Server {server_name!r} not found")
        result = await proxy.call_tool(tool_name, arguments)
        return result

    def get_proxy(self, name: str) -> FastMCPProxy | None:
        """プロキシインスタンスを取得。"""
        return self._proxies.get(name)

    def _create_proxy(self, name: str, config: dict) -> FastMCPProxy:
        """config から FastMCPProxy を生成。"""
        url = config.get("url")
        command = config.get("command")
        if url:
            proxy = create_proxy(url, name=name)
        elif command:
            args = config.get("args", [])
            transport = StdioTransport(command=command, args=args)
            proxy = create_proxy(transport, name=name)
        else:
            raise ValueError(f"Invalid config for {name}: need 'url' or 'command'")
        return proxy

    def _rebuild_mounts(self) -> None:
        """全プロキシを再マウント（追加/削除後の整合性確保）。"""
        # local_provider のみ残す
        self.mcp.providers = [self.mcp.local_provider]

        # 全 proxy を再マウント
        for srv_name, proxy in self._proxies.items():
            self.mcp.mount(proxy, namespace=srv_name)
