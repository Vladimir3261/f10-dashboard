"""Shared helpers. Keeps the repository root importable under any runner."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MAPPINGS = os.path.join(ROOT, "mappings")
OBD_MAPPING = os.path.join(MAPPINGS, "obd", "engine.yaml")
EXAMPLE_MAPPING = os.path.join(MAPPINGS, "examples", "uds_example.yaml")


def hexb(text: str) -> bytes:
    """b'\\x41\\x0c' from '41 0C'."""
    return bytes(int(part, 16) for part in text.split())
