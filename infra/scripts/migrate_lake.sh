#!/usr/bin/env bash
#
# Migrate the ClickHouse telemetry lake from one server to another.
#
# Streams rows old -> (this machine) -> new over two SSH connections, so the
# two servers never need to reach each other. Nothing is written to disk and
# no credentials are passed on a command line: each side's ClickHouse
# password is read from that host's own infra/.env at run time.
#
#   ./migrate_lake.sh --from root@OLD_HOST [--to root@NEW_HOST] [options]
#
# --to defaults to the droplet in the Terraform state, so after `make
# provision` you normally only pass --from.
#
# SAFE TO RE-RUN. telemetry.samples and telemetry.sessions are
# ReplacingMergeTree, so re-inserting the same rows collapses on merge
# rather than duplicating. An interrupted run can simply be repeated.
#
# Schema drift is handled: only columns present on BOTH sides are copied
# (a column the destination added later - e.g. sessions.mappings - keeps its
# default for migrated rows, rather than breaking the transfer).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA="$(dirname "$HERE")"

SRC=""
DST=""
SRC_DIR="/root/f10-dashboard/infra"     # where the OLD stack lives
DST_DIR="/opt/f10-dashboard/infra"      # where Ansible puts the new stack
TABLES="sessions vehicles samples"      # samples last: biggest, and needs the others
DRY_RUN=0

# Print the header comment block (everything after the shebang up to the
# first non-comment line) as the help text.
usage() {
  awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "${BASH_SOURCE[0]}"
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from)     SRC="$2"; shift 2 ;;
    --to)       DST="$2"; shift 2 ;;
    --src-dir)  SRC_DIR="$2"; shift 2 ;;
    --dst-dir)  DST_DIR="$2"; shift 2 ;;
    --tables)   TABLES="$2"; shift 2 ;;
    --dry-run)  DRY_RUN=1; shift ;;
    -h|--help)  usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
done

