#!/bin/bash
set -e

# PUID/PGID support — configurable user namespace mapping
# Prevents filesystem permission issues with mounted volumes
# and allows Chromium sandbox to work without --no-sandbox

PUID=${PUID:-1000}
PGID=${PGID:-1000}

# Remap the mcp-hub user/group to match the host user
groupmod -o -g "$PGID" mcp-hub 2>/dev/null || true
usermod -o -u "$PUID" -g "$PGID" mcp-hub 2>/dev/null || true

# Ensure data directory is writable by the remapped user
DATA_DIR="${MCP_HUB_DATA_DIR:-/opt/mcp-hub/data}"
mkdir -p "$DATA_DIR"
chown -R mcp-hub:mcp-hub "$DATA_DIR"

# Drop privileges and execute the command
exec gosu mcp-hub "$@"
