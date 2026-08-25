"""
ECU variant capability - proprietary measurement families, kept out of
the generic mapping layer exactly like OBD support bitmasks.

A mapping that reads a BMW-proprietary dynamic measurement declares which
SGBD variant it applies to:

    ecu:
      match:
        capability:
          sgbd_variant: d72n47a0

The generic registry only asks "does this ECU satisfy that capability".
Answering it is variant-specific and lives here. An ECU never satisfies
a variant by its address or by an ident string alone - it satisfies one
by actually answering the read the variant is defined by. That is the
same discipline the OBD layer uses: capability by probe, never by
assumption.

`VariantProbe` is how the application confirms a variant on connect: it
replays a mapping's own dynamic read (the 2C define + 22 poll) and
checks the ECU answers in the expected shape. Nothing here opens a
socket; the application passes in a request callable.
"""

from typing import Callable, Iterable, List, Optional, Sequence, Set, Tuple

from .mapping.model import Capability, RequestDef
from .mapping.registry import CapabilitySet

__all__ = [
    "VARIANT_CAPABILITY",
    "VariantCapabilitySet",
    "CombinedCapabilitySet",
    "VariantProbe",
    "variant_probes",
]

#: The capability kind a variant-gated request requires.
VARIANT_CAPABILITY = "sgbd_variant"


class VariantCapabilitySet(CapabilitySet):
    """
    The set of SGBD variants an ECU has been confirmed to be.

    Only answers `sgbd_variant` questions; every other kind is not ours,
    so we refuse it (`False`) and let another provider answer. An empty
    set satisfies nothing - a variant is proven, never assumed.
    """

    def __init__(self, confirmed: Optional[Iterable[str]] = None):
        self.confirmed: Set[str] = {str(v) for v in (confirmed or ())}

    @property
    def known(self) -> bool:
        return bool(self.confirmed)

    def satisfies(self, capability: Capability) -> bool:
        if capability.kind != VARIANT_CAPABILITY:
            return False

        return str(capability.value) in self.confirmed

    def __repr__(self) -> str:
        return f"VariantCapabilitySet({sorted(self.confirmed)})"


class CombinedCapabilitySet(CapabilitySet):
    """
    Several capability providers behind one interface.

    A capability is satisfied when ANY provider satisfies it, so an OBD
    provider answers `obd_mode01_pid` questions and a variant provider
    answers `sgbd_variant` ones without either knowing about the other.
    `known` is true if any member is known.
    """

    def __init__(self, *providers: CapabilitySet):
        self.providers: List[CapabilitySet] = list(providers)

    @property
    def known(self) -> bool:
        return any(getattr(p, "known", True) for p in self.providers)

    def satisfies(self, capability: Capability) -> bool:
        return any(p.satisfies(capability) for p in self.providers)

    def __repr__(self) -> str:
        return f"CombinedCapabilitySet({self.providers!r})"


class VariantProbe:
    """
    Confirms SGBD variants by replaying a mapping's own dynamic read.

    Given the candidate requests for a variant, it sends the first one's
    setup sequence and poll, and confirms the variant if the reply
    carries the expected prefix. This is deliberately data-driven: the
    variant, the frames and the expected shape all come from the mapping
    file, so a new variant needs a new mapping, not new code.
    """

    def __init__(
        self,
        request: Callable[..., bytes],
        timeout: Optional[float] = None,
    ):
        #: `request(payload, dst=..., timeout=...) -> bytes`, or raises.
        self._request = request
        self.timeout = timeout

    def _probe_one(self, req: RequestDef, dst: int) -> bool:
        from .protocol.request import build_payload

        try:
            for frame in req.setup:
                self._request(bytes(frame), dst=dst, timeout=self.timeout)

            response = self._request(
                build_payload(req), dst=dst, timeout=self.timeout
            )
        except Exception:
            return False

        prefix = bytes(req.response.prefix)

        if prefix and not bytes(response).startswith(prefix):
            return False

        need = max(
            req.response.min_length,
            (req.response.payload_offset + (req.response.data_length or 0)),
        )

        return len(response) >= need and len(response) > 0

    def confirm(
        self,
        variant_requests: Sequence[Tuple[str, RequestDef]],
        dst: int,
    ) -> Set[str]:
        """
        Probe one representative request per variant.

        `variant_requests` pairs a variant name with a request that
        proves it. Returns the set of variants the ECU confirmed.
        """
        confirmed: Set[str] = set()
        seen: Set[str] = set()

        for variant, req in variant_requests:
            if variant in seen:
                continue

            seen.add(variant)

            if self._probe_one(req, dst):
                confirmed.add(variant)

        return confirmed


def variant_probes(mappings) -> List[Tuple[str, RequestDef]]:
    """
    (variant, representative request) for every variant-gated mapping.

    A mapping declares its variant in `ecu.match` as an `sgbd_variant`
    capability; its first request is the one probed to confirm it. A
    mapping with no such match, or no requests, contributes nothing.
    """
    out: List[Tuple[str, RequestDef]] = []

    for mapping in mappings:
        variants = [
            str(c.value) for c in mapping.ecu.match
            if c.kind == VARIANT_CAPABILITY
        ]

        if not variants or not mapping.requests:
            continue

        for variant in variants:
            out.append((variant, mapping.requests[0]))

    return out
