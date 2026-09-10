#!/usr/bin/env python3
"""
Guard: a changed mapping file must have its `version` incremented.

The project identifies a recorded dataset by the mapping version stamped on
its samples (docs/DATA_VERSIONING.md). For that to mean anything, editing a
mapping file's content and forgetting to bump its version must be caught.
This checker compares each versioned data file against a git ref (default
HEAD): if the file's content changed - anything other than the version
itself - its version must be strictly greater than before.

It watches `mappings/**/*.yaml` and `config/modes.yaml` (the drive-mode
table, versioned data too: its `drive-modes@N` is part of every session's
mapping set). Code changes (loader, live.py, ...) never require a version
bump; the version tracks the data, not the program.

"Content" is what the loader sees: both sides are parsed with the
runtime's own YAML subset and compared with the version removed. So a
comment-only edit - a `#` line, a trailing `# ...` - is not a content
change and needs no bump (docs/DATA_VERSIONING.md: "bump for content, not
comments"), while a `#` inside a quoted string or a block scalar is
content, because the loader would see it. A side that fails to parse is
compared as text (minus the version line) so a broken file cannot slip
through as "unchanged".

Usage:
    python3 tools/check_mapping_versions.py            # vs HEAD
    python3 tools/check_mapping_versions.py --against origin/master

Exit status is 0 when every changed mapping was bumped, 1 otherwise. This
is a stdlib-only dev/CI tool - it is not imported by the runtime and needs
git on PATH.
"""

import argparse
import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bmwdiag.mapping.yamlsubset import loads  # noqa: E402  (stdlib-only package)

VERSION_RE = re.compile(r'^\s*version:\s*"?(\d+)"?\s*$', re.MULTILINE)

# The versioned data files. `git diff` is limited to these pathspecs and the
# result filtered again by is_watched(), so a stray .yaml elsewhere under
# config/ is not silently pulled in.
WATCHED_PATHSPECS = ("mappings", "config/modes.yaml")


def is_watched(path: str) -> bool:
    """A mapping file, or the drive-mode table."""
    if path == "config/modes.yaml":
        return True
    return path.startswith("mappings/") and path.endswith(".yaml")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def git_ok(*args: str):
    """Run git, returning stdout or None if the command failed."""
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def parse_version(text: str) -> int:
    """
    The version in `text` - `mapping.version` for a mapping file, top-level
    `version` for the mode table - or 0 if none (pre-versioning file).
    Read from the parsed document, so a trailing comment on the version
    line does not hide it; the regex is the fallback for text that does
    not parse.
    """
    try:
        doc = loads(text or "")
    except Exception:
        doc = None
    if isinstance(doc, dict):
        mapping = doc.get("mapping")
        raw = mapping.get("version") if isinstance(mapping, dict) else doc.get("version")
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
    m = VERSION_RE.search(text or "")
    return int(m.group(1)) if m else 0


def strip_version(text: str) -> str:
    """`text` with the version line removed, to compare the rest for change."""
    return VERSION_RE.sub("", text or "")


def content(text: str):
    """
    The file as the loader sees it, minus its version: the parsed document
    with `mapping.version` (mapping files) / top-level `version` (the mode
    table) removed. Comments are gone because the parser drops them; a `#`
    inside a quoted string or block scalar survives because it is data.

    If the text does not parse, fall back to the raw text minus the version
    line - the conservative side: a broken file compares as changed.
    """
    try:
        doc = loads(text or "")
    except Exception:
        return strip_version(text)
    if not isinstance(doc, dict):
        return doc
    doc = dict(doc)
    doc.pop("version", None)
    mapping = doc.get("mapping")
    if isinstance(mapping, dict):
        doc["mapping"] = {k: v for k, v in mapping.items() if k != "version"}
    return doc


def changed_mappings(ref: str):
    """Tracked watched files that differ from `ref` (working tree)."""
    out = git_ok("diff", "--name-only", ref, "--", *WATCHED_PATHSPECS) or ""
    files = [line for line in out.splitlines() if is_watched(line)]
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
    cosmetic = []                                        # differ in git, same to the loader

    for path in changed_mappings(args.against):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                new_text = fh.read()                     # working tree (what you have)
        except FileNotFoundError:
            continue                                     # deleted; nothing to bump
        old_text = git_ok("show", f"{args.against}:{path}")
        if old_text is None:
            continue                                     # newly added; version>=1 enforced by loader

        if content(new_text) == content(old_text):
            cosmetic.append(path)                        # only comments / the version (or nothing) changed
            continue

        checked += 1
        old_v, new_v = parse_version(old_text), parse_version(new_text)
        if new_v <= old_v:
            problems.append(
                f"  {path}: content changed but version did not increase "
                f"(was {old_v}, now {new_v}) - bump "
                + ("`version`" if path == "config/modes.yaml" else "`mapping.version`")
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
    for path in cosmetic:
        print(f"    {path}: comments/version only - no bump needed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
