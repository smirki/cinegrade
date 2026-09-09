#!/usr/bin/env bash
# Feasibility spike setup: SAM 3.1 via mlx-cv.
# sam/ is a uv PROJECT (sam/pyproject.toml + sam/uv.lock), not an ad hoc venv:
# `uv sync` creates sam/.venv from the lockfile. Run scripts with
# `uv run --project sam ...` (or `cd sam && uv run ...`), never bare python
# and never pip.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --extra mlx, or the verification below fails on a fresh machine: mlx and
# torch are OPTIONAL extras, so a plain `uv sync` installs neither and the
# `import mlx_cv` exits non zero under `set -e`. It only ever looked like it
# worked because sam/.venv already had mlx in it (round 1 finding 35).
# --inexact so a lane using the torch extra in this shared venv does not have
# it uninstalled underneath, which is the same reason the README says it.
uv lock --project "$HERE"
uv sync --project "$HERE" --inexact --extra mlx

echo "--- resolved (sam/uv.lock) ---"
uv run --project "$HERE" python -c "import mlx_cv, mlx; print('mlx_cv', mlx_cv.__version__); import mlx.core as mx; print('mlx device', mx.default_device())"

# The CPU floor (acceptance A2) is a second extra, on purpose: torch is a big
# download and the service runs without it. Uncomment, or run it by hand, on a
# machine that needs the torch backend:
#   uv sync --project "$HERE" --inexact --extra torch
