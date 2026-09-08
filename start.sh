#!/usr/bin/env bash
# Run the API and built browser interface in one foreground process.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT/backend/.venv"

fail() {
    printf 'Cannot start Director Studio: %s\n' "$*" >&2
    printf 'Run "%s/scripts/setup.sh" first.\n' "$ROOT" >&2
    exit 1
}

[[ -x "$VENV_DIR/bin/python" ]] || fail 'the Python environment is missing.'
[[ -f "$ROOT/frontend/dist/index.html" ]] || fail 'the browser interface has not been built.'

# Use the dedicated Python environment. pydantic-settings reads .env as data;
# never execute it as shell code. Exported DS_* settings retain their priority.
export VIRTUAL_ENV="$VENV_DIR"
export PATH="$VENV_DIR/bin:$PATH"
export PYTHONUNBUFFERED=1
cd "$ROOT/backend"

printf 'Starting Director Studio. The server log shows the listening address.\n'
printf 'Press Ctrl-C to stop. Set DS_HOST and DS_PORT to change the listener.\n'
exec "$VENV_DIR/bin/python" -m app.main
