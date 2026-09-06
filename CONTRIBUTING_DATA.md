# Contributing mapping data

The runtime knows nothing about BMW; the mapping files under
`mappings/` are the whole of that knowledge, and every row in them is
traceable to something. A contribution that adds or changes a channel
is judged on that trace, not on whether the number looks right. The
long form is `docs/MAPPING_RESEARCH.md`; this is the checklist.

## What a mapping contribution must carry

1. **Applicability.** Which ECU (`dde`, `egs`, …), which family
   (`n47`), which diagnostic variant (`d71` / `d72` / `d73` — different
   ECUs for our purposes; they never merge), and which capability
   probe proves it (`bmwdiag/variant.py`). A mapping that applies "to
   BMW diesels" does not exist.
2. **A source, per file and per override.** `source.type` from the
   closed list (`obd_standard | prg | ediabas | tool32 | ista | trace |
   manual | synthetic`) with the minimum `docs/MAPPING_RESEARCH.md`
   requires for that type: SGBD name, job and result for a table;
   capture file, tool and vehicle state for a trace; who and from what
   for `manual`. The source itself must be in
   `research/manifests/sources.yaml`, pinned, with its licence
   determined (`unknown` is a determination, not a gap).
3. **An evidence tier** for research records — A: raw frames that can
   be shown; B: a structured table (`sgbd_derived`); C: a claim in code
   or configuration; D: untraceable. Tier D never produces anything
   executable; it may be recorded as a lead.
4. **A provenance label on every fact**: `wire_observation` /
   `sgbd_derived` / `source_claim` / `inference` / `speculation`.
   Unknown stays the string `"unknown"` — not `null`, not a plausible
   default.
5. **A verification state that is true.** Mapping files use
   `discovered | candidate | verified | rejected`; `verified` means
   verified on `F10-520d-dev`, and `verification.method` says how —
   what was cross-checked against what, over what range. A single warm
   reading is not a validated scale. Research records use the longer
   ladder (`discovered → candidate → externally_verified →
   cross_source_confirmed → locally_verified → rejected`); only
   `locally_verified` means "on this car".
6. **Licence-compatible use.** Facts cited from a GPL / AGPL /
   PolyForm / licence-unknown source are fine; code or bulk tables from
   them are not. A source flagged `license.bulk_redistribution:
   withheld` contributes individual, cited facts only, and the build
   refuses to list its bulk in a tracked report.
7. **The version bump and the lock.** `mapping.version` goes up by one
   for any content change to that file (not for comments), and
   `mappings/VERSIONS.lock` is regenerated (`python3 -m bmwdiag.mapping
   lock mappings/`) in the same commit. `docs/DATA_VERSIONING.md`.
8. **Vehicle configuration** where it changes what a channel means.
   This car has no particulate filter, which is why two soot channels
   read an ECU model, not a pipe. That kind of fact goes in the
   mapping's `notes`; it cost weeks the one time it was not written
   down.

## Where it goes

- `mappings/candidates/…` with `production: false`, until verified on
  the car by a supervised read-only run (`tools/validate_candidate.py`;
  artifact under `validation-runs/`).
- Never `mappings/obd/engine.yaml` for anything proprietary. The
  production set is standard SAE only, and a test byte-pins it.
- Research evidence (`research/evidence/n47/`) is hand-transcribed with
  citations; the normalized JSONL is generated and not tracked
  (`research/normalized/README.md`).

## Not accepted

- Anything taken from ISTA, Tool32/EDIABAS SGBDs, PRG/GRP or DATEN
  files directly, or from a decompiled app. Those are research sources
  the owner may consult offline; their content is not redistributed.
- Bulk tables (hundreds of rows from one export), even with a source.
  `research/reports/legal-and-license-notes.md` explains the
  database-right problem and `docs/HISTORY_REWRITE.md` what it cost
  the first time.
- A VIN, a raw capture, or anything from `local/`.
- A decode whose scale was fitted to make one reading look right.
- Anything that executes. Mappings are plain data so they can compile
  to C later: no expressions, no eval, no hooks.
