#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
target=${VKLASS_DEPLOY_TARGET:-folksaga@192.168.1.228}
image=${VKLASS_IMAGE:-localhost/vklass-mcp:latest}
unit=$project_dir/deploy/folksaga/vklass-mcp.container
remote_unit=.config/containers/systemd/vklass-mcp.container

podman image exists "$image"
ssh "$target" 'install -d -m 0755 ~/.config/containers/systemd'
ssh "$target" "temporary=\$(mktemp); cat >\"\$temporary\"; install -m 0644 \"\$temporary\" '$remote_unit'; rm -f \"\$temporary\"" <"$unit"

printf 'Transferring %s to %s...\n' "$image" "$target"
podman save "$image" | ssh "$target" \
  'CONTAINERS_STORAGE_CONF=/etc/folksaga/storage.conf podman load >/dev/null'

ssh "$target" 'bash -se' <<'REMOTE'
export CONTAINERS_STORAGE_CONF=/etc/folksaga/storage.conf
state_dir=/srv/folksaga/data/vklass-mcp
secret_dir=/srv/folksaga/secrets
key_file=$secret_dir/vklass-mcp-state-key
secret_name=vklass-mcp-state-key

install -d -m 0700 "$state_dir" "$secret_dir"
if [[ ! -e "$key_file" ]]; then
    if podman secret inspect "$secret_name" >/dev/null 2>&1; then
        echo "Podman secret exists but $key_file is missing; refusing unrecoverable key replacement" >&2
        exit 1
    fi
    umask 077
    head -c 48 /dev/urandom | base64 | tr -d '\n' >"$key_file"
    printf '\n' >>"$key_file"
fi
chmod 0600 "$key_file"
if ! podman secret inspect "$secret_name" >/dev/null 2>&1; then
    podman secret create "$secret_name" "$key_file" >/dev/null
fi

systemctl --user daemon-reload
systemctl --user restart vklass-mcp.service
systemctl --user is-active --quiet vklass-mcp.service
for _ in $(seq 1 30); do
    if podman healthcheck run vklass-mcp >/dev/null 2>&1; then
        printf 'Vklass MCP is healthy behind the Folksaga Caddy edge.\n'
        exit 0
    fi
    sleep 2
done
echo "Vklass MCP did not become healthy" >&2
podman logs --tail 50 vklass-mcp >&2 || true
exit 1
REMOTE

printf 'Verify after the Caddy route is deployed:\n  curl -fsS https://vklass.perapp.dev/healthz\n'
