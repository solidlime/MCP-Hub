"""JsonStore 永続化テスト — rename_server を中心に。"""
import pytest

from mcp_hub.store import JsonStore


@pytest.fixture
def store(tmp_path):
    return JsonStore(data_dir=str(tmp_path))


@pytest.mark.asyncio
class TestRenameServer:
    async def test_rename_rekeys_and_rewrites_full_info_tools(self, store):
        await store.add_server("old", {"url": "http://x", "tags": ["a"]})
        await store.set_full_info_tools(["old_tool1", "old_tool2", "other_tool1"])

        ok = await store.rename_server("old", "new")

        assert ok is True
        assert await store.get_server("old") is None
        srv = await store.get_server("new")
        assert srv is not None
        assert srv["config"] == {"url": "http://x", "tags": ["a"]}
        # 旧プレフィックスは新名に置換され、他サーバーのエントリは不変
        data = await store._read()
        assert data["full_info_tools"] == ["new_tool1", "new_tool2", "other_tool1"]

    async def test_rename_collision_returns_false(self, store):
        await store.add_server("old", {"url": "http://x"})
        await store.add_server("new", {"url": "http://y"})

        ok = await store.rename_server("old", "new")

        assert ok is False
        # どちらも不変
        assert await store.get_server("old") is not None
        assert await store.get_server("new") is not None

    async def test_rename_missing_old_returns_false(self, store):
        ok = await store.rename_server("ghost", "new")
        assert ok is False

    async def test_rename_without_full_info_tools_key(self, store):
        await store.add_server("old", {"url": "http://x"})
        ok = await store.rename_server("old", "new")
        assert ok is True
        assert await store.get_server("new") is not None
        assert await store.get_server("old") is None
