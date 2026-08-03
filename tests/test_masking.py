"""Masking utility tests."""
import pytest

from mcp_hub.masking import mask_args, mask_text


class TestMaskArgs:
    def test_masks_nested_api_key(self):
        out = mask_args({"headers": {"Authorization": "Bearer sk-abcdef123456", "X": "1"}})
        assert "sk-abcdef123456" not in out
        assert "Bearer ***" in out

    def test_masks_token_by_key_name(self):
        out = mask_args({"api_key": "secret-123", "q": "hello"})
        assert "secret-123" not in out
        assert '"q": "hello"' in out

    def test_masks_private_key_block(self):
        out = mask_args({"cert": "-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----"})
        assert "AAAA" not in out
        assert "PRIVATE KEY" not in out

    def test_list_recursion(self):
        out = mask_args([{"password": "p@ss"}, "plain"])
        assert "p@ss" not in out
        assert "plain" in out

    def test_plain_values_unchanged(self):
        out = mask_args({"url": "https://example.com", "method": "GET"})
        assert "https://example.com" in out

    def test_truncates_to_500(self):
        out = mask_args({"big": "x" * 2000})
        assert len(out) <= 500


class TestMaskText:
    def test_masks_bearer_token(self):
        assert mask_text("Authorization: Bearer abcdef123456", 500) == "Authorization: Bearer ***"

    def test_masks_sk_token(self):
        assert mask_text("key=sk-abcdef123456 end", 500) == "key=sk-*** end"

    def test_masks_private_key_in_text(self):
        out = mask_text("-----BEGIN PRIVATE KEY-----\nSECRETDATA\n-----END PRIVATE KEY-----", 500)
        assert "SECRETDATA" not in out

    def test_truncation_happens_after_mask(self):
        # マスク後トランケーション: sk-token が切られる前にマスクされる
        text = "key=" + "sk-" + "a" * 300
        out = mask_text(text, 500)
        assert "sk-" + "a" * 300 not in out
        assert len(out) <= 500
