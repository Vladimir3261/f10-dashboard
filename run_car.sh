#!/usr/bin/env bash
# Launch live.py with every VERIFIED proprietary channel (DDE dynamic +
# flow + DPF/EGR + gearbox, and the EGS gear). Use this instead of a bare
# `live.py` so the dashboard always has gear, temps, DPF, etc.
#   ./run_car.sh                 # logs to a timestamped session db
#   ./run_car.sh --db my.db      # or pass your own live.py flags
cd "$(dirname "$0")"
DB_DEFAULT="local/sessions/drive-$(date -u +%Y%m%dT%H%M%SZ).db"
case " $* " in *" --db "*|*" --no-db "*) DB_ARG="";; *) DB_ARG="--db $DB_DEFAULT";; esac
exec python3 live.py \
  --extra-mappings mappings/candidates/bmw/dde/n47/d72n47a0_dynamic.yaml \
  --extra-mappings mappings/candidates/bmw/dde/n47/d72n47a0_flow.yaml \
  --extra-mappings mappings/candidates/bmw/dde/n47/d72n47a0_dpf_egr.yaml \
  --extra-mappings mappings/candidates/bmw/dde/n47/d72n47a0_gearbox.yaml \
  --extra-mappings mappings/candidates/bmw/egs/f10_transmission.yaml \
  --rate 10 --slow-every 100 $DB_ARG "$@"
