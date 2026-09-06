# research/ — the N47 mapping research pipeline

Offline tooling that turns pinned public sources into normalized,
provenance-carrying research records, and gates which of them may become
candidate runtime mappings. Nothing here runs at vehicle time; live.py
does not import this package.

```
public source (pinned)                     local/research-cache/ (gitignored)
        |                                          |
        v                                          v
research/importers/*         <- deterministic, preserve-everything parsers
        |
        v
research/normalized/n47/*.jsonl    partial knowledge stays partial
        |
        +--> research/gate.py          may this become executable? (16 requirements)
        +--> research/conflicts.py     who disagrees with whom
        |
        v
mappings/candidates/bmw/dde/n47/   production: false, capability-gated
        |
        v   (supervised on-car validation, one request at a time)
mappings/verified/bmw/dde/n47/     empty until something is locally verified
```

## Commands

```
python3 -m research.build          # re-import everything, rewrite normalized + generated reports
python3 -m research.build --evidence-only   # committed evidence only: no cache, no network (CI)
python3 -m research.build --reports-only    # rewrite the two generated reports from a FULL normalized set
                                            # (refuses on an --evidence-only set; --force overrides)
python3 -m unittest discover tests.research   # pipeline tests, no car, no network
python3 -m bmwdiag.mapping validate mappings/ # candidates validate like any mapping
```

A full `build` needs the source cache — see
[sources/README.md](sources/README.md). **The normalized output is not
tracked**: `normalized/n47/*.jsonl` is gitignored and regenerated on
demand, because most of it is a bulk derivative of a licence-unknown
SGBD export (see [normalized/README.md](normalized/README.md) and the
legal notes). The tests run on committed fixtures either way and skip,
visibly, the few that need the generated files.

## Layout

| Path | What |
|---|---|
| `manifests/sources.yaml` | every source: pin, license, trust tier, ancestry relationships |
| `importers/` | one deterministic importer per source format |
| `model.py` / `gate.py` / `conflicts.py` | record model, candidate gate, conflict detection |
| `evidence/n47/` | committed transcriptions of source-backed exchanges, with citations |
| `normalized/n47/` | generated JSONL (signals / requests / jobs / evidence) — gitignored, regenerate on demand |
| `reports/` | generated (`n47-coverage`, `n47-conflicts`) + hand-written (audit, legal, unresolved) |

## Rules (short form; docs/MAPPING_RESEARCH.md has the long one)

- **No invented BMW data.** Unknown stays `"unknown"`; every fact is
  labelled `wire_observation` / `sgbd_derived` / `source_claim` /
  `inference` / `speculation`.
- **Variants never merge.** `d71`, `d72`, `d73` coexist; an identifier
  is meaningful only WITH its variant's request pattern.
- **Tier D never executes.** Untraceable claims are leads, not mappings.
- **Only `read_only_telemetry` may poll.** Anything else — or `unknown`
  — is excluded at the gate.
- **Only `locally_verified` means verified for our F10.** External
  verification is about the source's car, and says so.
