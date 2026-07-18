"""Bootstrap persistent optional dependencies on first container startup."""
import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

PERSIST_DIR = os.path.join(os.environ.get("HOME", "/home/mcp-hub"), ".mcp-hub")
EXTRAS_DIR = os.path.join(PERSIST_DIR, "pip-extras")


def setup_path():
    """Add persistent extras to sys.path so installed packages are importable."""
    if os.path.isdir(EXTRAS_DIR) and EXTRAS_DIR not in sys.path:
        sys.path.insert(0, EXTRAS_DIR)


def run():
    """Ensure optional dependencies are installed. Idempotent — skips if already done."""
    setup_path()
    _ensure_fastembed()


def _ensure_fastembed():
    try:
        __import__("fastembed")
        return  # already installed
    except ImportError:
        pass
    logger.info("[bootstrap] Installing fastembed to %s...", EXTRAS_DIR)
    os.makedirs(EXTRAS_DIR, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--target", EXTRAS_DIR, "fastembed"],
        check=True,
    )
    logger.info("[bootstrap] fastembed installed.")
