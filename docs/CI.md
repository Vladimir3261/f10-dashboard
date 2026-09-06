# CI

`.github/workflows/ci.yml` runs on every push to `master` and on every
pull request, on Python 3.12 (the development host) and 3.13 (the Pi
runs 3.13.5 — `docs/PI_COMMISSIONING.md`). It needs no secret, no
network beyond the checkout, no car and no research source cache; a
run takes about a minute. Nothing is cached between runs.

## The steps, and why each exists

| Step | Command | Why |
|---|---|---|
| Hygiene | `python3 tools/check_hygiene.py` | The repository is public. This refuses a tracked path under `local/`, a tracked `.env` or private `config.json`, a tracked database, a 17-character BMW VIN, or a token value (`TOKEN=…`, JSON `"token"`, `dop_v1_…`, literal `Bearer …`) anywhere in a tracked text file. It prints the path and line number only, never the matched text — the log is public too. A VIN-shaped test string is allowed on a line marked `# hygiene: fake-vin`, so the exception is visible in review. |
| Byte-compile | `python3 -m compileall -q .` | Every file parses on both interpreters. Cheap, and catches a syntax error in a script no test imports (the Pi scripts, `tools/`). |
| Mapping validation | `python3 -m bmwdiag.mapping validate mappings/` | Every mapping file — production and candidates — loads through the same loader the runtime uses. A mapping that does not load is a channel that silently disappears in the car. |
| Lock check | `python3 -m bmwdiag.mapping lock --check mappings/` | `mappings/VERSIONS.lock` matches the `mapping.version` of every file on disk. The version is stamped on every recorded sample, so it must be right at commit time, not discovered later. |
| Version bump guard (PRs only) | `git fetch origin $BASE && python3 tools/check_mapping_versions.py --against origin/$BASE` | A mapping whose *content* changed relative to the PR base must have incremented its version; otherwise two different decodes would share one version number in the lake. Needs `fetch-depth: 0`. Runs only on pull requests — on a push to `master` there is no base to compare against. |
| Research pipeline | `python3 -m research.build --evidence-only` | The importers, model validation, candidate gate and conflict detection run on the committed evidence files alone. Writes only the gitignored normalized output; does not rewrite the tracked reports (those cover the cached sources too and would otherwise be silently truncated). Proves the pipeline is deterministic and self-contained without the licence-withheld source cache. |
| Test suite | `python3 tools/run_tests.py` | Same discovery as `python3 -m unittest discover`, same exit status, plus every skipped test is printed with its reason and the last line is `tests=N failures=… skipped=…`. The suite must pass with no car, no network and no BMW data; the handful of tests that need the full research output skip, visibly. A green run with a hidden skip count would be proof of nothing. |

The order is cheapest-first so a hygiene or syntax failure is reported
in seconds. The steps are independent; a failure in one does not stop
the rest of the matrix (`fail-fast: false`) because "3.13 fails, 3.12
passes" is information.

## Running the same thing locally

```
python3 tools/check_hygiene.py
python3 -m compileall -q .
python3 -m bmwdiag.mapping validate mappings/
python3 -m bmwdiag.mapping lock --check mappings/
python3 tools/check_mapping_versions.py --against origin/master
python3 -m research.build --evidence-only
python3 tools/run_tests.py
```

If the gitignored source cache is present under `local/research-cache/`
the suite also runs the three cache-dependent tests; run
`python3 -m research.build` (full) beforehand, or the provenance tests
will see an `--evidence-only` build and skip with a message saying so.
Either way the skip count on the last line is the number to look at.

## Branch protection (owner action; not done by any PR)

Repository settings are not changed from a branch. To require both
matrix legs on `master` and stop a direct push from bypassing them,
the owner runs, from a checkout with `gh` authenticated as the owner:

```bash
cat > /tmp/protection.json <<'EOF'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["checks (py3.12)", "checks (py3.13)"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF

gh api -X PUT repos/Vladimir3261/f10-dashboard/branches/master/protection \
    --input /tmp/protection.json
```

`strict: true` means a PR must be up to date with `master` before it
can merge, which is what makes the version-bump guard's diff honest.
`enforce_admins: true` applies the rule to the owner too; drop it if
the owner wants to keep a direct-push escape hatch (the agents never
push to `master` either way). `required_pull_request_reviews: null`
keeps review optional — a single-owner repository cannot require a
second reviewer without blocking itself. The status-check names are the
job names from the workflow; they must exist on at least one run before
GitHub will accept them, so run the workflow once (this PR's merge, or
any push to `master`) first.

To check what is set:

```bash
gh api repos/Vladimir3261/f10-dashboard/branches/master/protection
```

(404 means no protection, which is the state at the time of writing.)

If the history rewrite in `docs/HISTORY_REWRITE.md` is done, do it
**before** enabling protection: `allow_force_pushes: false` blocks the
`--mirror` push, and temporarily disabling protection is one more step
to forget.
