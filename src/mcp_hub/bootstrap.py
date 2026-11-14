"""Bootstrap persistent optional dependencies on first container startup.

供給網保護: ダウンロードは https + ホスト許可リスト + SHA256 固定、pip extras は
レジストリ固定（既定 pypi.org）とバージョン範囲ピン。download/install は
監査ログ `[bootstrap][audit] {JSON}` に残る。
"""
import hashlib
import hmac
import json
import logging
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

PERSIST_DIR = "/home/mcp-hub"
EXTRAS_DIR = os.path.join(PERSIST_DIR, "pip-extras")
BIN_DIR = os.path.join(PERSIST_DIR, "bin")

NODE_VERSION = "22.23.1"
UV_VERSION = "0.11.29"

# ネイティブ配布物は x86_64/aarch64 のみ提供。未対応アーキは x64 既定にフォールバック
_MACHINE = platform.machine().lower()
_NODE_ARCH = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}
_UV_ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}
if _MACHINE not in _NODE_ARCH:
    logger.warning("[bootstrap] Unknown arch %r — defaulting to x64", _MACHINE)
NODE_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-{_NODE_ARCH.get(_MACHINE, 'x64')}.tar.xz"
UV_URL = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-{_UV_ARCH.get(_MACHINE, 'x86_64')}-unknown-linux-gnu.tar.gz"

# 取得元の許可リスト。未知アーキ等で URL が増えた場合は追記しない限り拒否する。
DOWNLOAD_ALLOWED_HOSTS = (
    "nodejs.org",
    "github.com",
    "objects.githubusercontent.com",  # github release の実体配信先
)

# SHA256 固定（公式 SHASUMS256.txt / sha256.sum 由来）。
# ピンが無い URL は警告して続行。MCP_HUB_BOOTSTRAP_REQUIRE_HASH=1 なら拒否。
PINNED_SHA256 = {
    "https://nodejs.org/dist/v22.23.1/node-v22.23.1-linux-x64.tar.xz":
        "9749e988f437343b7fa832c69ded82a312e41a03116d766797ac14f6f9eee578",
    "https://nodejs.org/dist/v22.23.1/node-v22.23.1-linux-arm64.tar.xz":
        "0294e8b915ab75f92c7513d2fcb830ae06e10684e6c603e99a87dbf8835389c1",
    "https://github.com/astral-sh/uv/releases/download/0.11.29/uv-x86_64-unknown-linux-gnu.tar.gz":
        "04f8b82f5d47f0512dcd32c67a4a6f16a0ea27c81537c338fd0ad6b23cebe829",
    "https://github.com/astral-sh/uv/releases/download/0.11.29/uv-aarch64-unknown-linux-gnu.tar.gz":
        "94500fb064ae3c971a873cba64d94694c50677e0a4dbf78735c80509e7429919",
}

# fastembed 系のバージョン範囲ピン（fastembed 0.9.x の要求と両立する範囲）。
# MCP_HUB_EXTRAS_PINS で上書き可（カンマ区切り）。
EXTRAS_PINS = (
    "fastembed>=0.9,<1",
    "onnxruntime>=1.17,<1.24",
    "tokenizers>=0.15,<1",
    "huggingface_hub>=0.20,<2",
)

DEFAULT_PIP_INDEX_URL = "https://pypi.org/simple"


def _audit_record(event: str, **fields) -> str:
    """監査ログの JSON 1 行（固定形式: ログ解析しやすさ優先）。"""
    return json.dumps({"event": event, **fields}, ensure_ascii=False, sort_keys=True)


def _audit(event: str, **fields) -> None:
    logger.info("[bootstrap][audit] %s", _audit_record(event, **fields))


def _pip_index_url() -> str:
    """レジストリ URL。https 以外は警告して既定値へフォールバック。"""
    url = os.environ.get("MCP_HUB_PIP_INDEX_URL") or DEFAULT_PIP_INDEX_URL
    if not url.startswith("https://"):
        logger.warning(
            "insecure MCP_HUB_PIP_INDEX_URL %r ignored — using %s",
            url,
            DEFAULT_PIP_INDEX_URL,
        )
        return DEFAULT_PIP_INDEX_URL
    return url


