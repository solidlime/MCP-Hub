"""Backward-compatibility shim. Use src.mcp_hub.store.JsonStore directly."""
from .store import JsonStore as SqliteStore  # noqa: F401

__all__ = ["SqliteStore"]
