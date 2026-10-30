#!/usr/bin/env bash
set -euo pipefail

extension_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
node_bin="${NODE_BIN:-node}"
npm_cli="${NPM_CLI:-}"
cd "$extension_dir"

if [[ -n "$npm_cli" ]]; then
  "$node_bin" "$npm_cli" ci
  "$node_bin" "$npm_cli" run build
else
  npm ci
  npm run build
fi

install_dir="${XDG_DATA_HOME:-$HOME/.local/share}/vicinae/extensions/local-mdict-dictionary"
mkdir -p "$install_dir"
cp "$extension_dir/requirements.txt" "$extension_dir/setup-helper.sh" "$extension_dir/README.md" "$extension_dir/LICENSE" "$install_dir/"
chmod +x "$install_dir/setup-helper.sh" "$install_dir/assets/backend/helper.py" "$install_dir/assets/lookup-selected-text.sh"
# Virtual environments contain absolute paths: create one at its final location.
"$install_dir/setup-helper.sh"
printf 'Installed Local MDict Dictionary at %s\n' "$install_dir"
