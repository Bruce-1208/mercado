#!/bin/zsh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PYTHON_COMMAND="${PYTHON_COMMAND:-python3}"
cd "$SCRIPT_DIR"

if ! "$PYTHON_COMMAND" -m PyInstaller --version >/dev/null 2>&1; then
    echo "PyInstaller is not installed. Run:"
    echo "$PYTHON_COMMAND -m pip install pyinstaller"
    exit 1
fi

echo "Building MercadoLocalAgent for macOS ($(uname -m)) ..."
"$PYTHON_COMMAND" -m PyInstaller \
    --noconfirm \
    --clean \
    --distpath "$SCRIPT_DIR/dist/macos" \
    MercadoLocalAgent.spec

echo
echo "Build succeeded. The Zeshun console will include this file in macOS Agent downloads:"
echo "$SCRIPT_DIR/dist/macos/MercadoLocalAgent"
