#!/usr/bin/env bash
set -euo pipefail

extension_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="${PYTHON_BIN:-python3}"

"$python_bin" -m venv "$extension_dir/.venv"
"$extension_dir/.venv/bin/python" -m pip install --disable-pip-version-check -r "$extension_dir/requirements.txt"
printf 'Local MDict helper is ready: %s\n' "$extension_dir/.venv/bin/python"
