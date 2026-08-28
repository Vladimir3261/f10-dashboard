#!/usr/bin/env python3
"""
Guard: a changed mapping file must have its `version` incremented.

The project identifies a recorded dataset by the mapping version stamped on
its samples (docs/DATA_VERSIONING.md). For that to mean anything, editing a
mapping file's content and forgetting to bump its version must be caught.
This checker compares each mapping file against a git ref (default HEAD):
if the file's content changed - anything other than the version line
itself - its version must be strictly greater than before.

It watches ONLY `mappings/**/*.yaml`. Code changes (loader, live.py, ...)
never require a version bump; the version tracks the mapping data, not the
program.

Usage:
    python3 tools/check_mapping_versions.py            # vs HEAD
    python3 tools/check_mapping_versions.py --against origin/master

Exit status is 0 when every changed mapping was bumped, 1 otherwise. This
is a stdlib-only dev/CI tool - it is not imported by the runtime and needs
git on PATH.
"""

import argparse
import re
import subprocess
import sys

VERSION_RE = re.compile(r'^\s*version:\s*"?(\d+)"?\s*$', re.MULTILINE)


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def git_ok(*args: str):
    """Run git, returning stdout or None if the command failed."""
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def parse_version(text: str) -> int:
    """The mapping.version in `text`, or 0 if none (pre-versioning file)."""
    m = VERSION_RE.search(text or "")
    return int(m.group(1)) if m else 0


def strip_version(text: str) -> str:
    """`text` with the version line removed, to compare the rest for change."""
    return VERSION_RE.sub("", text or "")


def changed_mappings(ref: str):
    """Tracked mappings/*.yaml that differ from `ref` (working tree)."""
    out = git_ok("diff", "--name-only", ref, "--", "mappings") or ""
    files = [
        line for line in out.splitlines()
        if line.startswith("mappings/") and line.endswith(".yaml")
    ]
    return sorted(set(files))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--against", default="HEAD",
                    help="git ref to compare against (default HEAD)")
    args = ap.parse_args(argv)

    try:
        git("rev-parse", "--is-inside-work-tree")
    except Exception:
        print("error: not a git repository", file=sys.stderr)
        return 2

    problems = []
    checked = 0

    for path in changed_mappings(args.against):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                new_text = fh.read()                     # working tree (what you have)
        except FileNotFoundError:
            continue                                     # deleted; nothing to bump
        old_text = git_ok("show", f"{args.against}:{path}")
        if old_text is None:
            continue                                     # newly added; version>=1 enforced by loader

        if strip_version(new_text) == strip_version(old_text):
            continue                                     # only the version line (or nothing) changed

        checked += 1
        old_v, new_v = parse_version(old_text), parse_version(new_text)
        if new_v <= old_v:
            problems.append(
                f"  {path}: content changed but version did not increase "
                f"(was {old_v}, now {new_v}) - bump `mapping.version`"
            )

    if problems:
        print(f"mapping version check FAILED (vs {args.against}):",
              file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        print("\nAfter bumping, regenerate the lock:\n"
              "  python3 -m bmwdiag.mapping lock mappings/", file=sys.stderr)
        return 1

    print(f"ok  mapping versions: {checked} changed file(s) properly bumped "
          f"(vs {args.against})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
