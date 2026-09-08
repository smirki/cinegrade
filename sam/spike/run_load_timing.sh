#!/usr/bin/env bash
# Runs load_timing.py for both load paths, each as its own process (see the
# docstring in load_timing.py for why: cold vs warm needs a process boundary).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
/usr/bin/time -l ../.venv/bin/python load_timing.py processor
/usr/bin/time -l ../.venv/bin/python load_timing.py video
