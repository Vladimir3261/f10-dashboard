# Legal and license notes

Recorded risks and facts, not legal conclusions. This project may
become a commercial product, so the separation below is maintained
now, while it is cheap. Licenses were read at the pinned revisions in
`research/manifests/sources.yaml` (retrieved 2026-08-25).

## The separation

| Layer | What | Status |
|---|---|---|
| Third-party source code | klartext (AGPL-3.0), ediabaslib (GPL-3.0), ediabasx (PolyForm-NC), etc. | **Never copied.** Used as offline oracles and format references, cited by `file:line`. |
| Third-party proprietary data | BMW `.prg`/`.grp`, ISTA/DATEN databases | **Never downloaded, committed, or redistributed.** Future PRG work only on user-supplied, legally-acquired installations, processed offline. |
| Derived technical facts | identifiers, byte sequences, scaling constants, units, protocol observations | Extracted **with provenance** into research records; individual facts are treated as uncopyrightable, bulk extraction is treated as a database-right risk (below). |
| Our original runtime code | `live.py`, `bmwdiag/` | Clean; no third-party code, stdlib only. |
| Our original mapping format | `mappings/`, `docs/MAPPING_ARCHITECTURE.md` | Clean; designed here. |

## Per-artifact clean-room classification

| Imported artifact | Classification |
|---|---|
| `research/evidence/n47/wican_752/exchange.yaml` | manually transcribed protocol facts (raw bytes + metadata from a public issue; no prose copied) |
| `research/evidence/n47/klartext_f25/*.yaml` | manually transcribed protocol facts + structured factual data (scaling rows), cited to AGPL sources; **no code, no prose copied** |
| `research/evidence/n47/f10_field/oil_pressure_586F.yaml` | manually transcribed protocol facts from an MIT source |
| `research/evidence/n47/obdb/egs_dids.yaml` | structured factual data from a CC-BY-SA-4.0 source, attributed; share-alike applies if redistributed |
| `research/normalized/n47/*.jsonl` (d73 portion) | structured factual data mechanically derived from the license-unknown gist — see the flag below |
| `tests/research/fixtures/*` | small factual excerpts for deterministic tests, each with a source header |
| `mappings/candidates/bmw/dde/n47/*.yaml` | independently authored files in our own format encoding cited protocol facts |
| runtime `setup:` extension (`bmwdiag/`) | independently reproduced behavior — designed from the *documented wire sequence*, no reference implementation consulted for code |

## ⚠ Flags requiring review before any public hosting

1. **`research/normalized/n47/signals.jsonl`** contains a mechanical
   derivative of all 1645 rows of the MorGuux gist (license `unknown`),
   which is itself an export of BMW's `D73N47A0` SGBD table. Individual
   facts are fine to *use*; **redistributing the bulk set** raises both
   the gist-license question and an EU **database-right** question on
   BMW's side. The repository currently has zero commits and is not
   hosted; resolve before publishing, or regenerate-on-demand from the
   cache instead of committing the file.
2. **`ediabasx-docs-sgbd`** publishes whole PRG-derived tables. We used
   it only for individual row cross-checks (a handful of quoted rows
   with citations); do not bulk-scrape it, and do not mirror it.
3. **CC-BY-SA-4.0 (OBDb)**: the few extracted rows carry attribution in
   the evidence file; if the *data* is redistributed, share-alike terms
   apply to the derived data set.

## License register (verified at pin)

| Source | License | Commercial-runtime copy | Role |
|---|---|---|---|
| klartext | AGPL-3.0-or-later (SPDX headers "or-later"; GitHub shows AGPL-3.0) | no | oracle, Tier A evidence |
| EdiabasLib | GPL-3.0 | no | format reference, oracle |
| EdiabasX | PolyForm-Noncommercial-1.0.0 | no | offline oracle only |
| Bimmerz Box | PolyForm-Noncommercial-1.0.0 | no | architecture reference |
| MorGuux gist | none → unknown | no | Tier B table |
| ediabasx-docs-sgbd | none → unknown (PRG-derived) | no | row-lookup oracle |
| BimmerDis / BimmerJson | none → unknown | no | future extraction path |
| BimmerDaten | GPL-3.0 (LICENSE file; API says NOASSERTION) | no | oracle |
| MotorMouth93/BMW-XDFs | none → unknown | no | Tier C config facts |
| GovMateAi/bmw-pro-diagnostic | MIT | moot — rejected as a source | none |
| OBDb/BMW-5-Series | CC-BY-SA-4.0 | data with attribution + share-alike | Tier C claims |
| obd-gauge-cluster | MIT | yes | Tier A evidence, methodology |
| bmw-dash-display | MIT (verified at pin) | yes | cross-check, leads |
| dieslg8 / deepobd-configs | MIT | yes | ancestry comparators |
| wican-fw | GPL-3.0 (repo); issue text unknown | no | Tier A capture (facts) |
| freecarly | decompiled proprietary app | **rejected** | none |

## Standing rules applied

- No BMW PRG, GRP, ISTA, DATEN or database file was searched for,
  downloaded, committed, or redistributed.
- No pirated BMW Standard Tools / ISTA datasets were sought.
- Public availability was never treated as permission to redistribute.
- AGPL/GPL/PolyForm sources contribute *cited facts and test vectors*,
  never implementation; the runtime remains dependency-free and
  original.
- The decompiled-app source was rejected outright.
