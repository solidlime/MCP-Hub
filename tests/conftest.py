import json
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Disable fastembed in tests: each TextEmbedding load is ~670MB and tests don't
# need semantic search — prevents memory buildup → swap → hang (see run-tests.sh)
os.environ.setdefault("MCP_HUB_EMBEDDING", "0")

import mcp_hub.store as _mcp_store  # noqa: E402


@pytest.fixture(autouse=True)
def _bundled_config(monkeypatch, tmp_path_factory):
    """bundled default を deterministic なサーバー0件の設定に差し替える。

    実運用では JsonStore.init() が cwd の hub.config.json（npx サーバー5件入り）
    をシードするため、CI (node 無し環境) で接続試行 → spawn_failed イベントが
    ログバッファに混入し、test_log_api が環境依存で落ちていた。
    サーバーを除いた bundled default に差し替えることで、シード機構と
    meta_mode/embedding_model のデフォルト供給（test_get_settings_default の前提）
    はそのまま検証対象に保ちつつ、テストを hermetic にする。
    """
    cfg = tmp_path_factory.getbasetemp() / "bundled-default" / "hub.config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    if not cfg.exists():
        cfg.write_text(
            json.dumps(
                {"version": 1, "log_level": "info", "meta_mode": True, "mcpServers": {}}
            ),
            encoding="utf-8",
        )

    def _fake_bundled(self):
        return cfg

    monkeypatch.setattr(_mcp_store.JsonStore, "_find_bundled_default", _fake_bundled)
    try:
        # test_auth / test_integration_real は src.mcp_hub.* 経由で別モジュール
        # インスタンスをロードするため、そちらにも同様に適用する。
        # NOTE: tests/__init__.py が pytest にリポジトリルートを sys.path に
        # 入れさせる前提（削除するとこのパッチは無効化され、CI フレーキーが再発）
        import src.mcp_hub.store as _src_store  # noqa: F401
        monkeypatch.setattr(_src_store.JsonStore, "_find_bundled_default", _fake_bundled)
    except ImportError:
        pass


@pytest.fixture
def clean_env(monkeypatch):
    for key in ("TEST_VAR", "API_KEY", "PORT", "BRAVE_API_KEY", "FOO", "NAME", "KEY", "TOKEN", "PKG", "A", "B"):
        monkeypatch.delenv(key, raising=False)
