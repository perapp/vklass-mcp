#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
config_dir=${XDG_CONFIG_HOME:-$HOME/.config}/vklass-mcp
state_dir=${XDG_DATA_HOME:-$HOME/.local/share}/vklass-mcp
quadlet_dir=${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd

install -d -m 700 "$config_dir" "$state_dir"
install -d -m 755 "$quadlet_dir"
legacy_files=(
  "$state_dir/vklass.db"
  "$state_dir/vklass.db-wal"
  "$state_dir/vklass.db-shm"
  "$state_dir/session.json.fernet"
)
for legacy_file in "${legacy_files[@]}"; do
  if [[ -e "$legacy_file" ]]; then
    echo "Legacy single-user state detected: $legacy_file" >&2
    echo "Migrate it or securely remove all listed legacy files before installing v0.2." >&2
    exit 1
  fi
done
if [[ ! -e "$config_dir/server.env" ]]; then
  install -m 600 "$project_dir/.env.example" "$config_dir/server.env"
  echo "Created $config_dir/server.env; set the public HTTPS origin before starting."
fi
install -m 644 "$project_dir/deploy/quadlet/vklass-mcp.container" \
  "$quadlet_dir/vklass-mcp.container"

if podman secret inspect vklass-mcp-state-key >/dev/null 2>&1; then
  echo "Podman secret already exists: vklass-mcp-state-key"
else
  python3 -c 'import secrets; print(secrets.token_urlsafe(48))' \
    | podman secret create vklass-mcp-state-key - >/dev/null
  echo "Created Podman secret: vklass-mcp-state-key"
fi

systemctl --user daemon-reload
echo "Installed Quadlet. Build the image, review $config_dir/server.env, configure TLS, then run:"
echo "  systemctl --user start vklass-mcp.service"
