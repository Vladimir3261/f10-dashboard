# research/normalized — generated, not tracked

`n47/{signals,requests,jobs,evidence}.jsonl` are the normalized research
records: one JSON line per claim, key-sorted, line-sorted, byte-identical
on every rebuild from the same inputs.

They are **gitignored** (`.gitignore`: `/research/normalized/**/*.jsonl`).
Most of `signals.jsonl` (1619 of ~1685 rows) is a mechanical derivative of
the MorGuux `D73N47A0` gist, whose licence — and the database right on
the BMW table it exports — is unresolved. The manifest records that as
`license.bulk_redistribution: withheld` on `morguux-d73n47a0`, and
`research/reports/legal-and-license-notes.md` explains it. Individual
facts from that table are still used, cited, and reproduced in the
tracked coverage report when someone has given them a normalized name;
the bulk is not redistributed.

## Regenerate

```bash
# full set: needs the pinned source cache under local/research-cache/
# (fetch commands and sha256 pins: research/sources/README.md)
python3 -m research.build

# committed evidence only (no cache, no network) - what CI runs
python3 -m research.build --evidence-only

# rewrite the two generated reports from whatever is here
python3 -m research.build --reports-only
```

A full build refuses to run on a wrong-hash cache. The tracked reports
(`research/reports/n47-coverage.md`, `n47-conflicts.md`) are written by
the full build and are the committed, reviewable view of this data.

## Tests

`tests/research/test_provenance.py` reads these files when present and
skips — visibly, with a message naming this README — when they are
absent. The rest of `tests/research/` runs on the small excerpt fixtures
under `tests/research/fixtures/` (see the README there) and never needs
the cache.
