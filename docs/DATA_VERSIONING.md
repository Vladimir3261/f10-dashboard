# Data versioning

Every recorded sample can be tied back to the exact revision of the data
that produced it, so a dataset analysed months later is the *same*
dataset, not one silently re-interpreted by an edit in between.

Two kinds of file are versioned this way, in one ledger:

- **mapping files** (`mappings/**.yaml`) — what a channel is and how its
  bytes decode.
- **the drive-mode table** (`config/modes.yaml`) — how hard the car was
  being polled. Not a mapping, but versioned data that changes what a
  recorded drive means. See [`POLLING_AND_SAFETY.md`](POLLING_AND_SAFETY.md).

## The rule

- Every mapping file declares an integer **`version`** under `mapping:`,
  starting at **1**; `config/modes.yaml` declares one at the top level.
- **Increment it by one on every change to that file's content** — a
  decode, a scale, a DID, an added or removed signal, a mode's
  multiplier. The file is the versioned unit.
- **Not for comments or prose.** A comment does not alter what the file
  produces, and the version is stamped on every sample: bumping it for a
  prose edit falsely signals a data change and splits a dataset that
  never changed.
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
| SQLite `run_channels` | **per-run, per-channel provenance**: `mapping_id`, `mapping_version`, plus the label and unit in force for that run. This is the authoritative per-channel answer |
| SQLite `params.mapping_ver` | the version seen the **first** time this database ever recorded the channel. Kept for databases written before `run_channels`, and never updated in place — see below |
| SQLite `run_mappings` | one row per loaded file per run: `mapping_id, version, production, source_path` — the authoritative "what decoded this run" record |
| SQLite `runs.mapping_set` | compact fingerprint `id@version,…` (sorted) for the whole run, **including `drive-modes@N`** |
| ClickHouse `samples.mapping_ver` | per-sample version, filled by the ingest server from the client's per-channel value |
| ClickHouse `sessions.mappings` | the `id@version,…` fingerprint for the session |

So in the lake you can filter or group by `mapping_ver`, and know from
`sessions.mappings` exactly which revision of every mapping produced a
drive.

### Why provenance is scoped to the run

`params` is channel *identity* and is written once, on first sight, with
`INSERT OR IGNORE`. That makes `params.mapping_ver` a fact about the first
run that ever saw the channel — which stops being true as soon as a
mapping is revised and the same database is reused, as `telemetry.db` is
for months. New samples would keep shipping under the old version while
being decoded by the new one.

Updating that row in place would be worse, not better: every historical
sample would then claim it was decoded by a revision that did not exist
when it was recorded. Provenance has to be **immutable once written**.

So each run records one `run_channels` row per channel, and a sample
resolves its version through `(run_id, param_id)`. A run has exactly one
mapping configuration, so one row per channel per run is enough — the
version does not need repeating on all 100k samples. Derived channels go
through the same path and get the same guarantee.

The sync agent resolves through `run_channels` and falls back to
`params.mapping_ver` only when the **row** is missing, which means the
sample predates the table. An empty `mapping_version` is not a fallback:
the row exists, so that run's answer is known, and the answer is
"unknown". `runs.mapping_set` remains the whole-run fingerprint — it says
what was loaded, but cannot say which file owned one particular channel.

That fallback is the **best available legacy provenance, not a
guarantee**. A database that had already crossed a mapping revision
*before* `run_channels` existed may already hold a stale
`params.mapping_ver` — that is the original bug, and once it has happened
there is nothing left to reconstruct the true version from. The value is
reported because it is the only evidence there is, not because it is
known to be correct.

### The drive mode is text; its version is in the fingerprint

`sessions.mode` holds the mode **name** as plain text (`long`), because
that is what you actually want to read, group by and filter on. Which
*revision* of the mode table that name refers to lives in the same
`sessions.mappings` string as everything else:

```
mode      = "long"
mappings  = "candidate-f10-egs-transmission@4,drive-modes@1,sae-obd-engine@2"
```

Two reasons it is there rather than in a column of its own:

1. **One equality check** on `mappings` answers "were these two drives
   recorded the same way?" — no second column to remember to join on.
2. The mode table is **owned by no mapping file**, so folding its version
   into theirs would mean bumping ten files for one mode edit, falsely
   signalling that ten decode definitions changed and splitting
   per-channel datasets that did not.

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

- **`mappings/VERSIONS.lock`** — a committed ledger of every mapping's (and the mode table's)
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
stamped.

Databases written before `run_channels` gain the table simply by being
opened — it is a `CREATE TABLE IF NOT EXISTS`, so the migration is
idempotent — but their existing runs are deliberately **not** back-filled.
The only version those rows could be given is today's, which is exactly
the retroactive relabelling the table exists to prevent. They keep
resolving through `params.mapping_ver`, with the caveat above: best
available, not guaranteed. Applying the lake column to an already-deployed ClickHouse is a
one-time migration — see
`infra/clickhouse/migrations/2026-08-29_mapping_versioning.sql`.
