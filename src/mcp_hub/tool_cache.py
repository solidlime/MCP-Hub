"""任意機能: 冪等/参照系ツール結果の TTL キャッシュ。

キーは server + tool + 正規化引数。エラー・例外はキャッシュしない。
設定は hub.config.json の "tool_cache":
    {"enabled": false, "ttl": 60, "max_entries": 500, "max_bytes": 5000000,
     "allow": ["fetch_*"], "deny": ["browser_click"]}
allow が空なら deny 以外すべてキャッシュ（opt-in にしたい場合は allow を指定）。
パターンは fnmatch で "{server}_{tool}" または "{server}" にマッチする。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

DEFAULT_TTL = 60.0
DEFAULT_MAX_ENTRIES = 500
DEFAULT_MAX_BYTES = 5_000_000
_MAX_TTL = 86_400.0


def normalize_config(raw: dict | None) -> dict:
    """部分設定を既定値とマージして検証する。不正値は ValueError。"""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError("tool_cache はオブジェクトである必要があります")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("tool_cache.enabled は真偽値である必要があります")

    ttl = raw.get("ttl", DEFAULT_TTL)
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
        raise ValueError("tool_cache.ttl は数値（秒）である必要があります")
    if not (0 < ttl <= _MAX_TTL):
        raise ValueError(f"tool_cache.ttl は 0 より大きく {int(_MAX_TTL)} 以下である必要があります")

    max_entries = raw.get("max_entries", DEFAULT_MAX_ENTRIES)
    if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
        raise ValueError("tool_cache.max_entries は 1 以上の整数である必要があります")

    max_bytes = raw.get("max_bytes", DEFAULT_MAX_BYTES)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("tool_cache.max_bytes は 0 以上の整数である必要があります")

    def _patterns(key: str) -> list[str]:
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(
            isinstance(v, str) and v.strip() for v in value
        ):
            raise ValueError(f"tool_cache.{key} は空でない文字列のリストである必要があります")
        return [v.strip() for v in value]

    return {
        "enabled": enabled,
        "ttl": float(ttl),
        "max_entries": max_entries,
        "max_bytes": max_bytes,
        "allow": _patterns("allow"),
        "deny": _patterns("deny"),
    }


class ToolResultCache:
    """LRU + TTL の結果キャッシュ。イベントループ内で同期利用する前提。"""

    def __init__(self, config: dict | None = None, clock: Callable[[], float] | None = None):
        self._now = clock or time.monotonic
        self._entries: OrderedDict[str, tuple[float, int, Any]] = OrderedDict()
        self._bytes = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.configure(config)

    def configure(self, config: dict | None) -> None:
        cfg = normalize_config(config)
        self.enabled: bool = cfg["enabled"]
        self.ttl: float = cfg["ttl"]
        self.max_entries: int = cfg["max_entries"]
        self.max_bytes: int = cfg["max_bytes"]
        self.allow: list[str] = cfg["allow"]
        self.deny: list[str] = cfg["deny"]
        if not self.enabled:
            self.clear()

    # --- 判定 ---

    def is_cacheable(self, server: str, tool: str) -> bool:
        if not self.enabled or not server or server == "-":
            return False
        names = (f"{server}_{tool}", server)
        if any(fnmatch.fnmatch(n, p) for n in names for p in self.deny):
            return False
        if not self.allow:
            return True
        return any(fnmatch.fnmatch(n, p) for n in names for p in self.allow)

    @staticmethod
    def make_key(server: str, tool: str, arguments: dict | None) -> str:
        canonical = json.dumps(
            arguments or {}, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, default=str,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"{server}\x00{tool}\x00{digest}"

    # --- 操作 ---

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        expires_at, size, value = entry
        if expires_at <= self._now():
            self._drop(key)
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return value

    def put(self, key: str, value: Any, size: int | None = None) -> None:
        if not self.enabled:
            return
        if size is None:
            size = _approx_size(value)
        if self.max_bytes and size > self.max_bytes:
            return  # 単体で上限超過 → キャッシュしない
        self._drop(key)
        self._entries[key] = (self._now() + self.ttl, size, value)
        self._bytes += size
        while self._entries and (
            len(self._entries) > self.max_entries
            or (self.max_bytes and self._bytes > self.max_bytes)
        ):
            self._drop(next(iter(self._entries)))
            self.evictions += 1

    def invalidate_server(self, server: str) -> int:
        prefix = f"{server}\x00"
        doomed = [k for k in self._entries if k.startswith(prefix)]
        for key in doomed:
            self._drop(key)
        return len(doomed)

    def clear(self) -> int:
        count = len(self._entries)
        self._entries.clear()
        self._bytes = 0
        return count

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "entries": len(self._entries),
            "bytes": self._bytes,
        }

    def _drop(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._bytes -= entry[1]


def _approx_size(value: Any) -> int:
    """content の JSON 長でエントリサイズを見積もる（失敗時は小さめ）。"""
    try:
        content = getattr(value, "content", None)
        return len(json.dumps(content, default=str, ensure_ascii=False))
    except Exception:
        return 256
