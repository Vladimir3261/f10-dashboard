# The published derivative blobs: history was rewritten (2026-09-10)

The record of an owner decision (issue #18). Prepared as a decision
document on 2026-09-05; the rewrite was carried out on **2026-09-10**.
Old hashes below are quoted only where the record needs them to say
what was rewritten, and are marked *old*.

## Why

Commit *old* `d0b9bb5` (now `d5845dd`, 2026-08-25, the second commit in
the repository) added the generated research output. The #18 branch
removed the four `.jsonl` files from the tracked tree and replaced the
coverage report with one that no longer reproduces the bulk set, but
every clone still carried the original blobs:

| Path (at *old* `d0b9bb5`) | Blob | Size |
|---|---|---|
| `research/normalized/n47/signals.jsonl` | `867133a8` | 2,120,546 B |
| `research/reports/n47-coverage.md` (the 1,693-row version) | `e209eaa8` | 205,287 B |
| `research/normalized/n47/jobs.jsonl` | `795bacd1` | 4,881 B |
| `research/normalized/n47/evidence.jsonl` | `5f185d03` | 1,846 B |
| `research/normalized/n47/requests.jsonl` | `78cca025` | 882 B |

`signals.jsonl` is the one that matters: 1,619 of its 1,645
`morguux-d73n47a0` rows are a mechanical derivative of the whole
`D73N47A0` SGBD export (licence unknown; database-right question on the
BMW side — `research/reports/legal-and-license-notes.md`, flag 1). The
old coverage report reproduced the same 1,619 rows as a table. The
three small files are our own evidence/job records and are harmless;
they went because the same command removes them.

Leaving the tree clean but the blobs one `git checkout` away does not
resolve the distribution question, it only moves it; and the cost of a
rewrite only grows with the first fork (there were **0 forks, 0 stars**
on 2026-09-05, and the only clones were the owner's laptop, the Pi and
the agent worktrees). So: rewrite, before the first fork. The
alternative — leave history and record the reason in the legal notes —
was considered and not taken.

## What was done (2026-09-10)

1. The four stale, already-merged remote branches were deleted first,
   so no ref kept the old commits reachable after the push.
2. On a **fresh mirror clone** (`git clone --mirror`; `filter-repo`
   refuses a working clone by design):

   ```bash
   git filter-repo --strip-blobs-with-ids /tmp/strip-blobs.txt
   ```

   with the five blob ids above in the file. Stripping by blob id, not
   by path, keeps the *current* small `n47-coverage.md` in history and
   removes only the 1,693-row version.
3. `refs/pull/*` were dropped from the mirror before pushing — GitHub
   owns those and refuses them on push.
4. `git push --force --all` (there are no tags).

Result, verified on the pushed history:

- **197 commits**; every commit after the first is re-hashed.
- `master`: *old* `a918f33` → **`b5ed094`**. The tree hash is
  unchanged: **`a0ddd7aa`** at both — the rewrite altered no file at
  HEAD.
- The five blobs are gone: `git rev-list --objects origin/master`
  lists none of `867133a886aefd540f49437ef50a28776deb2d31`,
  `e209eaa8df09edd1ea04643f2f215b7c5466af1a`,
  `795bacd1d395ec770705e2d6f849d1328e375b12`,
  `5f185d039f6bd302e9f51b2bc3e7a2c05ccf1b4c`,
  `78cca02524d1702bb0048941054fa0454fae6451` (checked 2026-09-10; a
  fresh clone never receives them, and a pre-rewrite clone drops them
  at its next `git gc`).
- The short commit hashes cited in this repository as provenance
  (`docs/`, `research/reports/`, `drive-sessions/*/NOTES.md`, two code
  comments) were remapped to the same-length prefix of the new hash
  from `filter-repo`'s old→new commit map, in the follow-up PR after
  the rewrite. Session links in commit trailers and PR numbers were
  unaffected; GitHub re-linked the PRs to the rewritten commits.

## What each machine must do, once

A clone of the old history cannot fast-forward to the new one. Do not
pull; reset:

- **Laptop:** `git fetch && git reset --hard origin/master` (stash or
  branch any local work first — a `reset --hard` discards it).
- **The Pi, `/opt/f10-dashboard`:** the same, by hand over SSH. The
  admin panel's *pull* is `git pull --ff-only` against the pinned
  remote and **refuses** the divergent history — that refusal is
  correct, not a fault. After the reset, `sudo systemctl restart
  f10-dashboard f10-admin` picks up the tree (identical content, so
  nothing changes at runtime).
- **Agent worktrees:** done on 2026-09-10 (`git checkout --detach
  origin/master`). A branch created before the rewrite must be
  re-based onto the new `master` (`git rebase --onto origin/master
  <old-base> <branch>`) or re-created before it can be merged. The one
  remote branch that survived, `fix/issue-11-structured-diagnostic-
  errors` (5 commits, no PR), already forks from the rewritten
  history.

## What is NOT a reason to rewrite

The other tracked research artifacts. `tests/research/fixtures/*` are
documented excerpts (19 of 1,645 CSV rows; one XML fragment; five
custom jobs — `tests/research/fixtures/README.md`), the
`research/evidence/n47/*` files are hand-transcribed protocol facts
with citations, and the current `n47-coverage.md` lists only the 26
gist rows that carry a normalized name. Those are the "individual
facts, cited" that the legal notes treat as fine to use. The
`bulk_redistribution: withheld` guard in the source manifest is what
stops the file from coming back.
