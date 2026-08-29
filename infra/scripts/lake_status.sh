#!/usr/bin/env bash
#
# Show the analytics server's stack health and lake row counts.
#
#   ./lake_status.sh [--host root@HOST] [--dir /opt/f10-dashboard/infra]
#
# The host defaults to the droplet in the Terraform state. Credentials are
# read from the server's own infra/.env and never printed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA="$(dirname "$HERE")"

HOST=""
DIR="/opt/f10-dashboard/infra"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --dir)  DIR="$2"; shift 2 ;;
    -h|--help)
      awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "${BASH_SOURCE[0]}"
      exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$HOST" ]]; then
  ip="$(terraform -chdir="$INFRA/terraform" output -raw droplet_ip 2>/dev/null || true)"
  [[ -n "$ip" ]] || { echo "no --host given and no droplet_ip in terraform output" >&2; exit 1; }
  HOST="root@${ip}"
fi

# Reuse one SSH connection for all four queries below (avoids tripping
# SSH rate limiting, same reason migrate_lake.sh multiplexes).
SSH_CTL="$(mktemp -d /tmp/f10st.XXXXXX)"
SSH_OPTS=(-o BatchMode=yes -o ControlMaster=auto -o "ControlPath=${SSH_CTL}/%h" -o ControlPersist=60)
trap 'ssh -o "ControlPath=${SSH_CTL}/%h" -O exit "$HOST" 2>/dev/null || true; rm -rf "$SSH_CTL"' EXIT

echo "== $HOST =="

ssh "${SSH_OPTS[@]}" "$HOST" "cd '$DIR' && docker compose ps --format 'table {{.Service}}\t{{.Status}}'" \
  || echo "  (could not read compose status)"

echo
echo "-- ingest health --"
ssh "${SSH_OPTS[@]}" "$HOST" "curl -s -m5 localhost:8090/health || echo unreachable"

echo
echo "-- wireguard --"
ssh "${SSH_OPTS[@]}" "$HOST" \
  "wg show wg0 2>/dev/null | grep -E 'listening port|peer|latest handshake' || echo '  wg0 not up'"

echo
echo "-- lake --"
ssh "${SSH_OPTS[@]}" "$HOST" "cd '$DIR' && \
  CH_USER=\$(sed -n 's/^CH_USER=//p' .env | head -1) && \
  CH_PASS=\$(sed -n 's/^CH_PASS=//p' .env | head -1) && \
  docker compose exec -T clickhouse clickhouse-client --user \"\$CH_USER\" --password \"\$CH_PASS\" \
    --query \"
      SELECT line FROM (
        SELECT 1 AS i, 'samples      ' || toString(count()) AS line FROM telemetry.samples
        UNION ALL SELECT 2, 'sessions     ' || toString(count()) FROM telemetry.sessions
        UNION ALL SELECT 3, 'vehicles     ' || toString(count()) FROM telemetry.vehicles
        UNION ALL SELECT 4, 'mapping_ver  ' ||
          if(count() = 0, '(none)', arrayStringConcat(groupUniqArray(mapping_ver), ','))
          FROM telemetry.samples
        UNION ALL SELECT 5, 'span         ' ||
          if(count() = 0, 'n/a', toString(min(ts)) || ' .. ' || toString(max(ts)))
          FROM telemetry.samples
      ) ORDER BY i
    \"" || echo "  (could not query ClickHouse)"
