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
chown -R mcp-hub:mcp-hub "$DATA_DIR"

# Drop privileges and execute the command
exec gosu mcp-hub "$@"
