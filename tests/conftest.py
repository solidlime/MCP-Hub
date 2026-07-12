import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture
def clean_env(monkeypatch):
    for key in ("TEST_VAR", "API_KEY", "PORT", "BRAVE_API_KEY", "FOO", "NAME", "KEY", "TOKEN", "PKG", "A", "B"):
        monkeypatch.delenv(key, raising=False)