def _extras_pins() -> list[str]:
    """インストールする依存仕様（既定はバージョン範囲ピン）。"""
    raw = os.environ.get("MCP_HUB_EXTRAS_PINS", "")
    if raw.strip():
        return [p.strip() for p in raw.split(",") if p.strip()]
    return list(EXTRAS_PINS)


def _require_pinned_hash() -> bool:
    return os.environ.get("MCP_HUB_BOOTSTRAP_REQUIRE_HASH", "") == "1"


def setup_path():
    """Add persistent extras to sys.path so installed packages are importable."""
    if os.path.isdir(EXTRAS_DIR) and EXTRAS_DIR not in sys.path:
        sys.path.insert(0, EXTRAS_DIR)


def setup_env():
    """Add persistent bin dirs to PATH for subprocesses (npx, uvx, etc.)."""
    paths = [BIN_DIR]
    node_bin = os.path.join(BIN_DIR, "bin")
    if os.path.isdir(node_bin):
        paths.append(node_bin)
    # Add pip extras bin for console scripts (yt-dlp, etc.)
    extras_bin = os.path.join(EXTRAS_DIR, "bin")
    if os.path.isdir(extras_bin):
        paths.append(extras_bin)
    for p in paths:
        if p not in os.environ.get("PATH", ""):
            os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")
    # Add pip extras to PYTHONPATH for subprocesses (importable packages)
    if os.path.isdir(EXTRAS_DIR):
        py_path = os.environ.get("PYTHONPATH", "")
        if EXTRAS_DIR not in py_path:
            os.environ["PYTHONPATH"] = EXTRAS_DIR + os.pathsep + py_path if py_path else EXTRAS_DIR


def run():
    """Ensure optional dependencies are installed. Idempotent — skips if already done."""
    setup_path()
    setup_env()
    _ensure_bin_dir()
    _ensure_node()
    _ensure_uv()
    _ensure_fastembed()


def _ensure_bin_dir():
    os.makedirs(BIN_DIR, exist_ok=True)


def _ensure_node():
    node_bin = os.path.join(BIN_DIR, "bin", "node")
    if os.path.isfile(node_bin):
        return
    logger.info("[bootstrap] Downloading Node.js %s...", NODE_VERSION)
    try:
        _download_and_extract(NODE_URL, BIN_DIR, strip_components=1)
    except (ValueError, OSError, tarfile.TarError) as e:
        # 検証失敗時は未検証バイナリを展開せず、起動は継続する（node 依存サーバーのみ影響）
        _audit("download", ok=False, url=NODE_URL, error=str(e))
        logger.error("[bootstrap] Node.js install aborted: %s", e)
        return
    logger.info("[bootstrap] Node.js installed.")


def _ensure_uv():
    uv_bin = os.path.join(BIN_DIR, "uv")
    if os.path.isfile(uv_bin):
        return
    logger.info("[bootstrap] Downloading uv %s...", UV_VERSION)
    try:
        _download_and_extract(UV_URL, BIN_DIR, strip_components=1, files=["uv", "uvx"])
    except (ValueError, OSError, tarfile.TarError) as e:
        # 検証失敗時は未検証バイナリを展開せず、起動は継続する
        _audit("download", ok=False, url=UV_URL, error=str(e))
        logger.error("[bootstrap] uv install aborted: %s", e)
        return
    logger.info("[bootstrap] uv installed.")


