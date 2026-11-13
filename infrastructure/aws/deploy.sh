#!/usr/bin/env bash
# Run on an already provisioned Linux host with Docker Engine/Compose installed.
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 || ! "$1" =~ ^(cloud|hospital)$ || ( $# -eq 3 && "$3" != --metaguard && "$3" != --full-system ) ]]; then
  echo "Usage: bash infrastructure/aws/deploy.sh cloud|hospital /absolute/path/to/settings.env [--metaguard|--full-system]" >&2
  exit 2
fi
role="$1"
settings="$(realpath -- "$2")"
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
[[ -f "$settings" ]] || { echo "Settings file does not exist" >&2; exit 2; }
if grep -q 'REPLACE_' "$settings"; then
  echo "Replace all placeholders in the private env file first." >&2
  exit 2
fi
# Do not source the file or print a resolved configuration containing passwords.
cd -- "$repo"
docker info >/dev/null
compose=(docker compose --env-file "$settings"
  -f "infrastructure/docker/compose.${role}.yaml"
  -f "infrastructure/aws/compose.${role}.override.yaml")
audit=(python3 infrastructure/scripts/audit_deployment.py "$role" --env-file "$settings")
if [[ ${3:-} == --metaguard || ${3:-} == --full-system ]]; then
  [[ $(stat -c '%a' "$settings") =~ ^(400|600)$ ]] || { echo "MetaGuard settings must be owner-only readable (chmod 600)." >&2; exit 2; }
  overlay="infrastructure/aws/compose.metaguard.${role}.yaml"
  compose+=(-f "$overlay")
  audit+=(--compose-file "infrastructure/docker/compose.${role}.yaml"
    --compose-file "infrastructure/aws/compose.${role}.override.yaml" --compose-file "$overlay")
fi
if [[ ${3:-} == --full-system && $role == cloud ]]; then
  compose+=(-f infrastructure/aws/compose.dashboard.cloud.yaml)
  audit+=(--compose-file infrastructure/aws/compose.dashboard.cloud.yaml)
fi
"${compose[@]}" config --quiet
"${audit[@]}"
"${compose[@]}" up -d --build --wait --wait-timeout 180
"${compose[@]}" ps
