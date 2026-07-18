#!/bin/bash
set -e

# PUID/PGID support — configurable user namespace mapping
# Prevents filesystem permission issues with mounted volumes
# and allows Chromium sandbox to work without --no-sandbox

DATA_DIR="${MCP_HUB_DATA_DIR:-/opt/mcp-hub/data}"
mkdir -p "$DATA_DIR"

# Auto-detect PUID/PGID from the data directory owner if not explicitly set
if [ -z "${PUID+x}" ] && [ -z "${PGID+x}" ]; then
    PUID=$(stat -c '%u' "$DATA_DIR")
    PGID=$(stat -c '%g' "$DATA_DIR")
    echo "Auto-detected PUID=$PUID PGID=$PGID from $DATA_DIR"
else
    PUID=${PUID:-1000}
    PGID=${PGID:-1000}
fi

# Remap the mcp-hub user/group to match the host user
groupmod -o -g "$PGID" mcp-hub 2>/dev/null || true
usermod -o -u "$PUID" -g "$PGID" mcp-hub 2>/dev/null || true

# Ensure data directory is writable by the remapped user
# (includes npm/uv caches under $DATA_DIR/.npm and $DATA_DIR/.uv)
chown -R mcp-hub:mcp-hub "$DATA_DIR"

# Persist /home/mcp-hub across container restarts (npm cache, Chromium data, .uv fallback)
HOME_DIR="/home/mcp-hub"
mkdir -p "$HOME_DIR"
chown -R mcp-hub:mcp-hub "$HOME_DIR"

# Grant Docker socket access to mcp-hub user if socket is available
# Enables Docker-based MCP servers (e.g., llm-sandbox) without root
if [ -S /var/run/docker.sock ]; then
    DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
    groupadd -o -g "$DOCKER_GID" docker-host 2>/dev/null || true
    usermod -a -G docker-host mcp-hub 2>/dev/null || true
    echo "Docker socket detected (GID=$DOCKER_GID) — access granted to mcp-hub"
fi

# Bootstrap persistent dependencies (node, uv, fastembed)
# Runs once on first startup, subsequent startups are no-ops
gosu mcp-hub python -m mcp_hub.bootstrap

# Export the bootstrapped bin path so downstream process inherits it
export PATH="/home/mcp-hub/.mcp-hub/bin:$PATH"

# Drop privileges and execute the command
exec gosu mcp-hub "$@"
