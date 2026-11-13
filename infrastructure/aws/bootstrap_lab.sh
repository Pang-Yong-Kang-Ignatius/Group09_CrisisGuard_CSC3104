#!/usr/bin/env bash
# Prepare fresh Ubuntu 24.04 hosts. Secrets arrive later through pinned SSH.
set -euo pipefail
[[ ${1:-} == prepare && $# == 1 ]] || { echo 'Use launcher-managed prepare mode' >&2; exit 2; }
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl openssl openssh-client
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
# Public host key goes only to this instance's authenticated EC2 console output.
# Never trust an unauthenticated ssh-keyscan response as the initial pin.
printf 'CG_SSH_HOST_KEY '
cut -d ' ' -f 1,2 /etc/ssh/ssh_host_ed25519_key.pub
touch /var/lib/crisisguard-host-prepared
