#!/usr/bin/env bash
# Launcher for the studio GUI, so callers never need to know about the venv.
# Usage: ./content/studio.sh [--port 7431] [--verbose]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$HERE/.venv/bin/python" "$HERE/studio/server.py" "$@"
