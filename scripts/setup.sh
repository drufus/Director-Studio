#!/usr/bin/env bash
# Install the application dependencies and build its browser interface.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"
PYTHON_EXE="${DS_PYTHON_EXE:-python3}"
REQUIREMENTS="$BACKEND_DIR/requirements.txt"

fail() {
    printf 'Setup failed: %s\n' "$*" >&2
    exit 1
}

case "${1:-}" in
    '') ;;
    --dev) REQUIREMENTS="$BACKEND_DIR/requirements-dev.txt" ;;
    --help|-h)
        printf 'Usage: %s [--dev]\nInstall app dependencies and build the UI. --dev also installs backend test dependencies.\n' "$0"
        exit 0
        ;;
    *) fail "Unknown option: $1. Use --help for usage." ;;
esac
[[ $# -le 1 ]] || fail 'Only --dev or --help is supported.'

case "$(uname -s)" in
    Darwin|Linux) ;;
    *) fail 'This script supports macOS and Linux.' ;;
esac

command -v "$PYTHON_EXE" >/dev/null 2>&1 ||
    fail 'Python was not found. Install Python 3.11+ (3.12 recommended), or set DS_PYTHON_EXE to its executable path.'
"$PYTHON_EXE" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11+ is required (3.12 recommended).")'

command -v node >/dev/null 2>&1 || fail 'Node.js was not found. Install Node.js 22.12+ and add it to PATH.'
command -v npm >/dev/null 2>&1 || fail 'npm was not found. Install Node.js with npm and add it to PATH.'
node -e 'const [major, minor] = process.versions.node.split(".").map(Number); if (major < 22 || (major === 22 && minor < 12)) { console.error("Node.js 22.12+ is required."); process.exit(1); }'

for media_tool in ffmpeg ffprobe; do
    command -v "$media_tool" >/dev/null 2>&1 ||
        fail "$media_tool was not found. Install FFmpeg with 'brew install ffmpeg' on macOS or 'sudo apt install ffmpeg' on Ubuntu, then retry."
done

if [[ ! -d "$BACKEND_DIR/.venv" ]]; then
    printf 'Creating Python environment in %s\n' "$BACKEND_DIR/.venv"
    "$PYTHON_EXE" -m venv "$BACKEND_DIR/.venv" ||
        fail 'Could not create the virtual environment. On Ubuntu, install the venv package for your Python version (for example python3-venv), then retry.'
fi

VENV_PYTHON="$BACKEND_DIR/.venv/bin/python"
[[ -x "$VENV_PYTHON" ]] || fail 'backend/.venv has no usable Unix Python executable. Recreate it on this machine with Python 3.11+.'
"$VENV_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Existing backend/.venv requires Python 3.11+. Recreate it using a supported Python version.")'

# Environments made with uv can exist without pip. Seed it locally before
# installing requirements; this does not replace an existing environment.
if ! "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
    "$VENV_PYTHON" -m ensurepip --upgrade ||
        fail 'Could not install pip in backend/.venv. Install the Python venv/ensurepip package for your system, then retry.'
fi
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -r "$REQUIREMENTS"

printf 'Installing and building the browser interface...\n'
(
    cd "$FRONTEND_DIR"
    npm ci
    npm run build
)

if [[ ! -e "$BACKEND_DIR/.env" && ! -L "$BACKEND_DIR/.env" ]]; then
    (umask 077; cp "$BACKEND_DIR/.env.example" "$BACKEND_DIR/.env")
    printf 'Created %s; configure the application there.\n' "$BACKEND_DIR/.env"
else
    printf 'Keeping existing %s\n' "$BACKEND_DIR/.env"
fi

printf '\nSetup complete. Start Director Studio with:\n  "%s/start.sh"\n' "$ROOT"
