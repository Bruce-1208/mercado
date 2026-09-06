#!/usr/bin/env bash
set -euo pipefail

package_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(dirname -- "$package_root")"
python_executable="$package_root/.venv/bin/python"

if [[ ! -x "$python_executable" ]]; then
    echo "Python environment is missing. Run ./yandex/run.sh once to install dependencies." >&2
    exit 1
fi

cd "$project_root"
exec "$python_executable" -m yandex
