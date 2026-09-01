"""
Durable identity for a recording session.

A run's identity used to be derived from its file: CRC32 of the SQLite
basename, shifted, with the local run id in the low bits. That is
deterministic, which is what made it look sufficient, but it is not
identity - it is a function of where the data happens to be stored:

  * renaming `telemetry.db` to `telemetry-old.db` changes the identity of
    every run it contains, so a re-sync duplicates the whole history;
  * two drive files that happen to share a basename - which the per-drive
    naming makes likely, not exotic - collide outright, and the lake
    silently merges two different drives into one session;
  * CRC32 is 32 bits, so collisions are possible even without that;
  * copying a file to a new name mints new identities for old data.

So identity is minted once, when the run is created, and travels with it.
A ULID rather than a UUID4: it is lexicographically sortable by creation
time, which makes a directory listing or an ORDER BY useful, and it is
still 80 bits of randomness. No dependencies - this is 30 lines.

The numeric `session_id` the lake uses as a join key is DERIVED from the
ULID rather than from the filename, and only for runs that have one.
Runs recorded before this existed keep the legacy filename derivation,
because changing their id would duplicate every session already in the
lake. That is the same rule as everywhere else here: history keeps the
identity it was written with.
"""

import os
import time
import zlib

__all__ = ["new_ulid", "is_ulid", "session_id_from_ulid", "ULID_LENGTH"]

#: Crockford base32: no I, L, O or U, so a ULID cannot be misread aloud
#: or mistyped into a different valid id.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

ULID_LENGTH = 26


def _encode(value: int, length: int) -> str:
    out = []

    for _ in range(length):
        value, rem = divmod(value, 32)
        out.append(_ALPHABET[rem])

    return "".join(reversed(out))


def new_ulid(now_ms: int = None, randomness: bytes = None) -> str:
    """
    A fresh ULID: 48-bit millisecond timestamp then 80 random bits.

    `now_ms` and `randomness` are injectable so a test can pin the value;
    nothing in production passes them.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    if randomness is None:
        randomness = os.urandom(10)

    return (
        _encode(now_ms & ((1 << 48) - 1), 10)
        + _encode(int.from_bytes(randomness, "big"), 16)
    )


def is_ulid(text: str) -> bool:
    """Whether `text` is shaped like a ULID this module would mint."""
    if not text or len(text) != ULID_LENGTH:
        return False

    return all(c in _ALPHABET for c in text)


def session_id_from_ulid(ulid: str) -> int:
    """
    The lake's numeric join key, derived from the ULID.

    ClickHouse keys `samples` on a UInt64 `session_id`, and rekeying that
    to a string would rewrite every row and every query for no analytical
    gain. So the numeric id stays - but it is now a function of the
    session's own durable identity rather than of its filename.

    Deterministic, so a re-sync of the same run produces the same id and
    de-duplicates rather than doubling. 64 bits from a 128-bit input, so
    collisions are possible in principle; at a few hundred sessions a year
    they are not a practical concern, and `sessions.session_uid` carries
    the full identity for anything that needs certainty.
    """
    digest = zlib.crc32(ulid.encode()) & 0xFFFFFFFF
    tail = zlib.adler32(ulid.encode()) & 0xFFFFFFFF

    #: Two independent 32-bit functions rather than one repeated, so the
    #: high and low halves cannot agree by construction.
    return (digest << 32) | tail
