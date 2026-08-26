# drive-sessions/

Analysis output from `analysis/session_report.py`, one directory per
analysed run. Each holds:

- `report.md` — cold-start warm-up, proprietary-DDE-vs-standard-OBD live
  cross-checks, drive/load ranges + actual-vs-setpoint deviation, DPF,
  data-quality/coverage, and a **Key findings** interpretation.
- `summary.json` — the same metrics, machine-readable.
- `curves.html` — self-contained SVG plots (no dependencies).

These are VIN-redacted and safe to commit. The raw telemetry they are
computed from lives in a gitignored session DB under `local/sessions/`
(it carries the VIN in `runs.vin`). Regenerate any report with:

    python3 -m analysis.session_report --db local/sessions/<file>.db --out drive-sessions
