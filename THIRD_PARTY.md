# Third-party material

The runtime (`bmwdiag/`, `live.py`, `infra/`, the Pi scripts) is
original and depends on the Python standard library only. Third-party
material enters this repository in one way: as **cited facts** in
mapping files and research records, from sources pinned by commit or
hash in `research/manifests/sources.yaml`.

The licence of each source, as determined at its pinned revision, is
the **licence register** in
[`research/reports/legal-and-license-notes.md`](research/reports/legal-and-license-notes.md#license-register-verified-at-pin).
That file is the authority; this page says only how each class is
used.

| Licence class | Sources (the register has the pins) | What is taken |
|---|---|---|
| MIT | obd-gauge-cluster, bmw-dash-display, dieslg8, deepobd-configs | facts and test vectors; no code copied |
| GPL-3.0 / AGPL-3.0 | klartext, EdiabasLib, BimmerDaten, wican-fw | facts cited by file, one config fragment as a test fixture; **no code, no bulk tables**; the runtime is not a derivative |
| PolyForm-Noncommercial-1.0.0 | EdiabasX, Bimmerz Box | offline oracle / architecture reference only |
| CC-BY-SA-4.0 | OBDb/BMW-5-Series | Tier C claims with attribution; anything reproduced from it is share-alike |
| none → unknown | MorGuux `D73N47A0` gist, ediabasx-docs-sgbd, BimmerDis / BimmerJson, MotorMouth93 XDFs | individual cited facts; **bulk redistribution withheld** (`license.bulk_redistribution: withheld` in the manifest; `docs/HISTORY_REWRITE.md` for the blob that was published before this rule) |
| rejected | freecarly (decompiled proprietary app); GovMateAi (MIT, rejected on content) | nothing |

BMW's own files (PRG, GRP, SGBD, ISTA, DATEN) are not in this
repository and were not searched for. The proprietary-derived channels
that are here (`mappings/candidates/`) name the SGBD, job and result
they were derived from, load only through an explicit
`--extra-mappings`, and are the subject of the open flags in the legal
notes.

## Test fixtures

`tests/research/fixtures/` holds small excerpts of three sources — 19
CSV rows, one XML job fragment, five custom jobs — so the importers can
be tested without the source cache. `tests/research/fixtures/README.md`
gives the origin, pin, licence and reason for each.

## Infrastructure

No third-party Python package is vendored or required. `infra/` runs
ClickHouse (Apache-2.0) and Grafana (AGPL-3.0) as Docker images under
their own licences; nothing from either is in this tree beyond
configuration and provisioning files.
