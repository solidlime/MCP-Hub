"""FullInfoMiddleware / split_qualified_name のテスト。"""
from mcp_hub.middleware import split_qualified_name


class TestSplitQualifiedName:
    def test_longest_prefix_wins(self):
        connected = {"fetch": object(), "fetch_tools": object()}
        assert split_qualified_name("fetch_tools_get", connected) == ("fetch_tools", "get")
        assert split_qualified_name("fetch_fetch", connected) == ("fetch", "fetch")

    def test_no_match_returns_dash(self):
        connected = {"fetch": object()}
        assert split_qualified_name("other_thing", connected) == ("-", "other_thing")

    def test_server_with_underscore(self):
        connected = {"my_server": object()}
        assert split_qualified_name("my_server_do_stuff", connected) == ("my_server", "do_stuff")
