# Contributing

This is a personal tool for one car (`CLAUDE.md` says why). It is
public so the method can be read, not because it is looking for users.
Contributions are welcome when they fit that: a defect with a
reproduction, a decode cross-checked against something, a doc that was
wrong. Read `CLAUDE.md` first — its "load-bearing principles" are the
review criteria.

## Licence

The code is under the MIT licence in `LICENSE`; a contribution is
offered under the same terms. The *data* in the repository is a
separate question from the code — `LICENSE-DATA.md`,
`THIRD_PARTY.md` and `research/reports/legal-and-license-notes.md` say
which facts came from where and under what terms; `LICENSE` does not
relicense any of it.

## Before opening a pull request

```
python3 tools/run_tests.py                        # the suite; prints the skip count
python3 -m bmwdiag.mapping validate mappings/     # every mapping file loads
python3 -m bmwdiag.mapping lock --check mappings/ # versions match the lock
python3 tools/check_mapping_versions.py --against origin/master
python3 -m research.build --evidence-only         # committed evidence still imports
python3 tools/check_hygiene.py                    # no VIN, no token, no local/
```

That is what CI runs (`docs/CI.md`). Tests must pass with no car, no
network and no BMW data on the machine; a test that needs the research
source cache skips with a message that says so, and the skip count is
printed.

## What a PR is measured against

- **Read-only.** Nothing that sends a write, actuator or coding
  service, in any code path, tools included. The service allowlist in
  `tools/validate_candidate.py` is the choke point; do not widen it.
- **No proprietary tool as a runtime dependency**, and no third-party
  package at all in `bmwdiag/`, `live.py` or the Pi runtime — they are
  stdlib-only on purpose.
- **Mapping changes are data changes**: bump `mapping.version`,
  regenerate `mappings/VERSIONS.lock`, give the provenance
  (`CONTRIBUTING_DATA.md`). A mapping without a written source is not
  merged.
- **Nothing private.** No VIN (the car is `F10-520d-dev`), no
  `local/`, no `.env`, no sync token, no raw capture.
  `tools/check_hygiene.py` refuses the obvious shapes; it is a
  tripwire, not a guarantee.
- **Say what you measured.** A PR that changes polling, alignment or
  decoding states whether its numbers are synthetic or from the car,
  and what they were cross-checked against. Do not change an alignment
  tolerance to make a scheduler look good.
- **Current master over stale prose.** If an issue or a doc describes
  code that no longer looks like that, say so in the PR and solve the
  actual problem.

## Mechanics

- One issue, one branch off `master`, one PR. The owner merges.
- Commit messages: a sentence that says what changed and why, a body
  when the why is not obvious. Agent-authored commits carry a
  `Co-Authored-By:` trailer.
- Do not rewrite `master` history. `docs/HISTORY_REWRITE.md` is the
  one pending exception, and it is the owner's call.
- On-car runs produce a tracked, VIN-redacted artifact under
  `validation-runs/`; the raw copy stays in gitignored `local/`.

## Not wanted

Support for other cars, multi-tenant anything, packaging for
distribution, a plugin system, or "support" in the product sense. The
scope line in `CLAUDE.md` is deliberate.
