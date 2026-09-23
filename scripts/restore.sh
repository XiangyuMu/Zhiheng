#!/usr/bin/env sh
set -eu

# Prefer the configured interpreter, while remaining usable from a checkout
# where the caller invoked the script with a system Python that does not have
# the application dependencies installed (for example, CI running the test
# suite through ``.venv/bin/pytest``).  Production deployments can set
# ZHIHENG_PYTHON explicitly.
python_bin=${ZHIHENG_PYTHON:-python}
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if ! "$python_bin" -c 'import alembic' >/dev/null 2>&1 && [ -x "$repo_dir/.venv/bin/python" ]; then
  python_bin="$repo_dir/.venv/bin/python"
fi
export ZHIHENG_PYTHON="$python_bin"
exec "$python_bin" scripts/maintenance_restore.py
