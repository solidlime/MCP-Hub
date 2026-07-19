"""Bootstrap persistent optional dependencies on first container startup."""
import logging
import os
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

logger = logging.getLogger(__name__)

PERSIST_DIR = "/home/mcp-hub"
EXTRAS_DIR = os.path.join(PERSIST_DIR, "pip-extras")
BIN_DIR = os.path.join(PERSIST_DIR, "bin")

NODE_VERSION = "22.23.1"
NODE_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-x64.tar.xz"
UV_VERSION = "0.11.29"
UV_URL = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz"


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
    _download_and_extract(NODE_URL, BIN_DIR, strip_components=1)
    logger.info("[bootstrap] Node.js installed.")


def _ensure_uv():
    uv_bin = os.path.join(BIN_DIR, "uv")
    if os.path.isfile(uv_bin):
        return
    logger.info("[bootstrap] Downloading uv %s...", UV_VERSION)
    _download_and_extract(UV_URL, BIN_DIR, strip_components=1, files=["uv", "uvx"])
    logger.info("[bootstrap] uv installed.")


def _ensure_fastembed():
    try:
        __import__("fastembed")
        return  # already installed
    except ImportError:
        pass
    logger.info("[bootstrap] Installing fastembed to %s...", EXTRAS_DIR)
    os.makedirs(EXTRAS_DIR, exist_ok=True)
    uv_bin = os.path.join(BIN_DIR, "uv")
    subprocess.run(
        [uv_bin, "pip", "install", "--target", EXTRAS_DIR, "fastembed"],
        check=True,
        env={**os.environ, "VIRTUAL_ENV": os.environ.get("VIRTUAL_ENV", "/opt/venv")},
    )
    logger.info("[bootstrap] fastembed installed.")


def _download_and_extract(url, dest, strip_components=0, files=None):
    """Download tarball and extract to dest. Supports .tar.xz and .tar.gz."""
    tmp = tempfile.NamedTemporaryFile(suffix=".tar", delete=False)
    try:
        urllib.request.urlretrieve(url, tmp.name)

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
        members = tf.getmembers()
        for m in members:
            if strip_components > 0:
                parts = m.name.split("/", strip_components)
                if len(parts) > strip_components:
                    m.name = parts[strip_components]
                else:
                    continue
        tf.extractall(dest, members=members, filter="data")
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
