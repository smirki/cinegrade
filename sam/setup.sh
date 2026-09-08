#!/usr/bin/env bash
# Feasibility spike setup: SAM 3.1 via mlx-cv.
# sam/ is a uv PROJECT (sam/pyproject.toml + sam/uv.lock), not an ad hoc venv:
# `uv sync` creates sam/.venv from the lockfile. Run scripts with
# `uv run --project sam ...` (or `cd sam && uv run ...`), never bare python
# and never pip.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

uv lock --project "$HERE"
uv sync --project "$HERE"

echo "--- resolved (sam/uv.lock) ---"
uv run --project "$HERE" python -c "import mlx_cv, mlx; print('mlx_cv', mlx_cv.__version__); import mlx.core as mx; print('mlx device', mx.default_device())"
