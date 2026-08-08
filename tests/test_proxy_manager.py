"""_create_proxy URL mode: env → Authorization: Bearer header derivation tests."""
from unittest.mock import patch

from mcp_hub.proxy_manager import ProxyManager


class _MockProxy:
    def __init__(self, name="mock"):
        self.name = name


def _make_manager():
    mcp = type("MCP", (), {"mount": lambda self, p, namespace=None: None})()
    return ProxyManager(mcp, {})


class TestCreateProxyUrlEnv:
    def test_unique_token_env_derives_bearer_header(self):
        pm = _make_manager()
        with patch("mcp_hub.proxy_manager.create_proxy", return_value=_MockProxy("m")), \
             patch("mcp_hub.proxy_manager.Client") as client_cls:
            proxy = pm._create_proxy("map", {
                "url": "https://example.com/mcp",
                "env": {"MAPBOX_ACCESS_TOKEN": "abc"},
            })
        client_cls.assert_called_once()
        transport = client_cls.call_args.kwargs["transport"]
        assert transport.headers["Authorization"] == "Bearer abc"
        assert proxy.name == "m"

    def test_explicit_headers_take_precedence(self):
        pm = _make_manager()
        with patch("mcp_hub.proxy_manager.create_proxy", return_value=_MockProxy("m")), \
             patch("mcp_hub.proxy_manager.Client") as client_cls:
            pm._create_proxy("map", {
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer explicit"},
                "env": {"MAPBOX_ACCESS_TOKEN": "abc"},
            })
        transport = client_cls.call_args.kwargs["transport"]
        assert transport.headers["Authorization"] == "Bearer explicit"

    def test_ambiguous_env_falls_back_to_url_proxy(self):
        pm = _make_manager()
        with patch("mcp_hub.proxy_manager.create_proxy", return_value=_MockProxy("m")) as cp, \
             patch("mcp_hub.proxy_manager.Client") as client_cls:
            pm._create_proxy("map", {
                "url": "https://example.com/mcp",
                "env": {"TOKEN": "t", "SECRET": "s"},
            })
        cp.assert_called_once_with("https://example.com/mcp", name="map")
        client_cls.assert_not_called()
