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

    async def load_all(self) -> None:
        """DB から全サーバーを読み込んでマウント。"""
        servers = await self.registry.list_servers()
        if not servers:
            logger.info("No servers to load from DB")
            return

        for srv in servers:
            name = srv["name"]
            config = srv["config"]
            try:
                proxy = self._create_proxy(name, config)
                self._proxies[name] = proxy
                self.mcp.mount(proxy, namespace=name)
                logger.info("Loaded server %s", name)
            except Exception:
                logger.exception("Failed to load server %s", name)

    async def register_server(self, name: str, config: dict) -> list[str]:
        """サーバー登録 + DB保存 + マウント。

        Returns:
            プロキシ経由で利用可能なツール名のリスト。
        """
        # DB 保存
        await self.registry.add_server(name, config)

        # プロキシ生成
        proxy = self._create_proxy(name, config)
        self._proxies[name] = proxy

        # マウント (namespace = server_name)
        self.mcp.mount(proxy, namespace=name)

        # ツール一覧を取得
        try:
            tools = await proxy.list_tools()
            return [t.name for t in tools]
        except Exception:
            logger.warning("Could not list tools for %s (server may not be reachable)", name)
            return []

    async def unregister_server(self, name: str) -> bool:
        """サーバー削除 + アンマウント。"""
        existed = await self.registry.remove_server(name)
        if not existed:
            return False

        self._proxies.pop(name, None)

        # 再マウント: providers から全 proxy を除去して再追加
        self._rebuild_mounts()

        return True

    async def list_tools(self, name: str | None = None) -> dict[str, list[dict]]:
        """全サーバー or 特定サーバーのツール一覧。"""
        result: dict[str, list[dict]] = {}

        if name:
            proxy = self._proxies.get(name)
            if not proxy:
                return {}
            tools = await proxy.list_tools()
            result[name] = [{"name": t.name, "description": t.description or ""} for t in tools]
        else:
            for srv_name, proxy in self._proxies.items():
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
