"""bootstrap の供給網保護: ハッシュ固定・許可ホスト・レジストリ固定・監査ログ。"""

import hashlib
import io
import json
import logging
import os
import subprocess
import tarfile

import pytest

from mcp_hub import bootstrap


def _make_targz(payload: bytes, name: str = "uv") -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tf:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return raw.getvalue()


def _fake_urlretrieve(blob: bytes):
    def _retrieve(url, path):
        with open(path, "wb") as f:
            f.write(blob)
        return path, None

    return _retrieve


class TestPinnedHashes:
    def test_supported_arch_urls_are_pinned(self):
        for url in (bootstrap.NODE_URL, bootstrap.UV_URL):
            assert url in bootstrap.PINNED_SHA256, url
            assert len(bootstrap.PINNED_SHA256[url]) == 64

    def test_linux_x64_and_arm64_pinned(self):
        for url, digest in bootstrap.PINNED_SHA256.items():
            assert "linux" in url
            assert len(digest) == 64


class TestDownloadVerification:
    def test_hash_mismatch_refused(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            bootstrap.urllib.request,
            "urlretrieve",
            _fake_urlretrieve(_make_targz(b"evil")),
        )
        with pytest.raises(ValueError, match="SHA256 mismatch"):
            bootstrap._download_and_extract(bootstrap.UV_URL, str(tmp_path))
        assert os.listdir(tmp_path) == []

    def test_matching_hash_extracts(self, monkeypatch, tmp_path):
        blob = _make_targz(b"#!bin\n")
        digest = hashlib.sha256(blob).hexdigest()
        url = bootstrap.UV_URL
        monkeypatch.setitem(bootstrap.PINNED_SHA256, url, digest)
        monkeypatch.setattr(
            bootstrap.urllib.request, "urlretrieve", _fake_urlretrieve(blob)
        )
        bootstrap._download_and_extract(url, str(tmp_path), files=["uv"])
        assert (tmp_path / "uv").read_bytes() == b"#!bin\n"

    def test_unpinned_url_downloads_with_warning(self, monkeypatch, tmp_path, caplog):
        blob = _make_targz(b"x")
        url = "https://github.com/some/repo/releases/download/v1/tool.tar.gz"
        monkeypatch.setattr(
            bootstrap.urllib.request, "urlretrieve", _fake_urlretrieve(blob)
        )
        with caplog.at_level(logging.WARNING, logger="mcp_hub.bootstrap"):
            bootstrap._download_and_extract(url, str(tmp_path), files=["uv"])
        assert "no pinned SHA256" in caplog.text

    def test_require_hash_refuses_unpinned(self, monkeypatch, tmp_path):
        blob = _make_targz(b"x")
        url = "https://github.com/some/repo/releases/download/v1/tool.tar.gz"
        monkeypatch.setenv("MCP_HUB_BOOTSTRAP_REQUIRE_HASH", "1")
        monkeypatch.setattr(
            bootstrap.urllib.request, "urlretrieve", _fake_urlretrieve(blob)
        )
        with pytest.raises(ValueError, match="unpinned"):
            bootstrap._download_and_extract(url, str(tmp_path), files=["uv"])

    def test_non_allowlisted_host_refused(self, monkeypatch, tmp_path):
        called = []
        monkeypatch.setattr(
            bootstrap.urllib.request,
            "urlretrieve",
            lambda u, p: called.append(u),
        )
        with pytest.raises(ValueError, match="allowlist"):
            bootstrap._download_and_extract(
                "https://evil.example/tool.tar.gz", str(tmp_path)
            )
        assert called == []

    def test_download_emits_audit_log(self, monkeypatch, tmp_path, caplog):
        blob = _make_targz(b"#!bin\n")
        monkeypatch.setattr(
            bootstrap.urllib.request, "urlretrieve", _fake_urlretrieve(blob)
        )
        url = bootstrap.UV_URL
        monkeypatch.setitem(
            bootstrap.PINNED_SHA256, url, hashlib.sha256(blob).hexdigest()
        )
        with caplog.at_level(logging.INFO, logger="mcp_hub.bootstrap"):
            bootstrap._download_and_extract(url, str(tmp_path), files=["uv"])
        assert '"event": "download"' in caplog.text
        assert url in caplog.text