log()  { printf '\033[1;34m[migrate]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[migrate] WARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[migrate] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ -n "$SRC" ]] || die "--from is required (e.g. --from root@old-host). See --help."

# Default the destination to the droplet Terraform just created.
if [[ -z "$DST" ]]; then
  ip="$(terraform -chdir="$INFRA/terraform" output -raw droplet_ip 2>/dev/null || true)"
  [[ -n "$ip" ]] || die "--to not given and no droplet_ip in terraform output"
  DST="root@${ip}"
  log "destination from terraform output: $DST"
fi

# SSH connection multiplexing. A migration runs many queries; without this
# each one is a fresh TCP+auth handshake, and a server with fail2ban or
# sshd rate limiting starts REJECTing partway through ("Connection
# refused"). ControlMaster reuses ONE connection per host for the whole run.
# Deliberately under /tmp, not $TMPDIR: a unix socket path must stay under
# ~104 bytes and macOS $TMPDIR alone is already ~50.
SSH_CTL="$(mktemp -d /tmp/f10mig.XXXXXX)"
SSH_OPTS=(
  -o BatchMode=yes
  -o ControlMaster=auto
  -o "ControlPath=${SSH_CTL}/%h"
  -o ControlPersist=300
  -o ServerAliveInterval=30
)

cleanup() {
  local h
  for h in "$SRC" "$DST"; do
    [[ -n "$h" ]] && ssh -o "ControlPath=${SSH_CTL}/%h" -O exit "$h" 2>/dev/null || true
  done
  rm -rf "$SSH_CTL"
}
trap cleanup EXIT

# Run a ClickHouse query on a host. The query travels base64-encoded so no
# quoting can mangle it; credentials come from that host's own .env and are
# never echoed. Any extra args (e.g. --format) are appended.
ch() {
  local host="$1" dir="$2" query="$3"; shift 3
  local q64
  q64="$(printf '%s' "$query" | base64 | tr -d '\n')"
  ssh "${SSH_OPTS[@]}" "$host" \
    "cd '$dir' && \
     CH_USER=\$(sed -n 's/^CH_USER=//p' .env | head -1) && \
     CH_PASS=\$(sed -n 's/^CH_PASS=//p' .env | head -1) && \
     docker compose exec -T clickhouse clickhouse-client \
       --user \"\$CH_USER\" --password \"\$CH_PASS\" \
       --query \"\$(printf %s '$q64' | base64 -d)\" $*"
}

# Open (and keep) the shared master connection to a host, retrying a few
# times. A server that rate-limits SSH may refuse briefly; backing off and
# retrying is far better than losing a part-finished migration.
connect() {
  local host="$1" attempt
  for attempt in 1 2 3 4 5; do
    if ssh "${SSH_OPTS[@]}" -o ConnectTimeout=15 -N -f "$host" 2>/dev/null \
       || ssh "${SSH_OPTS[@]}" -o ConnectTimeout=15 "$host" true 2>/dev/null; then
      return 0
    fi
    warn "cannot reach $host (attempt $attempt/5) - backing off $((attempt * 10))s"
    warn "  if this persists, the host may be rate-limiting SSH (fail2ban):"
    warn "  check with:  ssh $host 'fail2ban-client status sshd'"
    sleep $((attempt * 10))
  done
  return 1
}

# --- pre-flight ------------------------------------------------------------

log "opening shared SSH connections..."
connect "$SRC" || die "cannot establish an SSH connection to $SRC"
connect "$DST" || die "cannot establish an SSH connection to $DST"

log "checking both ends are reachable and ClickHouse answers..."
src_v="$(ch "$SRC" "$SRC_DIR" "SELECT version()")" || die "cannot query source ClickHouse on $SRC"
dst_v="$(ch "$DST" "$DST_DIR" "SELECT version()")" || die "cannot query destination ClickHouse on $DST"
log "source      $SRC  (ClickHouse $src_v)"
log "destination $DST  (ClickHouse $dst_v)"

cols_of() {  # host dir table -> newline-separated column names
  ch "$1" "$2" "SELECT name FROM system.columns WHERE database='telemetry' AND table='$3' ORDER BY position"
}

total_copied=0

for table in $TABLES; do
  echo
  log "=== $table ==="

  src_cols="$(cols_of "$SRC" "$SRC_DIR" "$table" | tr -d '\r')"
  dst_cols="$(cols_of "$DST" "$DST_DIR" "$table" | tr -d '\r')"

  if [[ -z "$src_cols" ]]; then warn "$table does not exist on the source - skipping"; continue; fi
  if [[ -z "$dst_cols" ]]; then warn "$table does not exist on the destination - skipping"; continue; fi

  # Only columns both sides have. Destination-only columns (e.g. the newer
  # sessions.mappings) keep their DEFAULT for migrated rows.
  common="$(comm -12 <(echo "$src_cols" | sort) <(echo "$dst_cols" | sort))"
  [[ -n "$common" ]] || die "$table: no columns in common"
  collist="$(echo "$common" | paste -sd, -)"

  only_dst="$(comm -13 <(echo "$src_cols" | sort) <(echo "$dst_cols" | sort) | paste -sd, - || true)"
  [[ -n "$only_dst" ]] && log "destination-only columns (will take defaults): $only_dst"

  before_src="$(ch "$SRC" "$SRC_DIR" "SELECT count() FROM telemetry.$table" | tr -d '\r')"
  before_dst="$(ch "$DST" "$DST_DIR" "SELECT count() FROM telemetry.$table" | tr -d '\r')"
  log "rows: source=$before_src destination=$before_dst (before)"

  if [[ "$before_src" == "0" ]]; then log "nothing to copy"; continue; fi

  if [[ $DRY_RUN -eq 1 ]]; then
    log "DRY RUN - would copy $before_src row(s), columns: $collist"
    continue
  fi

  # Big tables are copied one month at a time: the source box is small and a
  # single unbounded SELECT can trip its memory limit. Everything else goes
  # in one stream.
  chunks=("")
  if [[ "$table" == "samples" ]]; then
    # Read the month list without `mapfile`, which is bash 4+ only - macOS
    # still ships bash 3.2, where it would fail exactly here, mid-migration.
    months_raw="$(ch "$SRC" "$SRC_DIR" \
      "SELECT DISTINCT toYYYYMM(ts) FROM telemetry.samples ORDER BY 1" | tr -d '\r')"
    chunks=()
    while IFS= read -r m; do
      [[ -n "$m" ]] && chunks+=("$m")
    done <<< "$months_raw"
    [[ ${#chunks[@]} -gt 0 ]] || chunks=("")
    log "copying in ${#chunks[@]} monthly chunk(s): ${chunks[*]}"
  fi

  for chunk in "${chunks[@]}"; do
    where=""
    label="all rows"
    if [[ -n "$chunk" ]]; then where="WHERE toYYYYMM(ts) = $chunk"; label="$chunk"; fi

    n="$(ch "$SRC" "$SRC_DIR" "SELECT count() FROM telemetry.$table $where" | tr -d '\r')"
    log "  $label: streaming $n row(s)..."

    # The actual transfer: Native format straight through this machine.
    ch "$SRC" "$SRC_DIR" "SELECT $collist FROM telemetry.$table $where FORMAT Native" \
      | ch "$DST" "$DST_DIR" "INSERT INTO telemetry.$table ($collist) FORMAT Native"
  done

  after_dst="$(ch "$DST" "$DST_DIR" "SELECT count() FROM telemetry.$table" | tr -d '\r')"
  log "rows: destination=$after_dst (after, +$((after_dst - before_dst)))"
  total_copied=$((total_copied + after_dst - before_dst))
done

echo
if [[ $DRY_RUN -eq 1 ]]; then
  log "dry run complete - nothing was written"
else
  log "migration complete: +$total_copied row(s) on the destination"
  log "verify with:  make lake-status"
  log "NOTE: counts settle after background merges collapse duplicate rows."
  log "      Re-running this script is safe and will not double-count."
fi
