# Mapping data versioning

Every recorded sample can be tied back to the exact mapping revision that
decoded it, so a dataset analysed months later is the *same* dataset, not
one silently re-interpreted by a mapping edit in between.

## The rule

- Every mapping file declares an integer **`version`** under `mapping:`,
  starting at **1**.
- **Increment it by one on every change to that file's content** — a
  decode, a scale, a DID, an added or removed signal, even a comment. The
  file is the versioned unit.
- The version tracks the **mapping data, never the code.** Editing
  `loader.py`, `live.py`, or anything else in the program does *not* change
  any mapping version. Only editing a file under `mappings/` does.
- Versions only ever go **up**. There is no v0; a brand-new mapping starts
  at 1.

```yaml
mapping:
  id: candidate-n47-d72-flow
  version: 1          # bump to 2 the next time this file's content changes
  production: false
```

The loader **requires** `version` (a positive integer); a mapping without
one fails to load. A quoted digit (`version: "1"`) is accepted and coerced
to the integer `1`, so the historical production file stays byte-identical.

## Where the version is stamped

A channel inherits the version of the mapping file it comes from (a read
signal from its request's file; a derived channel from its defining file).
That version travels with the data:

| store | what carries the version |
|---|---|
| SQLite `params.mapping_ver` | per-channel version (as text, `""` if unknown) |
| SQLite `run_mappings` | one row per loaded file per run: `mapping_id, version, production, source_path` — the authoritative "what decoded this run" record |
| SQLite `runs.mapping_set` | compact fingerprint `id@version,…` (sorted) for the whole run |
| ClickHouse `samples.mapping_ver` | per-sample version, filled by the ingest server from the client's per-channel value |
| ClickHouse `sessions.mappings` | the `id@version,…` fingerprint for the session |

So in the lake you can filter or group by `mapping_ver`, and know from
`sessions.mappings` exactly which revision of every mapping produced a
drive.

## Changing a mapping — the workflow

1. Edit the mapping file.
2. **Bump its `version`** (e.g. `1` → `2`).
3. Regenerate the lockfile:
   ```bash
   python3 -m bmwdiag.mapping lock mappings/
   ```
4. Commit the mapping change **and** the updated `mappings/VERSIONS.lock`
   together.

Two guards keep this honest:

- **`mappings/VERSIONS.lock`** — a committed ledger of every mapping's
  `id → version → path`. A unit test
  (`tests/test_mapping_versioning.py::LockfileEnforcement`) fails if the
  lock and the files on disk disagree, so a version change can't land
  without the lock — and therefore the change — being visible in review.
  Check it any time with:
  ```bash
  python3 -m bmwdiag.mapping lock mappings/ --check
  ```
- **`tools/check_mapping_versions.py`** — a git-diff guard: if a mapping
  file's content changed versus a ref (default `HEAD`) without its version
  increasing, it fails. Run it locally or in CI:
  ```bash
  python3 tools/check_mapping_versions.py                 # vs HEAD
  python3 tools/check_mapping_versions.py --against origin/master
  ```

## Why integer versions, and not a content hash

The dataset identifier is a small, human-readable number you can reason
about ("this drive used flow v2, before the boost-datum fix in v3"). A
content hash was considered and deliberately not used: the version is the
identifier stamped on the data, and the two guards above provide the
"did you forget to bump?" safety a hash would otherwise give — without
opaque identifiers in the data model.

The trade-off: the lock test and the git checker catch a forgotten bump
in review/CI, but nothing *inside* a single file at rest proves its
content matches its version. Keep the discipline: content change ⇒ bump.

## Existing data

Rows recorded before versioning landed keep `mapping_ver = ''` /
`mappings = ''`; their exact mapping revision is not recoverable and is
left blank rather than guessed. Everything recorded from now on is
stamped. Applying the lake column to an already-deployed ClickHouse is a
one-time migration — see
`infra/clickhouse/migrations/2026-08-29_mapping_versioning.sql`.
