"""
Sync wire format - columnar batches, LZMA-compressed.

The client ships telemetry to the ingest server over mobile networks, so
the payload has to be small. Two cheap, dependency-free wins:

  * **Columnar** - values of one column sit together, so repetitive
    telemetry (the same VIN, session and channel names over and over,
    slowly-changing numbers) compresses far better than row-oriented
    JSON would.
  * **LZMA** - Python's stdlib `lzma` gives an excellent ratio with no
    third-party dependency, which keeps the client runnable on the same
    laptop-in-a-car that must not need `pip install`.

A batch is a plain dict:

    {
      "table": "samples",
      "rows": <int>,
      "cursor": <int>,          # max rowid in this batch (the watermark)
      "meta": {...},            # db name, mapping_ver, agent id
      "cols": {name: [values...], ...}
    }

`encode` returns compressed bytes; `decode` returns the dict. Both sides
import this one module, so the format can never drift between them.
"""

import json
import lzma
from typing import Any, Dict, List

__all__ = ["encode", "decode", "columnar", "rows_of", "MAGIC", "VERSION"]

MAGIC = b"F10SYNC1"
VERSION = 1

#: LZMA preset. 6 is a good ratio/speed balance for a phone CPU; the data
#: is so repetitive that higher presets buy little.
_PRESET = 6 | lzma.PRESET_EXTREME


def columnar(table: str, rows: List[Dict[str, Any]], *,
             cursor: int = 0, meta: Dict[str, Any] = None) -> Dict[str, Any]:
    """Turn a list of row dicts into a columnar batch dict."""
    cols: Dict[str, List[Any]] = {}

    if rows:
        keys = list(rows[0].keys())

        for key in keys:
            cols[key] = [r.get(key) for r in rows]

    return {
        "table": table,
        "rows": len(rows),
        "cursor": cursor,
        "meta": meta or {},
        "cols": cols,
    }


def rows_of(batch: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Inverse of `columnar`: expand a batch back into row dicts."""
    cols = batch.get("cols", {})

    if not cols:
        return []

    names = list(cols.keys())
    n = len(cols[names[0]])

    return [{name: cols[name][i] for name in names} for i in range(n)]


def encode(batch: Dict[str, Any]) -> bytes:
    """Serialise + compress a batch. Framed with a magic header."""
    raw = json.dumps(batch, separators=(",", ":")).encode("utf-8")
    body = lzma.compress(raw, preset=_PRESET)

    return MAGIC + bytes([VERSION]) + body


def decode(blob: bytes) -> Dict[str, Any]:
    """Inverse of `encode`. Raises ValueError on a bad frame."""
    if len(blob) < len(MAGIC) + 1 or blob[: len(MAGIC)] != MAGIC:
        raise ValueError("not a sync batch (bad magic)")

    version = blob[len(MAGIC)]

    if version != VERSION:
        raise ValueError(f"unsupported wire version {version}")

    raw = lzma.decompress(blob[len(MAGIC) + 1:])

    return json.loads(raw.decode("utf-8"))
