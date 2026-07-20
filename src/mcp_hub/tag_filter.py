"""Tag-based filtering middleware for the MCP protocol path.

Intercepts tools/list, prompts/list, resources/list, and
resources/templates/list responses, filtering out components
from servers whose configured tags don't match the tags in
the X-MCP-Hub-Tags header (stored in the request_tags ContextVar).

Registered on the hub's FastMCP instance during lifespan startup.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

if TYPE_CHECKING:
    from fastmcp.prompts import Prompt
    from fastmcp.resources import Resource, ResourceTemplate
    from fastmcp.tools.base import Tool

    from .proxy_manager import ProxyManager

logger = logging.getLogger(__name__)


class TagFilterMiddleware(Middleware):
    """Filter tools/prompts/resources by server tags.

    When the X-MCP-Hub-Tags header is present in the request,
    only components from servers whose configured tags intersect
    with the requested tags (OR logic) are returned.

    Local (non-proxy) components always pass through.
    """

    def __init__(self, proxy_manager: ProxyManager) -> None:
        super().__init__()
        self._pm = proxy_manager

    # ── tools/list ──────────────────────────────────────────────────

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        tags = self._get_request_tags()
        if not tags:
            return tools
        return self._filter_items(tools, tags)

    # ── resources/list ──────────────────────────────────────────────

    async def on_list_resources(
        self,
        context: MiddlewareContext[mt.ListResourcesRequest],
        call_next: CallNext[mt.ListResourcesRequest, Sequence[Resource]],
    ) -> Sequence[Resource]:
        resources = await call_next(context)
        tags = self._get_request_tags()
        if not tags:
            return resources
        return self._filter_items(resources, tags)

    # ── resources/templates/list ────────────────────────────────────

    async def on_list_resource_templates(
        self,
        context: MiddlewareContext[mt.ListResourceTemplatesRequest],
        call_next: CallNext[
            mt.ListResourceTemplatesRequest, Sequence[ResourceTemplate]
        ],
    ) -> Sequence[ResourceTemplate]:
        templates = await call_next(context)
        tags = self._get_request_tags()
        if not tags:
            return templates
        return self._filter_items(templates, tags)

    # ── prompts/list ────────────────────────────────────────────────

    async def on_list_prompts(
        self,
        context: MiddlewareContext[mt.ListPromptsRequest],
        call_next: CallNext[mt.ListPromptsRequest, Sequence[Prompt]],
    ) -> Sequence[Prompt]:
        prompts = await call_next(context)
        tags = self._get_request_tags()
        if not tags:
            return prompts
        return self._filter_items(prompts, tags)

    # ── helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _get_request_tags() -> list[str] | None:
        """Read tags from the per-request ContextVar set by tag_middleware."""
        from .state import request_tags

        return request_tags.get()

    def _filter_items(self, items: Sequence[Any], tags: list[str]) -> list[Any]:
        """Keep items whose parent server's tags intersect with *tags*.

        Items without a ``_server`` attribute (local hub tools) always pass.
        """
        from .state import tags_match

        kept: list[Any] = []
        for item in items:
            server = getattr(item, "_server", None)
            if server is None:
                # Local tool / prompt / resource — always include
                kept.append(item)
                continue

            server_name = self._pm.proxy_to_name(id(server))
            if server_name is None:
                # Unknown server — include (safety valve)
                kept.append(item)
                continue

            server_tags = self._pm.server_tags(server_name)
            if tags_match(tags, server_tags):
                kept.append(item)
            else:
                logger.debug(
                    "TagFilter: excluded %s (server=%s, tags=%s, requested=%s)",
                    getattr(item, "name", str(item)),
                    server_name,
                    server_tags,
                    tags,
                )

        return kept
