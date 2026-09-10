#!/usr/bin/env python3
"""
api_token.py - the bearer tokens for /api/odometer (docs/ODOMETER_API.md).

The store is one JSON file, gitignored, mode 0600:

    local/api-tokens.json
    {"tokens": [{"name": "android-nav",
                 "token": "<urlsafe, 32 random bytes>",
                 "created": "2026-09-10T12:00:00Z"}]}

live.py re-reads it whenever its mtime changes, so a mint or a revoke
takes effect on the running process without a restart. Names are unique;
minting an existing name refuses rather than silently replacing a token
someone is using.

    python3 tools/api_token.py mint android-nav      # prints the token ONCE
    python3 tools/api_token.py list                  # names and dates, never tokens
    python3 tools/api_token.py revoke android-nav
    python3 tools/api_token.py --file /elsewhere.json list

The token is printed by `mint` and never again: `list` shows names,
creation dates and a length, nothing that opens the endpoint.
"""

import argparse
import json
import os
import secrets
import stat
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List

DEFAULT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "local", "api-tokens.json",
)


def read_store(path: str) -> Dict[str, Any]:
    """The file's content, or an empty store when it does not exist."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return {"tokens": []}

    if not isinstance(doc, dict) or not isinstance(doc.get("tokens"), list):
        raise SystemExit(f"{path}: not a token store ({{'tokens': [...]}})")

    return doc


def write_store(path: str, doc: Dict[str, Any]) -> None:
    """
    Atomic replace at mode 0600.

    Written next to the target and renamed over it, so live.py never
    reads a half-written file; created 0600 from the first byte rather
    than chmod'ed afterwards, so there is no window in which the tokens
    are world-readable.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".api-tokens.", dir=directory)

    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)

        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")

        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass

        raise

    #: An existing file keeps whatever mode it had through os.replace
    #: (the new inode's mode wins), but say so explicitly for a file
    #: someone loosened by hand.
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def entries(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [e for e in doc["tokens"] if isinstance(e, dict)]


def mint(path: str, name: str) -> str:
    if not name or any(ch.isspace() for ch in name):
        raise SystemExit("a token name is one word")

    doc = read_store(path)

    if any(e.get("name") == name for e in entries(doc)):
        raise SystemExit(
            f"{name!r} already has a token - revoke it first "
            f"(`api_token.py revoke {name}`)"
        )

    token = secrets.token_urlsafe(32)
    doc["tokens"] = entries(doc) + [{
        "name": name,
        "token": token,
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }]
    write_store(path, doc)

    return token


def revoke(path: str, name: str) -> bool:
    doc = read_store(path)
    before = entries(doc)
    kept = [e for e in before if e.get("name") != name]

    if len(kept) == len(before):
        return False

    doc["tokens"] = kept
    write_store(path, doc)

    return True


def listing(path: str) -> List[Dict[str, Any]]:
    """Names, dates and token lengths - never the tokens."""
    return [
        {
            "name": e.get("name"),
            "created": e.get("created"),
            "length": len(str(e.get("token") or "")),
        }
        for e in entries(read_store(path))
    ]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--file", default=DEFAULT_FILE,
                    help=f"token store (default {DEFAULT_FILE})")
    sub = ap.add_subparsers(dest="command", required=True)
    m = sub.add_parser("mint", help="create a token for NAME and print it once")
    m.add_argument("name")
    sub.add_parser("list", help="names and creation dates (no tokens)")
    r = sub.add_parser("revoke", help="delete NAME's token")
    r.add_argument("name")
    args = ap.parse_args(argv)

    if args.command == "mint":
        token = mint(args.file, args.name)
        print(f"# token for {args.name!r} - shown once, stored in {args.file}",
              file=sys.stderr)
        print(token)
        return 0

    if args.command == "revoke":
        if revoke(args.file, args.name):
            print(f"revoked {args.name!r}")
            return 0

        print(f"no token named {args.name!r} in {args.file}", file=sys.stderr)
        return 1

    rows = listing(args.file)

    if not rows:
        print(f"no tokens in {args.file}")
        return 0

    for row in rows:
        print(f"{row['name']:24s} created {row['created']}  "
              f"({row['length']} chars)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
