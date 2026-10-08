#!/bin/zsh
set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec "${PYTHON_COMMAND:-python3}" "$SCRIPT_DIR/scripts/build_windows_agent.py" "$@"
