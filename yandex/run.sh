#!/usr/bin/env bash
set -euo pipefail

package_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(dirname -- "$package_root")"
python_executable="$package_root/.venv/bin/python"
bootstrap_python="${PYTHON:-python3}"

if [[ ! -x "$python_executable" ]]; then
    "$bootstrap_python" -m venv "$package_root/.venv"
fi

"$python_executable" -m pip install -r "$package_root/requirements.txt"
"$python_executable" -m playwright install chromium

cd "$project_root"
exec "$python_executable" -m yandex
