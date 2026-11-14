"""スキーマ差分の検出と履歴（task #10）。

子サーバーのツール/リソース/プロンプトを「名前 → 指紋」に落として比較する。
本体は保持しない（比較 O(n)・履歴は有界）ための最小表現。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

# 履歴上限。UI/API は常に新しい側から読むため、古いイベントは捨ててよい。
DIFF_MAX = 200

# 差分を通知・索引再構築のトリガーにする on_change イベント名。
SCHEMA_CHANGED_EVENT = "schema_changed"


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:16]


def diff_items(old: dict[str, str], new: dict[str, str]) -> dict[str, list[str]]:
    """名前集合の added/removed と、同名で指紋が変わった changed。"""
    return {
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
        "changed": sorted(k for k in new.keys() & old.keys() if old[k] != new[k]),
    }


def has_changes(diff: dict[str, list[str]]) -> bool:
    return bool(diff["added"] or diff["removed"] or diff["changed"])


def tool_fingerprints(tools: list[Any]) -> dict[str, str]:
    """ツール名 → 説明 + input/output schema の指紋。"""
    out: dict[str, str] = {}
    for tool in tools:
        name = getattr(tool, "name", None)
        if not name:
            continue
        schema = getattr(tool, "parameters", None)
        if schema is None:
            schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
        output = getattr(tool, "output_schema", None) or getattr(tool, "outputSchema", None)
        out[str(name)] = digest([getattr(tool, "description", None), schema, output])
    return out


def item_fingerprints(items: Any, key_attr: str = "uri") -> dict[str, str]:
    """リソースの URI / プロンプト名 → 説明・引数スキーマの指紋。"""
    out: dict[str, str] = {}
    for item in items or ():
        key = getattr(item, key_attr, None) or getattr(item, "name", None)
        if not key:
            continue
        out[str(key)] = digest([
            getattr(item, "name", None),
            getattr(item, "title", None),
            getattr(item, "description", None),
            getattr(item, "mime_type", None),
            getattr(item, "arguments", None),
        ])
    return out


def summary(changes: dict) -> str:
    """1行サマリ。例: "tools +2 -1 ~3"。UI/ログ用。"""
    return ", ".join(
        f"{kind} +{len(d['added'])} -{len(d['removed'])} ~{len(d['changed'])}"
        for kind, d in changes.items()
        if d
    )


@dataclass
class SchemaDiffEntry:
    """子サーバーのスキーマ差分イベント。"""

    ts: float            # epoch 秒
    server: str
    trigger: str         # "connect" | "refresh" | "periodic"
    changes: dict        # {"tools": {"added": [], "removed": [], "changed": []}, ...}
    id: int = 0          # 単調増加シーケンス（app_state.append_diff が付与）

    def to_dict(self) -> dict:
        data = asdict(self)
        data["summary"] = summary(self.changes)
        return data