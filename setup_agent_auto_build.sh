#!/bin/zsh
set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.tools/windows-agent/watcher-venv"
if [[ "${1:-}" == "--uninstall" ]]; then
    exec "${PYTHON_COMMAND:-python3}" "$SCRIPT_DIR/scripts/watch_windows_agent.py" --uninstall
fi
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    "${PYTHON_COMMAND:-python3}" -m venv "$VENV_DIR"
fi
if ! "$VENV_DIR/bin/python" -c 'from watchdog.observers.fsevents import FSEventsObserver' 2>/dev/null; then
    "$VENV_DIR/bin/python" -m pip install 'watchdog>=6,<7'
fi
exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/scripts/watch_windows_agent.py" --install
