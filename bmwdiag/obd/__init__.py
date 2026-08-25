"""
Standard OBD-II specifics, kept out of the generic mapping layer.

Mode 01 support bitmasks, the `01 00 / 01 20 / ...` traversal and the
"is this the engine" test are properties of SAE J1979, not of the mapping
engine. Isolating them here means the mapping decoder never learns what a
PID is, and a future BMW-proprietary capability provider can sit beside
this one without either knowing about the other.
"""

from .capability import (
    OBD_SUPPORT_PIDS,
    ObdCapabilityProvider,
    ObdCapabilitySet,
    supported_from_bitmask,
    walk_supported_pids,
)

__all__ = [
    "OBD_SUPPORT_PIDS",
    "ObdCapabilityProvider",
    "ObdCapabilitySet",
    "supported_from_bitmask",
    "walk_supported_pids",
]
