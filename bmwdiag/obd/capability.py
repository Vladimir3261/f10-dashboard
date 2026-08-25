"""
OBD Mode 01 capability discovery.

An ECU advertises which current-data PIDs it implements through four
bitmask PIDs. Walking them is the only honest way to know what a given
car supports - an address is never evidence.

The result is an `ObdCapabilitySet`, which answers the one generic
question the mapping registry asks: does this ECU satisfy this capability?
"""

from typing import Callable, Dict, Iterable, Optional, Set

from ..mapping.model import Capability
from ..mapping.registry import CapabilitySet

__all__ = [
    "OBD_SUPPORT_PIDS",
    "OBD_CAPABILITY_PID",
    "supported_from_bitmask",
    "walk_supported_pids",
    "ObdCapabilitySet",
    "ObdCapabilityProvider",
]

#: The Mode 01 support bitmask PIDs, and the four bytes each returns.
#: These are protocol structure, not vehicle knowledge, so they live with
#: the OBD layer rather than in a mapping file.
OBD_SUPPORT_PIDS: Dict[int, int] = {0x00: 4, 0x20: 4, 0x40: 4, 0x60: 4}

#: The capability kind an OBD-protocol request requires.
OBD_CAPABILITY_PID = "obd_mode01_pid"

#: PID 0x0C (engine speed) is what identifies an engine ECU.
ENGINE_PID = 0x0C


def supported_from_bitmask(base: int, bits: int) -> Set[int]:
    """Expand one 32-bit support bitmask into the PIDs it advertises."""
    found: Set[int] = set()

    for i in range(32):
        if bits & (1 << (31 - i)):
            found.add(base + i + 1)

    return found


def walk_supported_pids(
    request: Callable[[bytes], bytes],
    bases: Iterable[int] = (0x00, 0x20, 0x40, 0x60),
) -> Set[int]:
    """
    Walk the support bitmask blocks for one ECU.

    `request` sends a Mode 01 payload and returns the response, or raises.
    Any failure ends the walk with whatever was gathered so far, which is
    what the previous implementation did.
    """
    found: Set[int] = set()

    for base in bases:
        try:
            resp = request(bytes([0x01, base]))
        except Exception:
            break

        if len(resp) < 6 or resp[0] != 0x41 or resp[1] != base:
            break

        bits = int.from_bytes(resp[2:6], "big")
        found |= supported_from_bitmask(base, bits)

        #
        # The lowest bit of each block advertises the next block.
        #
        if not bits & 1:
            break

    return found


class ObdCapabilitySet(CapabilitySet):
    """
    The capabilities of one OBD-speaking ECU.

    An empty support set means discovery told us nothing; `known` is then
    False and every check passes, so an ECU that answers Mode 01 but
    publishes no bitmask is still polled across the whole table.
    """

    def __init__(self, supported: Optional[Iterable[int]] = None):
        self.supported: Set[int] = set(supported or ())

    @property
    def known(self) -> bool:
        return bool(self.supported)

    def satisfies(self, capability: Capability) -> bool:
        if capability.kind != OBD_CAPABILITY_PID:
            #
            # Unknown capability kinds are not ours to answer. Refusing is
            # the safe default: it disables the request rather than
            # sending an unmapped payload to an ECU.
            #
            return False

        if not self.known:
            return True

        try:
            pid = int(capability.value)
        except (TypeError, ValueError):
            return False

        return pid in self.supported

    @property
    def is_engine(self) -> bool:
        return ENGINE_PID in self.supported

    def score(self, pids: Iterable[int]) -> int:
        """How many of the mapped PIDs this ECU advertises."""
        return sum(1 for pid in pids if pid in self.supported)

    def __repr__(self) -> str:
        return f"ObdCapabilitySet({len(self.supported)} pids)"


class ObdCapabilityProvider:
    """
    Discovers OBD capability for an ECU address over a transport.

    Kept as a thin object so the application can reuse its own HSFZ
    request helper (with reconnect-on-broken-pipe) rather than this
    module having any opinion about sockets.
    """

    def __init__(
        self,
        request: Callable[..., bytes],
        timeout: Optional[float] = None,
    ):
        self._request = request
        self.timeout = timeout

    def _send(self, dst: int, payload: bytes) -> bytes:
        return self._request(payload, dst=dst, timeout=self.timeout)

    def supported(self, dst: int) -> Set[int]:
        return walk_supported_pids(lambda payload: self._send(dst, payload))

    def capabilities(self, dst: int) -> ObdCapabilitySet:
        return ObdCapabilitySet(self.supported(dst))