def _ensure_fastembed():
    try:
        __import__("fastembed")
        return  # already installed
    except ImportError:
        pass
    index_url = _pip_index_url()
    specs = _extras_pins()
    logger.info("[bootstrap] Installing fastembed to %s...", EXTRAS_DIR)
    _audit("deps.install", phase="start", target=EXTRAS_DIR, index_url=index_url, specs=specs)
    os.makedirs(EXTRAS_DIR, exist_ok=True)
    uv_bin = os.path.join(BIN_DIR, "uv")
    try:
        subprocess.run(
            [
                uv_bin,
                "pip",
                "install",
                "--target",
                EXTRAS_DIR,
                "--index-url",
                index_url,
                *specs,
            ],
            check=True,
            env={**os.environ, "VIRTUAL_ENV": os.environ.get("VIRTUAL_ENV", "/opt/venv")},
        )
    except (subprocess.CalledProcessError, OSError) as e:
        # 起動は継続し、呼び出し側は BM25 フォールバックで動作する
        _audit("deps.install", phase="done", ok=False, target=EXTRAS_DIR, error=str(e))
        logger.warning(
            "[bootstrap] fastembed install failed (%s) — continuing with BM25 fallback",
            e,
        )
        return
    _audit("deps.install", phase="done", ok=True, target=EXTRAS_DIR, index_url=index_url)
    logger.info("[bootstrap] fastembed installed.")


def _check_download_source(url: str) -> None:
    """https とホスト許可リストを検証する（抽出前のゲート）。"""
    if not url.startswith("https://"):
        raise ValueError(f"Refusing non-https download: {url}")
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host not in DOWNLOAD_ALLOWED_HOSTS:
        raise ValueError(
            f"Refusing download from host outside allowlist: {host!r} "
            f"(allowed: {list(DOWNLOAD_ALLOWED_HOSTS)})"
        )


def _verify_download(url: str, path: str) -> str:
    """SHA256 を検証して digest を返す。不一致は例外（抽出しない）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    expected = PINNED_SHA256.get(url)
    if expected is None:
        if _require_pinned_hash():
            raise ValueError(
                f"Refusing unpinned download (MCP_HUB_BOOTSTRAP_REQUIRE_HASH=1): {url}"
            )
        logger.warning("no pinned SHA256 for %s (unverified download)", url)
        return actual
    if not hmac.compare_digest(expected, actual):
        raise ValueError(f"SHA256 mismatch for {url}: expected {expected}, got {actual}")
    return actual


def _download_and_extract(url, dest, strip_components=0, files=None):
    """Download tarball and extract to dest. Supports .tar.xz and .tar.gz.

    https + ホスト許可リスト + SHA256 ピン一致を通過したものだけを展開する。
    """
    _check_download_source(url)
    tmp = tempfile.NamedTemporaryFile(suffix=".tar", delete=False)
    try:
        urllib.request.urlretrieve(url, tmp.name)
        try:
            digest = _verify_download(url, tmp.name)
        except ValueError as e:
            _audit("download", ok=False, url=url, error=str(e))
            raise
        _audit("download", ok=True, url=url, sha256=digest)

        # .tar.xz needs lzma open; .tar.gz works directly with tarfile
        if url.endswith(".tar.xz"):
            import lzma
            with lzma.open(tmp.name) as f:
                with tarfile.open(fileobj=f) as tf:
                    _extract(tf, dest, strip_components, files)
        else:
            with tarfile.open(tmp.name) as tf:
                _extract(tf, dest, strip_components, files)
    finally:
        os.unlink(tmp.name)


def _extract(tf, dest, strip_components, files):
    """Extract members from an open tarfile, optionally filtering and stripping."""
    if files is None:
        filtered = []
        for m in tf.getmembers():
            if strip_components > 0:
                parts = m.name.split("/", strip_components)
                if len(parts) > strip_components:
                    m.name = parts[strip_components]
                else:
                    continue
            filtered.append(m)
        tf.extractall(dest, members=filtered, filter="data")
    else:
        members = []
        for m in tf.getmembers():
            base = os.path.basename(m.name)
            if base in files:
                if strip_components > 0:
                    m.name = base
                members.append(m)
        tf.extractall(dest, members=members, filter="data")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run()
