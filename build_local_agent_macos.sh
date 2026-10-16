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

# Publish metadata only after the executable has finished building.
"$PYTHON_COMMAND" - <<'PY'
import hashlib, io, json, platform, re, zipfile
from pathlib import Path
from bit.local_agent_distribution import _build_macos_archive
binary = Path("dist/macos/MercadoLocalAgent")
version = re.search(r'^AGENT_VERSION = "([0-9.]+)"', Path("local_agent.py").read_text(), re.M).group(1)
buffer = io.BytesIO()
with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    _build_macos_archive(archive, Path.cwd(), binary)
    archive.writestr("README-update.txt", f"泽顺 Agent {version} macOS 更新包\n"
                     "首次升级请退出旧 Agent，将本包覆盖到原目录，保留 local-agent.json。\n"
                     "1.3.0 及以后版本可在状态窗口点击“确定更新”，自动完成后续升级。\n")
package = Path("dist/Zeshun-MercadoLocalAgent-macos-update.zip")
package.with_suffix(".tmp").write_bytes(buffer.getvalue())
package.with_suffix(".tmp").replace(package)
metadata = {"agent_version": version, "architecture": platform.machine(),
            "exe_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "zip_sha256": hashlib.sha256(buffer.getvalue()).hexdigest()}
temporary = Path("dist/macos-agent-build.json.tmp")
temporary.write_text(json.dumps(metadata, indent=2))
temporary.replace("dist/macos-agent-build.json")
PY

echo
echo "Build succeeded. The Zeshun console will include this file in macOS Agent downloads:"
echo "$SCRIPT_DIR/dist/macos/MercadoLocalAgent"
