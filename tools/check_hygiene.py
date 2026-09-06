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
* a 17-character VIN anywhere in a tracked text file - the car is
  referred to by its label `F10-520d-dev`, never by VIN. Three nets,
  any one is a hit: a BMW/MINI WMI (`WBA`/`WBS`/`WBY`/`WBX`/`WMW`/
  `5UX`) + 14 characters from the VIN alphabet; a prefix-independent
  shape (11 VIN-alphabet characters with at least one letter, then a
  6-digit serial, the way European BMW VINs are laid out); or any
  17-character VIN-alphabet token whose ISO 3779 check digit (position
  9) validates, which catches North-American-format VINs of any make.
  A test that needs a VIN-shaped string marks the line
  `# hygiene: fake-vin`; the marker is visible in review, which is the
  point;
* a token value in a tracked file: `...TOKEN=value` or a JSON
  `"token": "value"` whose value is at least 16 characters and not a
  placeholder (`change-me`, `<...>`, `${...}`, `{...}`, `os.environ`,
  ...), a DigitalOcean `dop_v1_...`, or a literal `Bearer <long-token>`.
  `*.example` / `*.template` files are exempt from the first two - they
  are the committed templates.

A hit prints the path and the line number only - never the matched
text, because the whole point is that the text must not appear in a
log either. Binary files are not scanned; a text file over 4 MB is not
scanned either, and says so (`skipped (size ...)`) so the log shows
what was left out. Exit status 1 on any hit, 0 when clean. Stdlib
only; needs git on PATH when run without arguments.
"""

import os
import re
import subprocess
import sys
from typing import Iterable, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Net 1: BMW / MINI world-manufacturer identifiers.
VIN = re.compile(r"\b(?:WBA|WBS|WBY|WBX|WMW|5UX)[A-HJ-NPR-Z0-9]{14}\b")
#: Net 2: prefix-independent shape - 11 VIN-alphabet characters holding
#: at least one letter (so a 17-digit number is not a VIN), then a
#: 6-digit serial.
VIN_SHAPE = re.compile(
    r"\b(?=[A-HJ-NPR-Z0-9]{0,10}[A-HJ-NPR-Z])[A-HJ-NPR-Z0-9]{11}[0-9]{6}\b"
)
#: Net 3: any 17-character VIN-alphabet token, checked against the ISO
#: 3779 check digit at position 9 (North-American format; European BMW
#: VINs carry no check digit, which is what nets 1 and 2 are for).
VIN_ANY = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
FAKE_VIN_MARK = "hygiene: fake-vin"

_VIN_VALUES = {c: v for c, v in zip("ABCDEFGHJKLMNPRSTUVWXYZ",
                                    (1, 2, 3, 4, 5, 6, 7, 8, 1, 2, 3, 4, 5,
                                     7, 9, 2, 3, 4, 5, 6, 7, 8, 9))}
_VIN_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)


def vin_check_digit_valid(token: str) -> bool:
    """ISO 3779 / FMVSS 115 check digit (position 9) for a 17-char token."""
    if len(token) != 17:
        return False

    total = 0

    for char, weight in zip(token, _VIN_WEIGHTS):
        total += (int(char) if char.isdigit() else _VIN_VALUES.get(char, 0)) * weight

    expected = "X" if total % 11 == 10 else str(total % 11)
    return token[8] == expected


def vin_hit(line: str) -> str:
    """Which VIN net fires on this line ("" when none)."""
    if VIN.search(line):
        return "17-character VIN"

    if VIN_SHAPE.search(line):
        return "17-character VIN (shape)"

    if any(vin_check_digit_valid(m) for m in VIN_ANY.findall(line)):
        return "17-character VIN (check digit)"

    return ""

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

    if not _is_text(data):
        return hits

    if len(data) > TEXT_MAX_BYTES:
        print(f"hygiene: {path}: skipped (size {len(data)} B > {TEXT_MAX_BYTES} B)")
        return hits

    example = ".example" in path or path.endswith(".template")
    is_this_tool = path == "tools/check_hygiene.py"
    text = data.decode("utf-8", errors="replace")

    for number, line in enumerate(text.splitlines(), 1):
        if FAKE_VIN_MARK not in line:
            what = vin_hit(line)

            if what:
                hits.append((number, what))

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
