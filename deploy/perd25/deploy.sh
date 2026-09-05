#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
target=${VKLASS_DEPLOY_TARGET:-perapp@192.168.1.228}
image=${VKLASS_IMAGE:-localhost/vklass-mcp:latest}
activate=${VKLASS_DEPLOY_ACTIVATE:-1}
unit=$project_dir/deploy/perd25/vklass-mcp.container
remote_stage=.config/containers/systemd/vklass-mcp.container.new
[[ "$activate" == 0 || "$activate" == 1 ]] || {
    echo "VKLASS_DEPLOY_ACTIVATE must be 0 or 1" >&2
    exit 2
}

podman image exists "$image"
ssh "$target" 'install -d -m 0755 ~/.config/containers/systemd'
ssh "$target" "temporary=\$(mktemp); cat >\"\$temporary\"; install -m 0644 \"\$temporary\" '$remote_stage'; rm -f \"\$temporary\"" <"$unit"

printf 'Transferring %s to %s...\n' "$image" "$target"
podman save "$image" | ssh "$target" 'podman load >/dev/null'

ssh "$target" "VKLASS_DEPLOY_ACTIVATE='$activate' bash -se" <<'REMOTE'
set -euo pipefail
activate=${VKLASS_DEPLOY_ACTIVATE:?}
state_dir=$HOME/.local/share/vklass-mcp
key_dir=$HOME/.config/vklass-mcp
key_file=$key_dir/state-key
secret_name=vklass-mcp-state-key
unit=$HOME/.config/containers/systemd/vklass-mcp.container
staged_unit=${unit}.new
backup_unit=$(mktemp)
had_unit=0
activated=0

if [[ -f "$unit" ]]; then cp -a "$unit" "$backup_unit"; had_unit=1; fi
rollback() {
    local status=$?
    if (( activated )); then
        echo 'Vklass deployment failed; restoring the previous Quadlet' >&2
        systemctl --user stop vklass-mcp.service 2>/dev/null || true
        if (( had_unit )); then install -m 0644 "$backup_unit" "$unit"; else rm -f "$unit"; fi
        systemctl --user daemon-reload
        if (( had_unit )); then systemctl --user start vklass-mcp.service || true; fi
    fi
    rm -f "$backup_unit" "$staged_unit"
    exit "$status"
}
trap rollback EXIT

install -d -m 0700 "$HOME/.local/share" "$state_dir" "$key_dir"
if [[ ! -f "$key_file" ]]; then
    if podman secret inspect "$secret_name" >/dev/null 2>&1; then
        echo "Podman secret exists but $key_file is missing; refusing unrecoverable key replacement" >&2
        exit 1
    elif [[ -n $(find "$state_dir" -mindepth 1 -print -quit) ]]; then
        echo "Application state exists but no state key is available" >&2
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

install -m 0644 "$staged_unit" "$unit"
systemctl --user daemon-reload
if [[ "$activate" == 0 ]]; then
    trap - EXIT
    rm -f "$backup_unit" "$staged_unit"
    printf 'Vklass MCP is staged but not started.\n'
    exit 0
fi

activated=1
systemctl --user restart vklass-mcp.service
systemctl --user is-active --quiet vklass-mcp.service
for _ in $(seq 1 30); do
    if podman healthcheck run vklass-mcp >/dev/null 2>&1 \
            && curl -fsS http://127.0.0.1:8787/healthz >/dev/null; then
        trap - EXIT
        rm -f "$backup_unit" "$staged_unit"
        printf 'Vklass MCP is healthy on loopback for perapp-edge.\n'
        exit 0
    fi
    sleep 2
done
echo "Vklass MCP did not become healthy" >&2
podman logs --tail 50 vklass-mcp >&2 || true
exit 1
REMOTE

if [[ "$activate" == 1 ]]; then
    curl -fsS https://vklass.perapp.dev/healthz >/dev/null
    printf 'Verified https://vklass.perapp.dev/healthz\n'
fi
