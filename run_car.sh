#!/usr/bin/env bash
# Launch live.py with every VERIFIED proprietary channel (DDE dynamic +
# flow + DPF/EGR + gearbox, and the EGS gear). Use this instead of a bare
# `live.py` so the dashboard always has gear, temps, DPF, etc.
#   ./run_car.sh                 # logs to a timestamped session db
#   ./run_car.sh --db my.db      # or pass your own live.py flags
#   ./run_car.sh --mode long     # start in a quieter drive mode
#
# A VALIDATION DRIVE adds one or more issue #15 candidate files on top of
# the verified set, by name, without editing this script:
#   ./run_car.sh --candidate egr
#   ./run_car.sh --candidate injectors --candidate ibs --mode long
# The names are the candidate file stems listed in candidate_file()
# below; an unknown name refuses to start and prints the list. Nothing
# else changes: a bare launch composes exactly the command it always
# did, so the default set stays the production/verified one.
#
# Poll rates come from the mapping files (wall-clock per channel) and are
# scaled at runtime by the drive mode, switchable from the dashboard.
# `--rate` is only the loop granularity - it caps how fast the fastest
# tier can go, it does not set any channel's rate. There is no rate
# override flag at all any more: rates live in the mapping files, and a
# drive mode is how you scale them for one trip (and gets recorded).
cd "$(dirname "$0")"

# name -> candidate mapping file. Keep in step with docs/TELEMETRY_CANDIDATES.md.
candidate_file() {
  case "$1" in
    injectors|egr|airpath|ibs|tank)
      echo "mappings/candidates/bmw/dde/n47/d72n47a0_$1.yaml" ;;
    egs-speeds) echo "mappings/candidates/bmw/egs/f10_transmission_speeds.yaml" ;;
    sae-extra)  echo "mappings/candidates/obd/engine_sae_extra.yaml" ;;
    *) return 1 ;;
  esac
}
CANDIDATE_NAMES="injectors egr airpath ibs tank egs-speeds sae-extra"

# Pull `--candidate NAME` / `--candidate=NAME` out; everything else is
# forwarded to live.py untouched and in order.
CANDIDATE_ARGS=()
SEEN=" "
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --candidate)
      [[ $# -ge 2 ]] || { echo "run_car.sh: --candidate needs a name (one of: $CANDIDATE_NAMES)" >&2; exit 2; }
      NAME="$2"; shift 2 ;;
    --candidate=*)
      NAME="${1#--candidate=}"; shift ;;
    *)
      ARGS+=("$1"); shift; continue ;;
  esac
  if ! FILE="$(candidate_file "$NAME")"; then
    echo "run_car.sh: unknown candidate '$NAME'; known: $CANDIDATE_NAMES" >&2
    exit 2
  fi
  case "$SEEN" in *" $NAME "*)
    echo "run_car.sh: candidate '$NAME' given twice" >&2; exit 2 ;;
  esac
  [[ -f "$FILE" ]] || { echo "run_car.sh: candidate '$NAME' -> $FILE is missing" >&2; exit 2; }
  SEEN="$SEEN$NAME "
  CANDIDATE_ARGS+=(--extra-mappings "$FILE")
done
set -- "${ARGS[@]+"${ARGS[@]}"}"

DB_DEFAULT="local/sessions/drive-$(date -u +%Y%m%dT%H%M%SZ).db"
case " $* " in *" --db "*|*" --no-db "*) DB_ARG="";; *) DB_ARG="--db $DB_DEFAULT";; esac
exec python3 live.py \
  --extra-mappings mappings/candidates/bmw/dde/n47/d72n47a0_dynamic.yaml \
  --extra-mappings mappings/candidates/bmw/dde/n47/d72n47a0_flow.yaml \
  --extra-mappings mappings/candidates/bmw/dde/n47/d72n47a0_dpf_egr.yaml \
  --extra-mappings mappings/candidates/bmw/dde/n47/d72n47a0_gearbox.yaml \
  --extra-mappings mappings/candidates/bmw/egs/f10_transmission.yaml \
  "${CANDIDATE_ARGS[@]+"${CANDIDATE_ARGS[@]}"}" \
  --rate 10 $DB_ARG "$@"
