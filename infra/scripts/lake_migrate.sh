#!/usr/bin/env bash
#
# Apply the ClickHouse schema migrations to the analytics server.
#
#   ./lake_migrate.sh [--host root@HOST] [--dir /opt/f10-dashboard/infra]
#                     [--dry-run] [--only FILE]
#
# Why this exists: `clickhouse/init/001_schema.sql` runs ONLY on a fresh
# volume, and `make deploy` does not touch the schema at all. So a column
# added to the init script is invisible to a lake that already exists, and
# the ingest server silently drops it - ClickHouse runs with
# input_format_skip_unknown_fields=1, so an unknown column is not an error.
# That failure mode is the dangerous kind: the drive looks healthy and the
# column is quietly absent. It cost `sessions.mode` and
# `sessions.clock_synced` exactly that way.
#
# Every migration is written to be idempotent (ADD COLUMN IF NOT EXISTS),
# so re-running the whole directory is safe and is the intended usage.
# Migrations are applied in filename order, which is why they are dated.
#
# The host defaults to the droplet in the Terraform state. Credentials are
# read from the server's own infra/.env and are never printed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA="$(dirname "$HERE")"
MIGRATIONS="$INFRA/clickhouse/migrations"

HOST=""
DIR="/opt/f10-dashboard/infra"
DRY_RUN=0
ONLY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)    HOST="$2"; shift 2 ;;
    --dir)     DIR="$2"; shift 2 ;;
    --only)    ONLY="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
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

shopt -s nullglob
files=("$MIGRATIONS"/*.sql)
shopt -u nullglob

if [[ -n "$ONLY" ]]; then
  files=("$MIGRATIONS/$(basename "$ONLY")")
  [[ -f "${files[0]}" ]] || { echo "no such migration: $ONLY" >&2; exit 1; }
fi

if [[ ${#files[@]} -eq 0 ]]; then
  echo "no migrations in $MIGRATIONS"
  exit 0
fi

# One multiplexed SSH connection for every file: a separate connection per
# migration trips the server's rate limiting once there are more than a
# handful, the same reason lake_status.sh and migrate_lake.sh multiplex.
SSH_CTL="$(mktemp -d /tmp/f10mg.XXXXXX)"
SSH_OPTS=(-o BatchMode=yes -o ControlMaster=auto -o "ControlPath=${SSH_CTL}/%h" -o ControlPersist=60)
trap 'ssh -o "ControlPath=${SSH_CTL}/%h" -O exit "$HOST" 2>/dev/null || true; rm -rf "$SSH_CTL"' EXIT

echo "== $HOST =="
echo "applying ${#files[@]} migration(s) from clickhouse/migrations/"
[[ $DRY_RUN -eq 1 ]] && echo "(dry run - nothing will be executed)"
echo

# Read credentials once, on the server, rather than per file.
remote_ch() {
  ssh "${SSH_OPTS[@]}" "$HOST" "cd '$DIR' && \
    CH_USER=\$(sed -n 's/^CH_USER=//p' .env | head -1) && \
    CH_PASS=\$(sed -n 's/^CH_PASS=//p' .env | head -1) && \
    docker compose exec -T clickhouse clickhouse-client \
      --user \"\$CH_USER\" --password \"\$CH_PASS\" $*"
}

failed=0

for f in "${files[@]}"; do
  name="$(basename "$f")"

  if [[ $DRY_RUN -eq 1 ]]; then
    printf '  %-45s would apply\n' "$name"
    continue
  fi

  # Piped in over stdin: the file never lands on the server, so there is
  # nothing to clean up and no stale copy to apply by accident later.
  if remote_ch --multiquery < "$f" >/dev/null 2>"$SSH_CTL/err"; then
    printf '  %-45s ok\n' "$name"
  else
    printf '  %-45s FAILED\n' "$name"
    sed 's/^/      /' "$SSH_CTL/err" | grep -v 'level=warning' | head -5
    failed=1
  fi
done

[[ $DRY_RUN -eq 1 ]] && exit 0

echo
echo "-- sessions columns now --"
remote_ch --query "SELECT name FROM system.columns
  WHERE database='telemetry' AND table='sessions' ORDER BY position FORMAT TSV" \
  2>/dev/null | sed 's/^/  /' || echo "  (could not read the schema back)"

if [[ $failed -ne 0 ]]; then
  echo
  echo "at least one migration failed - the schema above may be incomplete" >&2
  exit 1
fi
