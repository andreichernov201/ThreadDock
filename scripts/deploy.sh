#!/usr/bin/env bash
# Run from the extracted release directory. No secrets are read by GitHub.
set -euo pipefail
umask 077

sha=${1:?Expected tested commit SHA}
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || exit 2
base=/opt/threaddock
release=$(pwd -P)
[[ "$release" == "$base/releases/$sha" ]] || exit 2
test -s "$base/shared/.env"
mkdir -p "$base/backups"
exec 9>"$base/deploy.lock"
flock -w 1800 9
ln -sfn "$base/shared/.env" "$release/.env"
chmod 600 "$base/shared/.env"
compose=(docker compose -p threaddock -f "$release/compose.yaml" -f "$release/compose.vps.yaml")
"${compose[@]}" config --quiet

# Build before stopping the live bot. A failed build leaves it running.
"${compose[@]}" build --pull bot
image=threaddock:local
"${compose[@]}" stop -t 35 bot

on_failure() {
  # Never blindly restore an older DB after Telegram updates may be acknowledged.
  "${compose[@]}" stop -t 35 bot || true
  printf '%s\n' 'Deployment failed. Bot stopped; data and pre-deploy backup retained.' >&2
}
trap on_failure ERR

docker volume create threaddock_threaddock-data >/dev/null
backup="$base/backups/$(date -u +%Y%m%dT%H%M%SZ)-$sha.tar.gz"
docker run --rm --user 0:0 \
  -v threaddock_threaddock-data:/source:ro \
  -v "$base/backups:/backups" \
  "$image" python scripts/backup.py /source "/backups/$(basename "$backup")"

"${compose[@]}" up -d --no-build --wait --wait-timeout 180
ln -sfn "$release" "$base/current"
trap - ERR
printf 'Healthy release: %s\nBackup: %s\n' "$sha" "$backup"
