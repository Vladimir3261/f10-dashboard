# The published derivative blobs: rewrite history, or not

An owner decision, prepared here (issue #18). Nothing in this file has
been done; the numbers are measured on `origin/master` at 21ecfd0.

## What is in history

Commit `d0b9bb5` (2026-08-25, the second commit in the repository)
added the generated research output. The #18 branch removes the four
`.jsonl` files from the tracked tree and replaces the coverage report
with one that no longer reproduces the bulk set, but every clone still
carries the original blobs:

| Path (at `d0b9bb5`) | Blob | Size | gzip -9 |
|---|---|---|---|
| `research/normalized/n47/signals.jsonl` | `867133a8` | 2,120,546 B | ~89 KB |
| `research/reports/n47-coverage.md` (the 1,693-row version) | `e209eaa8` | 205,287 B | ~15.5 KB |
| `research/normalized/n47/jobs.jsonl` | `795bacd1` | 4,881 B | — |
| `research/normalized/n47/evidence.jsonl` | `5f185d03` | 1,846 B | — |
| `research/normalized/n47/requests.jsonl` | `78cca025` | 882 B | — |

Each was written once and never modified, so there is exactly one blob
per path. `signals.jsonl` is the one that matters: 1,619 of its 1,645
`morguux-d73n47a0` rows are a mechanical derivative of the whole
`D73N47A0` SGBD export (licence unknown; database-right question on the
BMW side — `research/reports/legal-and-license-notes.md`, flag 1). The
old coverage report reproduced the same 1,619 rows as a table. The
three small files are our own evidence/job records and are harmless;
they are listed because the same command removes them.

Repository facts that bear on the decision:

- 172 commits on `master`; `d0b9bb5` is the second, so a rewrite
  changes the hash of **170 commits** (every one after it).
- Pack size 1.39 MiB; the two blobs are ~105 KB compressed. Size is
  not the reason to do this — the content is.
- The repository is public on GitHub with **0 forks and 0 stars**
  (checked 2026-09-05). The only known clones are the owner's laptop,
  the Pi (`/opt/f10-dashboard`, pulled ff-only by the admin panel) and
  the agent worktrees.
- Open PR branches at the time of writing: `fix/issue-16-…` (#36) and
  `fix/issue-18-…`. Every branch carries `d0b9bb5` in its ancestry.

## Option A — leave history as it is

The tracked tree no longer redistributes the bulk set; the blobs are
reachable only by someone who checks out an old commit or asks for the
object by hash. On GitHub the data stays visible at
`…/blob/d0b9bb5/research/normalized/n47/signals.jsonl` for as long as
the commit is in any branch.

- Cost: none. No re-clone, no force-push, no re-pin.
- Risk: the thing the legal notes flagged is still published, just not
  at `HEAD`. If the question is "did you distribute it", the answer
  stays yes.

## Option B — rewrite history with `git filter-repo`

Strip the five blobs from every commit that carries them.
`git filter-repo` (not `filter-branch`) is the tool GitHub documents;
it is a single Python file, installed with `pip install
git-filter-repo` or from the distro. Stripping by blob id rather than
by path keeps the *current*, small `n47-coverage.md` (written by this
PR) in history and removes only the 1,693-row version.

```bash
# on a FRESH mirror clone - filter-repo refuses to run on a clone with
# uncommitted state or a configured origin, by design
git clone --mirror git@github.com:Vladimir3261/f10-dashboard.git f10-rewrite.git
cd f10-rewrite.git

cat > /tmp/strip-blobs.txt <<'IDS'
867133a886aefd540f49437ef50a28776deb2d31
e209eaa8df09edd1ea04643f2f215b7c5466af1a
795bacd1d395ec770705e2d6f849d1328e375b12
5f185d039f6bd302e9f51b2bc3e7a2c05ccf1b4c
78cca02524d1702bb0048941054fa0454fae6451
IDS

git filter-repo --strip-blobs-with-ids /tmp/strip-blobs.txt

# sanity: the big blob must be unreachable
git cat-file -e 867133a886aefd540f49437ef50a28776deb2d31 && echo "STILL THERE" || echo "gone"

git push --force --mirror
```

(The path-based equivalent, `git filter-repo --invert-paths --path
research/normalized/n47/signals.jsonl …`, also works but removes the
coverage report from every commit, including the current one, so the
blob-id form is the right one here.)

Then, on GitHub: the old commits stay in the server's object store
until it is garbage-collected. To make `…/blob/d0b9bb5/…` a 404 and
drop the objects from the cache, open a support request ("remove
cached views and references to sensitive data") and quote the old
commit SHAs — GitHub's documented procedure for exactly this, and the
only way to make the dereferenced objects unreachable by hash.

Consequences, in order of pain:

1. **Every clone must be re-cloned**, not pulled. The Pi's admin panel
   does `git pull --ff-only` against a pinned remote and will refuse
   the divergent history; the Pi needs `git fetch && git reset --hard
   origin/master` (or a fresh clone into `/opt/f10-dashboard`) once,
   by hand over SSH. The laptop and every agent worktree, the same.
2. **Every open PR branch must be rebased onto the rewritten master**
   (`git rebase --onto origin/master <old-base> <branch>`) and
   force-pushed, or GitHub shows every commit since `d0b9bb5` as part
   of the PR. Merge the open PRs first if possible, rewrite, then
   re-branch.
3. **Every commit hash cited in this repository changes.** The
   `validation-runs/*/` artifacts, `drive-sessions/*/`, the
   `research/reports/*` narratives, CLAUDE.md and the docs quote
   short hashes (`21ecfd0`, `d0b9bb5`, `e8496e0`, …) as provenance;
   after a rewrite those point at nothing. They stay readable as
   history (the messages survive), but `git show <hash>` stops
   working. A one-off sed over the tree with the old→new map
   `filter-repo` writes (`.git/filter-repo/commit-map`) fixes the ones
   that matter.
4. **Forks and clones you do not control keep the old history.** With
   0 forks today this is the cheapest it will ever be; every fork made
   from now on carries the blobs until it, too, is rewritten.
5. Session links in commit trailers (`Claude-Session: …`) and the PR
   numbers are unaffected; GitHub re-links PRs to the rewritten
   commits by branch name after the force-push.

## Recommendation

**Do B, now, before the first fork.** The reason the file was removed
from the tree is that redistributing it is a risk the legal notes
called out before the repository had a single commit; leaving the same
bytes one `git checkout d0b9bb5` away does not resolve that, it only
moves it. The cost is one re-clone on three machines and a rebase of
whichever PR branches are still open, and the cost only grows: the
next fork is the moment option B stops being complete.

If the owner prefers A, record the reason in the legal notes (flag 1)
so the next reader does not re-open the question, and keep the
`bulk_redistribution: withheld` guard — it is what stops the file from
coming back.

## What is NOT a reason to rewrite

The other tracked research artifacts. `tests/research/fixtures/*` are
documented excerpts (19 of 1,645 CSV rows; one XML fragment; five
custom jobs — `tests/research/fixtures/README.md`), the
`research/evidence/n47/*` files are hand-transcribed protocol facts
with citations, and the current `n47-coverage.md` lists only the 26
gist rows that carry a normalized name. Those are the "individual
facts, cited" that the legal notes treat as fine to use.
