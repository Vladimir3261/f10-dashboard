"""
ECU compatibility and identity - two claims, kept apart.

A mapping that reads a BMW-proprietary measurement needs the ECU to
speak the family of reads it was written for. It declares that as a
capability and nominates the requests that prove it:

    ecu:
      sgbd: d72n47a0                # the table the rows came from
      match:
        capability:
          diagnostic_profile: fseries-f303-d72-compatible
        probe: [n47.d72.dyn.4517, n47.d72.dyn.4BC3]

A `diagnostic_profile` is *behavioural compatibility*: the ECU answers
one of the nominated reads in its declared shape. That is what a probe
can prove, and it is the only thing activating a mapping needs.

`exact_sgbd` is a different claim - "this ECU is exactly that SGBD
revision" - and a successful read never proves it. Two SGBDs of one
family accept the same `2C 01 F3 03` define and can still disagree on
what source 0x4517 means. Identity comes only from identity evidence
(an ident DID that answers, a reference tool's ident page), which on
F10-520d-dev does not exist yet: F191/F194/F197/F18A all return NRC
0x31. So the honest answer is `unknown`, and a mapping that needs more
than compatibility says `exact_sgbd:` and stays dormant until evidence
arrives. Before 2026-09-05 one capability (`sgbd_variant`) carried both
claims and one probe "confirmed" it - the runtime claimed an identity it
had never seen.

Every outcome carries its reason. A probe that fails says how - the ECU
refused (NRC), the gateway would not route, nothing came back, the
answer had the wrong prefix or was too short - and a profile nobody
nominated a probe for is `unknown`, not `False`. Nothing here opens a
socket; the application passes in a request callable.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .mapping.execute import fault_kind
from .mapping.model import Capability, MappingFile, RequestDef
from .mapping.registry import CapabilitySet

__all__ = [
    "PROFILE_CAPABILITY",
    "SGBD_CAPABILITY",
    "CONFIRMED",
    "COMPATIBLE",
    "AMBIGUOUS",
    "UNSUPPORTED",
    "UNKNOWN",
    "ProbeResult",
    "ProfileNomination",
    "ProfileResolution",
    "IdentityFact",
    "IdentityResolution",
    "EcuIdentity",
    "CombinedCapabilitySet",
    "ProfileProbe",
    "profile_nominations",
]

#: Behavioural compatibility - proven by a nominated probe answering.
PROFILE_CAPABILITY = "diagnostic_profile"
#: Exact SGBD identity - proven by identity evidence, never by a probe.
SGBD_CAPABILITY = "exact_sgbd"

#: Outcomes. A requirement resolves to exactly one of these, and the
#: vocabulary is deliberately asymmetric: a profile can be `compatible`
#: but never `confirmed`, an identity `confirmed` but never `compatible`.
CONFIRMED = "confirmed"      # identity: evidence names exactly this SGBD
COMPATIBLE = "compatible"    # profile: a nominated probe answered in shape
AMBIGUOUS = "ambiguous"      # identity: the evidence disagrees with itself
UNSUPPORTED = "unsupported"  # profile: every probe failed / identity: another SGBD
UNKNOWN = "unknown"          # nothing was probed, or no evidence exists


# ------------------------------------------------------------- results


@dataclass(frozen=True)
class ProbeResult:
    """One nominated request, sent, and what came of it."""

    request_id: str
    answered: bool
    #: Stable, groupable: "answered" | "wrong_prefix" | "short_response"
    #: | a `fault_kind` ("negative_response", "transport_timeout",
    #: "transport_nack", "transport_link", "other").
    reason: str
    #: Human detail: the NRC and the frame it refused, the bytes that
    #: came back, the exception text.
    detail: str = ""

    def describe(self) -> str:
        text = f"{self.request_id}: {self.reason}"

        return f"{text} ({self.detail})" if self.detail else text

    def as_dict(self) -> Dict[str, Any]:
        return {
            "request": self.request_id, "answered": self.answered,
            "reason": self.reason, "detail": self.detail,
        }


@dataclass(frozen=True)
class ProfileNomination:
    """What the loaded mappings say about one profile, before any wire."""

    profile: str
    #: Nominated probes across every file requiring the profile, in
    #: mapping order, one entry per request id.
    requests: Tuple[RequestDef, ...] = ()
    #: `ecu.sgbd` of those files - the tables the rows were derived
    #: from. Provenance the report shows next to `exact_sgbd: unknown`;
    #: it is not evidence and never satisfies an identity requirement.
    derived_from: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileResolution:
    """What the ECU proved about one profile, and how."""

    profile: str
    outcome: str                              # COMPATIBLE | UNSUPPORTED | UNKNOWN
    probes: Tuple[ProbeResult, ...] = ()
    derived_from: Tuple[str, ...] = ()
    #: Why `unknown`, when it is.
    note: str = ""

    def describe(self) -> str:
        if self.outcome == COMPATIBLE:
            hit = next(p for p in self.probes if p.answered)

            return f"{COMPATIBLE}: {hit.request_id} answered"

        if self.outcome == UNSUPPORTED:
            return (
                f"{UNSUPPORTED}: "
                + "; ".join(p.describe() for p in self.probes)
            )

        return f"{UNKNOWN}: {self.note or 'not probed'}"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "profile": self.profile,
            "outcome": self.outcome,
            "probes": [p.as_dict() for p in self.probes],
            "derived_from": list(self.derived_from),
            "note": self.note,
            "summary": self.describe(),
        }


@dataclass(frozen=True)
class IdentityFact:
    """One piece of identity evidence: which SGBD, and who said so."""

    sgbd: str
    #: e.g. "uds 22 F19E", "ISTA ident page 2026-08-25". Never a probe.
    origin: str

    def as_dict(self) -> Dict[str, Any]:
        return {"sgbd": self.sgbd, "origin": self.origin}


@dataclass(frozen=True)
class IdentityResolution:
    """
    What the evidence says the ECU IS. Additive and separate from every
    profile: a profile resolution never feeds this, in either direction.
    """

    outcome: str = UNKNOWN                    # CONFIRMED | AMBIGUOUS | UNKNOWN
    sgbd: Optional[str] = None
    facts: Tuple[IdentityFact, ...] = ()

    @classmethod
    def from_facts(cls, facts: Iterable[IdentityFact]) -> "IdentityResolution":
        facts = tuple(facts)
        names = sorted({f.sgbd for f in facts})

        if not names:
            return cls(UNKNOWN, None, facts)

        if len(names) == 1:
            return cls(CONFIRMED, names[0], facts)

        return cls(AMBIGUOUS, None, facts)

    def describe(self) -> str:
        if self.outcome == CONFIRMED:
            return f"{CONFIRMED}: {self.sgbd} per " + ", ".join(
                f.origin for f in self.facts
            )

        if self.outcome == AMBIGUOUS:
            return f"{AMBIGUOUS}: evidence disagrees - " + "; ".join(
                f"{f.sgbd} per {f.origin}" for f in self.facts
            )

        return f"{UNKNOWN}: no identity evidence this session"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome,
            "sgbd": self.sgbd,
            "facts": [f.as_dict() for f in self.facts],
            "summary": self.describe(),
        }


# --------------------------------------------------------- capability


class EcuIdentity(CapabilitySet):
    """
    What one ECU has demonstrated (profiles) and what is known about
    what it is (identity), answering `diagnostic_profile` and
    `exact_sgbd` questions. Every other kind is not ours - refused, so
    another provider can answer it.

    Nothing is assumed: an empty EcuIdentity satisfies no requirement,
    and `outcome()` says `unknown` for a profile nobody probed rather
    than `unsupported`.
    """

    def __init__(
        self,
        profiles: Iterable[ProfileResolution] = (),
        identity: Optional[IdentityResolution] = None,
    ):
        self.profiles: Dict[str, ProfileResolution] = {
            p.profile: p for p in profiles
        }
        self.identity = identity or IdentityResolution()

    @property
    def known(self) -> bool:
        return (
            any(p.outcome == COMPATIBLE for p in self.profiles.values())
            or self.identity.outcome == CONFIRMED
        )

    @property
    def compatible(self) -> Set[str]:
        return {
            name for name, p in self.profiles.items() if p.outcome == COMPATIBLE
        }

    def outcome(self, capability: Capability) -> str:
        """The resolution of one requirement, in the shared vocabulary."""
        value = str(capability.value)

        if capability.kind == PROFILE_CAPABILITY:
            resolution = self.profiles.get(value)

            return resolution.outcome if resolution else UNKNOWN

        if capability.kind == SGBD_CAPABILITY:
            if self.identity.outcome == CONFIRMED:
                return CONFIRMED if self.identity.sgbd == value else UNSUPPORTED

            return self.identity.outcome

        return UNKNOWN

    def satisfies(self, capability: Capability) -> bool:
        if capability.kind == PROFILE_CAPABILITY:
            return self.outcome(capability) == COMPATIBLE

        if capability.kind == SGBD_CAPABILITY:
            return self.outcome(capability) == CONFIRMED

        return False

    def explain(self, capability: Capability) -> Optional[str]:
        value = str(capability.value)

        if capability.kind == PROFILE_CAPABILITY:
            resolution = self.profiles.get(value)

            if resolution is None:
                return f"{UNKNOWN}: no loaded mapping nominates a probe for it"

            return resolution.describe()

        if capability.kind == SGBD_CAPABILITY:
            outcome = self.outcome(capability)

            if outcome == UNSUPPORTED:
                return f"{UNSUPPORTED}: the evidence says {self.identity.sgbd}"

            return self.identity.describe()

        return None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "profiles": [
                self.profiles[name].as_dict() for name in sorted(self.profiles)
            ],
            "exact_sgbd": self.identity.as_dict(),
        }

    def __repr__(self) -> str:
        return (
            f"EcuIdentity(compatible={sorted(self.compatible)}, "
            f"exact_sgbd={self.identity.outcome})"
        )


class CombinedCapabilitySet(CapabilitySet):
    """
    Several capability providers behind one interface.

    A capability is satisfied when ANY provider satisfies it, so an OBD
    provider answers `obd_mode01_pid` questions and an EcuIdentity
    answers `diagnostic_profile` ones without either knowing about the
    other. `known` is true if any member is known; `explain` is the
    first provider with something to say.
    """

    def __init__(self, *providers: CapabilitySet):
        self.providers: List[CapabilitySet] = list(providers)

    @property
    def known(self) -> bool:
        return any(getattr(p, "known", True) for p in self.providers)

    def satisfies(self, capability: Capability) -> bool:
        return any(p.satisfies(capability) for p in self.providers)

    def explain(self, capability: Capability) -> Optional[str]:
        for provider in self.providers:
            why = provider.explain(capability)

            if why:
                return why

        return None

    def __repr__(self) -> str:
        return f"CombinedCapabilitySet({self.providers!r})"


# ------------------------------------------------------------- probing


class ProfileProbe:
    """
    Resolves diagnostic profiles by replaying their nominated reads.

    For each profile the nominated requests are sent in order - setup
    frames, then the poll - and the first one whose reply carries the
    declared prefix and length makes the profile `compatible`. Further
    nominations are then not sent: compatibility is one shape-correct
    answer, and a second one would add wire traffic at connect and no
    evidence about identity. If every nomination fails the profile is
    `unsupported`, with each failure recorded; with no nomination at all
    it is `unknown`. Data-driven throughout: the profile, the frames and
    the expected shape all come from the mapping files.
    """

    def __init__(
        self,
        request: Callable[..., bytes],
        timeout: Optional[float] = None,
    ):
        #: `request(payload, dst=..., timeout=...) -> bytes`, or raises.
        self._request = request
        self.timeout = timeout

    def _exchange(self, payload: bytes, dst: int) -> bytes:
        return bytes(self._request(payload, dst=dst, timeout=self.timeout))

    def probe_one(self, req: RequestDef, dst: int) -> ProbeResult:
        from .protocol.request import build_payload

        try:
            for frame in req.setup:
                frame = bytes(frame)
                try:
                    self._exchange(frame, dst)
                except Exception as exc:
                    return ProbeResult(
                        req.id, False, fault_kind(exc),
                        f"setup {frame.hex(' ')}: {exc}",
                    )

            payload = build_payload(req)
            response = self._exchange(payload, dst)
        except Exception as exc:
            return ProbeResult(req.id, False, fault_kind(exc), str(exc))

        prefix = bytes(req.response.prefix)

        if not response:
            return ProbeResult(
                req.id, False, "short_response", "empty response"
            )

        if prefix and not response.startswith(prefix):
            return ProbeResult(
                req.id, False, "wrong_prefix",
                f"expected {prefix.hex(' ')}, got "
                + (response.hex(" ") or "nothing"),
            )

        need = max(
            1,
            req.response.min_length,
            req.response.payload_offset + (req.response.data_length or 0),
        )

        if len(response) < need:
            return ProbeResult(
                req.id, False, "short_response",
                f"expected at least {need} bytes, got {len(response)}",
            )

        return ProbeResult(req.id, True, "answered", response.hex(" "))

    def resolve_profile(
        self, nomination: ProfileNomination, dst: int
    ) -> ProfileResolution:
        if not nomination.requests:
            return ProfileResolution(
                nomination.profile, UNKNOWN,
                derived_from=nomination.derived_from,
                note="no loaded mapping nominates a probe for it",
            )

        results: List[ProbeResult] = []

        for req in nomination.requests:
            result = self.probe_one(req, dst)
            results.append(result)

            if result.answered:
                return ProfileResolution(
                    nomination.profile, COMPATIBLE, tuple(results),
                    nomination.derived_from,
                )

        return ProfileResolution(
            nomination.profile, UNSUPPORTED, tuple(results),
            nomination.derived_from,
        )

    def resolve(
        self, nominations: Sequence[ProfileNomination], dst: int
    ) -> List[ProfileResolution]:
        """One resolution per nominated profile, in nomination order."""
        return [self.resolve_profile(n, dst) for n in nominations]


def profile_nominations(
    mappings: Iterable[MappingFile],
) -> List[ProfileNomination]:
    """
    Gather every profile the loaded mappings require, with the probes
    they nominate for it and the SGBD tables they were derived from.

    A profile several files require is one nomination - its probes in
    mapping order, each request id once - so one connect probes it once.
    A file that requires a profile but nominates nothing still puts the
    profile on the list: it resolves `unknown`, visibly, instead of the
    file vanishing from the profile without a word.
    """
    order: List[str] = []
    requests: Dict[str, List[RequestDef]] = {}
    derived: Dict[str, List[str]] = {}

    for mapping in mappings:
        profiles = [
            str(c.value) for c in mapping.ecu.match
            if c.kind == PROFILE_CAPABILITY
        ]

        if not profiles:
            continue

        by_id = {r.id: r for r in mapping.requests}

        for profile in profiles:
            if profile not in requests:
                order.append(profile)
                requests[profile] = []
                derived[profile] = []

            for request_id in mapping.ecu.probe:
                if all(r.id != request_id for r in requests[profile]):
                    requests[profile].append(by_id[request_id])

            if mapping.ecu.sgbd and mapping.ecu.sgbd not in derived[profile]:
                derived[profile].append(mapping.ecu.sgbd)

    return [
        ProfileNomination(p, tuple(requests[p]), tuple(derived[p]))
        for p in order
    ]
