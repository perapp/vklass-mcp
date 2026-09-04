#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
target=${VKLASS_DEPLOY_TARGET:-folksaga@192.168.1.228}
image=${VKLASS_IMAGE:-localhost/vklass-mcp:latest}
unit=$project_dir/deploy/perd25/vklass-mcp.container
remote_stage=.config/containers/systemd/vklass-mcp.container.new

podman image exists "$image"
ssh "$target" 'install -d -m 0755 ~/.config/containers/systemd'
ssh "$target" "temporary=\$(mktemp); cat >\"\$temporary\"; install -m 0644 \"\$temporary\" '$remote_stage'; rm -f \"\$temporary\"" <"$unit"

printf 'Transferring %s to %s default rootless store...\n' "$image" "$target"
podman save "$image" | ssh "$target" 'podman load >/dev/null'

ssh "$target" 'bash -se' <<'REMOTE'
set -euo pipefail
state_dir=$HOME/.local/share/vklass-mcp
key_dir=$HOME/.config/vklass-mcp
key_file=$key_dir/state-key
secret_name=vklass-mcp-state-key
unit=$HOME/.config/containers/systemd/vklass-mcp.container
staged_unit=${unit}.new
legacy_state=/srv/folksaga/data/vklass-mcp
legacy_key=/srv/folksaga/secrets/vklass-mcp-state-key
marker=$state_dir/.migrated-from-folksaga
backup_unit=$(mktemp)
had_unit=0
migration_started=0
migration_succeeded=0

if [[ -f "$unit" ]]; then cp -a "$unit" "$backup_unit"; had_unit=1; fi
rollback() {
    local status=$?
    if (( migration_started && ! migration_succeeded )); then
        echo 'Vklass migration failed; restoring the previous Quadlet' >&2
        systemctl --user stop vklass-mcp.service 2>/dev/null || true
        if (( had_unit )); then install -m 0644 "$backup_unit" "$unit"; else rm -f "$unit"; fi
        systemctl --user daemon-reload
        if (( had_unit )); then systemctl --user start vklass-mcp.service || true; fi
    fi
    rm -f "$backup_unit" "$staged_unit"
    exit "$status"
}
trap rollback EXIT

# Stop while copying SQLite, including its WAL and shared-memory files. The old
# data and key remain untouched as a manual rollback copy.
migration_started=1
systemctl --user stop vklass-mcp.service
install -d -m 0700 "$HOME/.local/share" "$key_dir"
if [[ -d "$legacy_state" && ! -e "$marker" ]]; then
    if [[ -d "$state_dir" && -n $(find "$state_dir" -mindepth 1 -maxdepth 1 -print -quit) ]]; then
        echo "$state_dir already contains data without a completed migration marker" >&2
        exit 1
    fi
    install -d -m 0700 "$state_dir"
    cp -a "$legacy_state/." "$state_dir/"
    touch "$marker"
else
    install -d -m 0700 "$state_dir"
fi

if [[ ! -f "$key_file" ]]; then
    if [[ -f "$legacy_key" ]]; then
        install -m 0600 "$legacy_key" "$key_file"
        cmp -s "$legacy_key" "$key_file"
    elif podman secret inspect "$secret_name" >/dev/null 2>&1; then
        echo "Podman secret exists but $key_file is missing; refusing unrecoverable key replacement" >&2
        exit 1
    elif [[ -n $(find "$state_dir" -mindepth 1 ! -name .migrated-from-folksaga -print -quit) ]]; then
        echo "Application state exists but no state key is available" >&2
        exit 1
    else
        umask 077
        head -c 48 /dev/urandom | base64 | tr -d '\n' >"$key_file"
        printf '\n' >>"$key_file"
    fi
fi
chmod 0600 "$key_file"
if ! podman secret inspect "$secret_name" >/dev/null 2>&1; then
    podman secret create "$secret_name" "$key_file" >/dev/null
fi

install -m 0644 "$staged_unit" "$unit"
systemctl --user daemon-reload
systemctl --user start vklass-mcp.service
systemctl --user is-active --quiet vklass-mcp.service
for _ in $(seq 1 30); do
    if podman healthcheck run vklass-mcp >/dev/null 2>&1 \
            && curl -fsS http://127.0.0.1:8787/healthz >/dev/null; then
        migration_succeeded=1
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

curl -fsS https://vklass.perapp.dev/healthz >/dev/null
printf 'Verified https://vklass.perapp.dev/healthz\n'