class TestExtrasInstall:
    @pytest.fixture(autouse=True)
    def _no_fastembed(self, monkeypatch):
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "fastembed":
                raise ImportError("no fastembed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)

    def test_pinned_specs_and_index_passed(self, monkeypatch, tmp_path, caplog):
        calls: dict = {}

        def fake_run(argv, **kwargs):
            calls["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(bootstrap, "EXTRAS_DIR", str(tmp_path))
        with caplog.at_level(logging.INFO, logger="mcp_hub.bootstrap"):
            bootstrap._ensure_fastembed()

        argv = calls["argv"]
        for spec in bootstrap.EXTRAS_PINS:
            assert spec in argv, spec
        assert "--index-url" in argv
        assert argv[argv.index("--index-url") + 1] == bootstrap.DEFAULT_PIP_INDEX_URL
        assert "--target" in argv
        assert str(tmp_path) in argv
        assert '"event": "deps.install"' in caplog.text
        assert '"ok": true' in caplog.text

    def test_index_url_env_override(self, monkeypatch, tmp_path):
        calls: dict = {}

        def fake_run(argv, **kwargs):
            calls["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(bootstrap, "EXTRAS_DIR", str(tmp_path))
        monkeypatch.setenv("MCP_HUB_PIP_INDEX_URL", "https://mirror.internal/simple")
        bootstrap._ensure_fastembed()
        argv = calls["argv"]
        assert argv[argv.index("--index-url") + 1] == "https://mirror.internal/simple"

    def test_insecure_index_url_ignored(self, monkeypatch, tmp_path, caplog):
        calls: dict = {}

        def fake_run(argv, **kwargs):
            calls["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(bootstrap, "EXTRAS_DIR", str(tmp_path))
        monkeypatch.setenv("MCP_HUB_PIP_INDEX_URL", "http://mirror.internal/simple")
        with caplog.at_level(logging.WARNING, logger="mcp_hub.bootstrap"):
            bootstrap._ensure_fastembed()
        argv = calls["argv"]
        assert argv[argv.index("--index-url") + 1] == bootstrap.DEFAULT_PIP_INDEX_URL
        assert "insecure" in caplog.text

    def test_pins_env_override(self, monkeypatch, tmp_path):
        calls: dict = {}

        def fake_run(argv, **kwargs):
            calls["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(bootstrap, "EXTRAS_DIR", str(tmp_path))
        monkeypatch.setenv("MCP_HUB_EXTRAS_PINS", "fastembed==0.9.1, onnxruntime==1.32.0")
        bootstrap._ensure_fastembed()
        argv = calls["argv"]
        assert "fastembed==0.9.1" in argv
        assert "onnxruntime==1.32.0" in argv

    def test_failure_is_audited(self, monkeypatch, tmp_path, caplog):
        def boom(argv, **kwargs):
            raise subprocess.CalledProcessError(1, "uv")

        monkeypatch.setattr(subprocess, "run", boom)
        monkeypatch.setattr(bootstrap, "EXTRAS_DIR", str(tmp_path))
        with caplog.at_level(logging.INFO, logger="mcp_hub.bootstrap"):
            bootstrap._ensure_fastembed()  # raise しない
        assert '"event": "deps.install"' in caplog.text
        assert '"ok": false' in caplog.text


def test_audit_payload_is_json():
    record = bootstrap._audit_record("deps.install", ok=True, manager="uv")
    json.loads(record)
    assert '"event": "deps.install"' in record


class TestStartupContinuesOnVerificationFailure:
    """検証失敗時は未検証バイナリを展開せず run() は継続する。"""

    def test_node_hash_mismatch_does_not_abort_startup(
        self, monkeypatch, tmp_path, caplog
    ):
        monkeypatch.setattr(bootstrap, "BIN_DIR", str(tmp_path / "bin"))
        monkeypatch.setattr(
            bootstrap.urllib.request,
            "urlretrieve",
            _fake_urlretrieve(_make_targz(b"evil")),
        )
        with caplog.at_level(logging.INFO, logger="mcp_hub.bootstrap"):
            bootstrap._ensure_node()  # raise しない
        assert "aborted" in caplog.text
        assert '"ok": false' in caplog.text
        assert not (tmp_path / "bin" / "bin" / "node").exists()

    def test_uv_hash_mismatch_does_not_abort_startup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bootstrap, "BIN_DIR", str(tmp_path / "bin"))
        monkeypatch.setattr(
            bootstrap.urllib.request,
            "urlretrieve",
            _fake_urlretrieve(_make_targz(b"evil")),
        )
        bootstrap._ensure_uv()  # raise しない
        assert not (tmp_path / "bin" / "uv").exists()
