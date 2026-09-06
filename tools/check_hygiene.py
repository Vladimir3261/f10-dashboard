#!/usr/bin/env python3
"""
Public-repo hygiene guard: nothing private in the tracked tree.

    python3 tools/check_hygiene.py            # every tracked file
    python3 tools/check_hygiene.py PATH...    # just these files

What it refuses (docs/CI.md has the reasoning):

* a tracked path under `local/` (gitignored scratch: VIN table, raw
  captures, telemetry databases, the research source cache);
* a tracked `.env` or `.env.*` other than `*.example`, a tracked
  `infra/sync/config.json` or admin `config.json`, a tracked `*.db`;
* a 17-character BMW/MINI VIN (`WBA`/`WBS`/`WBY`/`WMW` + 14 characters
  from the VIN alphabet) anywhere in a tracked text file - the car is
  referred to by its label `F10-520d-dev`, never by VIN. A test that
  needs a VIN-shaped string marks the line `# hygiene: fake-vin`; the
  marker is visible in review, which is the point;
* a token value in a tracked file: `...TOKEN=value` or a JSON
  `"token": "value"` whose value is at least 16 characters and not a
  placeholder (`change-me`, `<...>`, `${...}`, `{...}`, `os.environ`,
  ...), a DigitalOcean `dop_v1_...`, or a literal `Bearer <long-token>`.
  `*.example` / `*.template` files are exempt from the first two - they
  are the committed templates.

A hit prints the path and the line number only - never the matched
text, because the whole point is that the text must not appear in a
log either. Exit status 1 on any hit, 0 when clean. Stdlib only; needs
git on PATH when run without arguments.
"""

import os
import re
import subprocess
import sys
from typing import Iterable, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VIN = re.compile(r"\b(?:WBA|WBS|WBY|WMW)[A-HJ-NPR-Z0-9]{14}\b")
FAKE_VIN_MARK = "hygiene: fake-vin"

#: Token-shaped content. `*.example` / `*.template` files are exempt from
#: the first two rules - the committed templates carry placeholders.
TOKEN_ENV = re.compile(r"^\s*[A-Z0-9_]*TOKEN\s*=\s*(.+?)\s*$")
TOKEN_JSON = re.compile(r'"(?:token|api_token|auth_token)"\s*:\s*"([^"]+)"')
TOKEN_DO = re.compile(r"\bdop_v1_[0-9a-f]{64}\b")
TOKEN_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{24,}")

#: A real token is long and random. Anything shorter than this is a test
#: value; anything matching PLACEHOLDER is a template or code reading the
#: value from somewhere else.
TOKEN_MIN_LEN = 16
PLACEHOLDER = re.compile(
    r"^(change-me|<|\$\{|\$[A-Z_]+$|\{|\(|os\.|xxx|your-|the-same-long-random)",
    re.I,
)

TEXT_MAX_BYTES = 4 * 1024 * 1024


def _token_value_is_real(value: str) -> bool:
    value = value.strip().strip("\"'")
    return len(value) >= TOKEN_MIN_LEN and not PLACEHOLDER.match(value)


def tracked_files() -> List[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True,
        stdout=subprocess.PIPE,
    ).stdout
    return [p.decode("utf-8") for p in out.split(b"\0") if p]


def path_problems(path: str) -> List[str]:
    problems = []
    parts = path.split("/")
    name = parts[-1]

    if "local" in parts[:-1]:
        problems.append("tracked path under local/")

    if (name == ".env" or name.startswith(".env.")) and ".example" not in name:
        problems.append("tracked .env file")

    if path in ("infra/sync/config.json", "hardware/raspberry-pi/admin/config.json"):
        problems.append("tracked private config.json")

    if name.endswith((".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm")):
        problems.append("tracked database file")

    return problems


def _is_text(data: bytes) -> bool:
    return b"\0" not in data[:8192]


def content_problems(path: str, data: bytes) -> List[Tuple[int, str]]:
    """(line number, what) - the matched text is never returned."""
    hits: List[Tuple[int, str]] = []

    if not _is_text(data) or len(data) > TEXT_MAX_BYTES:
        return hits

    example = ".example" in path or path.endswith(".template")
    is_this_tool = path == "tools/check_hygiene.py"
    text = data.decode("utf-8", errors="replace")

    for number, line in enumerate(text.splitlines(), 1):
        if VIN.search(line) and FAKE_VIN_MARK not in line:
            hits.append((number, "17-character VIN"))

        if is_this_tool:
            continue

        if TOKEN_DO.search(line):
            hits.append((number, "DigitalOcean token"))

        if TOKEN_BEARER.search(line):
            hits.append((number, "literal Bearer token"))

        if not example:
            match = TOKEN_ENV.match(line)

            if match and _token_value_is_real(match.group(1)):
                hits.append((number, "TOKEN= with a value"))

            match = TOKEN_JSON.search(line)

            if match and _token_value_is_real(match.group(1)):
                hits.append((number, "JSON token with a value"))

    return hits


def check(paths: Iterable[str]) -> int:
    failures = 0

    for path in paths:
        for problem in path_problems(path):
            print(f"HYGIENE {path}: {problem}")
            failures += 1

        full = os.path.join(ROOT, path)

        if not os.path.isfile(full):
            continue

        with open(full, "rb") as handle:
            data = handle.read()

        for number, what in content_problems(path, data):
            print(f"HYGIENE {path}:{number}: {what}")
            failures += 1

    return failures


def main(argv: List[str]) -> int:
    paths = argv if argv else tracked_files()
    failures = check(paths)

    if failures:
        print(f"hygiene: {failures} problem(s) in the tracked tree")
        return 1

    print(f"hygiene: {len(paths)} tracked file(s) clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
