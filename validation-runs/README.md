# validation-runs/

On-car validation artifacts, one directory per run of
`tools/validate_candidate.py`. **Every run writes here** — this is the
permanent record of what was asked of the car and what it answered.

```
validation-runs/<UTC-timestamp>-<cmd>/
    run.json       full machine record: environment, every wire frame
                   (tx / rx / nrc / latency), decoded values, outcomes
    summary.md     human-readable: what was sent, what came back, what
                   decoded, and the plausibility questions to answer
    frames.ndjson  one JSON object per wire frame, in order
```

Files here are **VIN-redacted** and safe to commit. The unredacted copy
of each run — which contains the VIN read from the gateway — is written
to `local/validation-runs-raw/` and is gitignored, exactly like
`telemetry.db` and the rest of the per-car data (see the top-level
README's "Data and privacy" section).

## Workflow

1. Stop `live.py` (the ZGW serves one HSFZ client at a time).
2. `python3 tools/validate_candidate.py identify` — confirm the engine
   ECU and read its identity; resolve the DDE variant offline.
3. `python3 tools/validate_candidate.py run <candidate.yaml> <request>` —
   run one request; read its `summary.md`.
4. Answer each plausibility box against the physical state of the car.
5. Promote: edit the candidate mapping's `verification.status` to
   `locally_verified` (vehicle `F10-520d-dev`) for anything confirmed,
   or `rejected` with the NRC/reason for anything the car refused.

Nothing is promoted automatically — these artifacts are the evidence a
human weighs before editing a mapping.

A negative response in a frame is recorded as the **number** the ECU
sent — `"nrc": 49` with `"nrc_hex": "0x31"`, `"nrc_name":
"requestOutOfRange"`, `"service": 34` and, when the transport kept the
frame, `"raw": "7f 22 31"` — since 2026-09-05 (issue #11). Runs before
that date have `nrc` as prose (`"negative response to 0x22: NRC 0x31"`)
or `null`; they are historical evidence and are not rewritten.

The runner is **read-only**: every frame passes a service allowlist
(`0x01/0x09/0x22/0x19/0x3E`, plus `0x2C` define/clear/read
subfunctions) before it leaves the machine; write and control services
abort the run.
