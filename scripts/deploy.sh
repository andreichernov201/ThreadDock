#!/usr/bin/env bash
# stdin contains a short-lived read-only GHCR token, never a persistent PAT.
set -euo pipefail
umask 077

sha=${1:?Expected tested commit SHA}
image=${2:?Expected immutable image reference}
registry_user=${3:?Expected registry username}
public_host=${4:-}
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || exit 2
[[ "$image" =~ ^ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$ ]] || exit 2
[[ "$registry_user" =~ ^[a-zA-Z0-9_-]+(\[bot\])?$ ]] || {
  echo 'Invalid GitHub registry username.' >&2
  exit 2
}
base=/opt/threaddock
release=$(pwd -P)
[[ "$release" == "$base/releases/$sha" ]] || exit 2
test -s "$base/shared/.env"
mkdir -p "$base/backups"
exec 9>"$base/deploy.lock"
flock -w 1800 9
ln -sfn "$base/shared/.env" "$release/.env"
chmod 600 "$base/shared/.env"
printf 'THREDDOCK_IMAGE=%s\n' "$image" > "$release/image.env"
unset THREDDOCK_IMAGE

# Pull and verify while the existing bot is still serving requests.
DOCKER_CONFIG=$(mktemp -d /tmp/threaddock-registry.XXXXXXXX)
export DOCKER_CONFIG
cleanup() {
  case "$DOCKER_CONFIG" in /tmp/threaddock-registry.*) rm -rf -- "$DOCKER_CONFIG";; esac
}
trap cleanup EXIT
docker login ghcr.io --username "$registry_user" --password-stdin >/dev/null
docker pull "$image"
if [[ -n "$public_host" ]]; then
  docker run --rm --user 0:0 --network none \
    -v "$base/shared:/config" "$image" python scripts/configure_web.py /config/.env "$public_host"
fi
compose=(docker compose -p threaddock --env-file "$base/shared/.env" --env-file "$release/image.env" -f "$release/compose.production.yaml")
if [[ -f "$base/shared/web.enabled" ]]; then compose+=(--profile web); fi
"${compose[@]}" config --quiet
docker run --rm --env-file "$base/shared/.env" --read-only \
  --tmpfs /tmp:size=128m,mode=1777 --cap-drop ALL --security-opt no-new-privileges:true \
  "$image" python main.py --check
if [[ -f "$base/shared/web.enabled" ]]; then "${compose[@]}" pull caddy; fi

old_release=$(readlink -f "$base/current" 2>/dev/null || true)
docker volume create threaddock_threaddock-data >/dev/null
# Initialize a new volume with the same UID as the application image.
docker run --rm --user 0:0 --network none \
  -v threaddock_threaddock-data:/app/data "$image" chown 10001:10001 /app/data

"${compose[@]}" stop -t 35 bot
stage=backup
on_failure() {
  printf 'Deployment failed during %s. Recent bot logs:\n' "$stage" >&2
  "${compose[@]}" logs --no-color --tail=30 bot >&2 || true
  "${compose[@]}" stop -t 35 bot || true
  if [[ "$stage" == backup && "$old_release" == "$base/releases/"* && -f "$old_release/image.env" ]]; then
    # No new application has run yet, so its database cannot have migrated.
    (cd "$old_release" && docker compose -p threaddock \
      --env-file "$base/shared/.env" --env-file "$old_release/image.env" \
      -f compose.production.yaml up -d --no-build --pull never bot) || true
  fi
  printf '%s\n' 'Deployment failed. Inspect logs and backups before retrying; no database was restored automatically.' >&2
}
trap on_failure ERR
backup="$base/backups/$(date -u +%Y%m%dT%H%M%SZ)-$sha.database.tar.gz"
docker run --rm --user 0:0 --network none \
  -v threaddock_threaddock-data:/source:ro -v "$base/backups:/backups" \
  "$image" python scripts/backup.py --database-only --keep 10 /source "/backups/$(basename "$backup")"

stage=starting
"${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 180
ln -sfn "$release" "$base/current"
trap - ERR

if [[ -f "$base/shared/web.enabled" && -f "$base/shared/web-origin" ]]; then
  public_url=$(cat "$base/shared/web-origin")
  if ! curl --fail --silent --show-error --retry 12 --retry-delay 3 --retry-all-errors \
    --max-time 10 "$public_url/healthz"; then
    "${compose[@]}" logs --no-color --tail=30 caddy >&2 || true
    printf '%s\n' 'The bot is healthy, but archive HTTPS is not reachable yet. Check ports 80/443 and DNS.' >&2
    exit 1
  fi
  printf '\nArchive: %s\n' "$public_url"
fi

docker image prune -af \
  --filter 'label=org.opencontainers.image.source=https://github.com/andreichernov201/ThreadDock' \
  --filter until=168h >/dev/null || true
printf 'Healthy release: %s\nImage: %s\nDatabase backup: %s\n' "$sha" "$image" "$backup"
