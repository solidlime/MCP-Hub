"""機密値マスキングユーティリティ。

ツールログに引数・エラーを記録する前に適用する。
順序は「マスク → トランケーション」固定（先に切ると
PRIVATE KEY ブロックが途中で切れてパターン不一致になる）。
"""

from __future__ import annotations

import json
import re
from typing import Any

# キー名部分一致で値全体をマスク
SENSITIVE_KEY_HINTS = (
    "api_key", "apikey", "token", "secret", "password", "passwd",
    "auth", "credential", "key",
)

# 値そのもののパターン
_SK_TOKEN = re.compile(r"sk-[A-Za-z0-9_\-]{4,}")
_BEARER = re.compile(r"(?i)(Bearer\s+)[^\s\"']+")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-]*?PRIVATE KEY-----.*?-----END [^-]*?PRIVATE KEY-----",
    re.DOTALL,
)

_ARG_MAX_LEN = 500
_TEXT_MAX_LEN = 500
_TRACEBACK_MAX_LEN = 4000


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in SENSITIVE_KEY_HINTS)


def _mask_scalar(value: Any) -> Any:
    """str 値に機密パターンが含まれる場合 *** に置換。"""
    if not isinstance(value, str):
        return value
    if _SK_TOKEN.search(value) or _BEARER.search(value) or _PRIVATE_KEY.search(value):
        return "***"
    return value


def _mask_sensitive_value(value: Any) -> Any:
    """敏感キーの値。機密パターン部分のみ *** に置換して残りは保持する。

    例: "Bearer sk-abc..." → "Bearer ***"（Bearer ラベルは残す）。
    パターンにマッチしない場合は値全体を *** にする。
    """
    if not isinstance(value, str):
        return "***"
    if not (_SK_TOKEN.search(value) or _BEARER.search(value) or _PRIVATE_KEY.search(value)):
        return "***"
    masked = _SK_TOKEN.sub("sk-***", value)
    masked = _BEARER.sub(r"\1***", masked)
    masked = _PRIVATE_KEY.sub("***", masked)
    return masked


def _mask_recursive(obj: Any) -> Any:
    """dict/list を再帰的に処理し、機密キー名・機密パターンをマスク。"""
    if isinstance(obj, dict):
        return {
            key: (_mask_sensitive_value(value) if _is_sensitive_key(str(key)) else _mask_recursive(value))
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_mask_recursive(item) for item in obj]
    return _mask_scalar(obj)


def mask_args(args: dict | list | Any) -> str:
    """引数を JSON 化し、マスク→500字トランケーションして返す。"""
    masked = _mask_recursive(args)
    text = json.dumps(masked, ensure_ascii=False, default=str)
    return text[:_ARG_MAX_LEN]


def mask_text(text: str, max_len: int = _TEXT_MAX_LEN) -> str:
    """自由テキスト（エラー等）をマスク→トランケーションして返す。"""
    masked = _SK_TOKEN.sub("sk-***", text)
    masked = _BEARER.sub(r"\1***", masked)
    masked = _PRIVATE_KEY.sub("***", masked)
    return masked[:max_len]
