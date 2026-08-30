#!/usr/bin/env bash
# Launch live.py with every VERIFIED proprietary channel (DDE dynamic +
# flow + DPF/EGR + gearbox, and the EGS gear). Use this instead of a bare
# `live.py` so the dashboard always has gear, temps, DPF, etc.
#   ./run_car.sh                 # logs to a timestamped session db
#   ./run_car.sh --db my.db      # or pass your own live.py flags
#   ./run_car.sh --mode long     # start in a quieter drive mode
#
# Poll rates come from the mapping files (wall-clock per channel) and are
# scaled at runtime by the drive mode, switchable from the dashboard.
# `--rate` is only the loop granularity - it caps how fast the fastest
# tier can go, it does not set any channel's rate. There is deliberately
# no `--slow-every` here any more: it forces `slow` back to the legacy
# cycle-based cadence and would override what the mappings declare.
cd "$(dirname "$0")"
DB_DEFAULT="local/sessions/drive-$(date -u +%Y%m%dT%H%M%SZ).db"
case " $* " in *" --db "*|*" --no-db "*) DB_ARG="";; *) DB_ARG="--db $DB_DEFAULT";; esac
exec python3 live.py \
  --extra-mappings mappings/candidates/bmw/dde/n47/d72n47a0_dynamic.yaml \
  --extra-mappings mappings/candidates/bmw/dde/n47/d72n47a0_flow.yaml \
  --extra-mappings mappings/candidates/bmw/dde/n47/d72n47a0_dpf_egr.yaml \
  --extra-mappings mappings/candidates/bmw/dde/n47/d72n47a0_gearbox.yaml \
  --extra-mappings mappings/candidates/bmw/egs/f10_transmission.yaml \
  --rate 10 $DB_ARG "$@"
