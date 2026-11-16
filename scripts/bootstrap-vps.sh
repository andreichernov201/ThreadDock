#!/usr/bin/env bash
# Run once as root with an existing PUBLIC deployment key file as argument.
# Does not change SSH passwords, SSH port, firewall rules or existing users' keys.
set -euo pipefail
umask 077
[[ $(id -u) == 0 ]] || { echo 'Run as root.' >&2; exit 1; }
[[ $(uname -m) == x86_64 ]] || { echo 'This image targets x86_64.' >&2; exit 1; }
key=${1:?Path to the public Ed25519 deployment key is required}
test -f "$key"
pubkey=$(cat "$key")
[[ "$pubkey" != *$'\n'* && "$pubkey" != *$'\r'* ]]
grep -Eq '^ssh-ed25519 [A-Za-z0-9+/=]+( .*)?$' "$key"
ssh-keygen -l -f "$key" >/dev/null

if ! command -v docker >/dev/null; then
  . /etc/os-release
  [[ "$ID" == ubuntu ]] || { echo 'Expected Ubuntu.' >&2; exit 1; }
  apt-get update
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
docker compose version
systemctl enable --now docker
if ! id threaddock-deploy >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash threaddock-deploy
fi
usermod -aG docker threaddock-deploy
home_dir=$(getent passwd threaddock-deploy | cut -d: -f6)
[[ "$home_dir" == /home/threaddock-deploy ]] || exit 2
install -d -m 0700 -o threaddock-deploy -g threaddock-deploy "$home_dir/.ssh"
authorized="$home_dir/.ssh/authorized_keys"
touch "$authorized"
if ! grep -qF "$(cut -d' ' -f2 "$key")" "$authorized"; then
  printf 'restrict %s\n' "$pubkey" >> "$authorized"
fi
chmod 0600 "$authorized"
chown threaddock-deploy:threaddock-deploy "$authorized"
install -d -m 0755 -o threaddock-deploy -g threaddock-deploy /opt/threaddock /opt/threaddock/releases
install -d -m 0700 -o threaddock-deploy -g threaddock-deploy /opt/threaddock/shared /opt/threaddock/backups
printf '%s\n' 'Bootstrap complete. Configure /opt/threaddock/shared/.env before enabling deployments.'
printf '%s\n' 'Verified SSH host public key:'
cat /etc/ssh/ssh_host_ed25519_key.pub
