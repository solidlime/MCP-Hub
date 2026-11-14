"""設定ホットリロード: hub.config.json の変更を再起動なしで反映する。

呼び出し元は `POST /admin/api/reload` と、任意のファイル監視
（MCP_HUB_WATCH_CONFIG=<秒>）。適用順は サーバー差分 → ランタイム設定 →
検索インデックス再構築。設定ファイルが不正なら適用前に弾き、ランタイムは
変更前のまま残す（部分適用で壊れた状態を作らない）。
"""

from __future__ import annotations

import asyncio
import copy
import logging
from pathlib import Path
from typing import Any

from .config import load_config
from .state import app_state
from .validators import ValidationError, validate_server_config

logger = logging.getLogger(__name__)


class ReloadError(RuntimeError):
    """設定を適用できなかった（ランタイムは変更前のまま）。"""


def _validate_servers(desired: dict[str, dict]) -> None:
    """適用前にサーバー設定を検証する（壊れたエントリで部分適用しない）。

    disabled で url/command を持たないエントリは接続対象でないので通す。
    検証はコピーに対して行い、正規化した値は使わない（保存値との比較で
    毎回「変更あり」になるのを避ける。正規化は _create_proxy 側の責務）。
    """
    for name, config in desired.items():
        if not isinstance(name, str) or not name.strip():
            raise ReloadError(f"サーバー名が不正です: {name!r}")
        if config.get("disabled") and not (config.get("url") or config.get("command")):
            continue
        try:
            validate_server_config(name, copy.deepcopy(config))
        except ValidationError as e:
            raise ReloadError(f"サーバー '{name}' の設定が不正です: {e}") from e


async def reload_runtime(
    registry: Any = None,
    proxy_manager: Any = None,
    meta_app: Any = None,
    dispatcher: Any = None,
    *,
    config_path: str | None = None,
) -> dict[str, Any]:
    """設定ファイルを読み直して差分適用する。未指定の依存は app_state から拾う。"""
    registry = registry if registry is not None else app_state.registry
    proxy_manager = (
        proxy_manager if proxy_manager is not None else app_state.proxy_manager
    )
    meta_app = meta_app if meta_app is not None else getattr(app_state, "meta_app", None)
    dispatcher = (
        dispatcher
        if dispatcher is not None
        else getattr(app_state, "mcp_dispatcher", None)
    )
    if registry is None or proxy_manager is None:
        raise ReloadError("ランタイムが初期化されていません")

    # 1) パース・検証。失敗したら何も触らない。
    try:
        # ファイル不在＝「全サーバー削除」ではない（atomic replace の途中など）。
        # ここを通すと全アンマウントになるので、読む前に拒否する。
        path = Path(config_path) if config_path is not None else registry.path
        if not path.exists():
            raise ReloadError(f"設定ファイルが見つかりません: {path}")
        config = load_config(config_path)
        stored = await registry._read()
        # 差分の真実源は生の保存内容（HubConfig.servers は disabled を落とすため使えない）
        raw_servers = stored.get("mcpServers") or stored.get("servers") or {}
        if not isinstance(raw_servers, dict):
            raise ValueError(f"mcpServers must be a dict, got {type(raw_servers)}")
    except (ValueError, AttributeError, TypeError) as e:
        raise ReloadError(f"設定ファイルが不正です: {e}") from e

    desired = {name: cfg for name, cfg in raw_servers.items() if isinstance(cfg, dict)}
    _validate_servers(desired)

    # 2) サーバー差分（追加/削除/再構成）
    servers = await proxy_manager.apply_server_configs(desired)

    # 3) ランタイム設定の再適用。
    # full_info_tools / llm / tags はリクエスト毎に registry から読むので再適用不要。
    index = getattr(meta_app, "index", None)
    model_changed = False
    embeddings_toggled = False
    if index is not None:
        # use_embeddings は実効フラグ、embedding_model は索引キーごと破棄される
        embeddings_toggled = index.set_use_embeddings(config.use_embeddings)
        previous_model = index.embedding_model
        model_changed = index.set_embedding_model(config.embedding_model) != previous_model
    if config.log_level:
        logging.getLogger().setLevel(str(config.log_level).upper())
    # meta_mode は dispatcher がリクエスト毎に store から読む（キャッシュのみ落とす）
    invalidate = getattr(dispatcher, "invalidate_cache", None)
    if callable(invalidate):
        invalidate()

    # ツール結果キャッシュ: 設定を再適用し、既存エントリは意味が変わるので破棄
    try:
        app_state.tool_cache.configure(stored.get("tool_cache"))
    except ValueError:
        logger.warning("tool_cache 設定が不正のため既定値で継続: %r", stored.get("tool_cache"))
        app_state.tool_cache.configure(None)
    app_state.tool_cache.clear()

    # 索引の再構築は埋め込み設定が変わった時だけ。サーバー差分は on_change
    # （connected/updated/removed）経由の debounce 再構築が拾うので二重に走らせない。
    rebuilt = False
    if meta_app is not None and (model_changed or embeddings_toggled):
        try:
            await meta_app.rebuild_index()
            rebuilt = True
        except Exception:
            # 索引は壊れても BM25 側で検索は継続できる（degraded 状態として報告）
            logger.exception("Index rebuild after reload failed")

    return {
        "ok": True,
        "servers": servers,
        "settings": {
            "meta_mode": bool(stored.get("meta_mode", False)),
            "use_embeddings": config.use_embeddings,
            "embedding_model": getattr(index, "embedding_model", config.embedding_model),
            "embedding_model_changed": model_changed,
            "index_rebuilt": rebuilt,
        },
    }


async def watch_config(
    interval: float,
    *,
    registry: Any = None,
    config_path: str | None = None,
) -> None:
    """設定ファイルの mtime 変化を監視して reload する（任意機能）。

    MCP_HUB_WATCH_CONFIG=<秒> で有効化。管理 API 経由の保存でも mtime が動くが、
    その場合は差分ゼロで終わるので実害はない。
    """
    if interval <= 0:
        return
    registry = registry if registry is not None else app_state.registry
    path = Path(config_path) if config_path is not None else registry.path
    last: float | None = None
    while True:
        await asyncio.sleep(interval)
        try:
            mtime = path.stat().st_mtime
            if last is None:
                last = mtime
                continue
            if mtime == last:
                continue
            last = mtime
            report = await reload_runtime(config_path=config_path)
            logger.info("Config hot reload: %s", report["servers"])
        except ReloadError as e:
            logger.warning("Config hot reload skipped: %s", e)
        except asyncio.CancelledError:
            raise
        except OSError as e:
            # ファイル一時消失（atomic replace の途中等）は次周期で追いつく
            logger.warning("Config watch: cannot stat %s (%s) — retrying", path, e)
        except Exception:
            logger.exception("Config hot reload iteration failed — will retry")
