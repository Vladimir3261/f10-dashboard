"""
Generic diagnostic request representation and transport interface.

`DiagnosticTransport` is the only thing the mapping engine needs in order
to talk to a vehicle. The application's HSFZ client is adapted to it; the
tests substitute a dictionary.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:                                    # pragma: no cover - 3.8+
    from typing import Protocol, runtime_checkable
except ImportError:                     # pragma: no cover
    Protocol = object                   # type: ignore

    def runtime_checkable(cls):         # type: ignore
        return cls

from ..mapping.errors import MappingError
from ..mapping.model import RequestDef

__all__ = [
    "DiagnosticTransport",
    "ObdPidReader",
    "DiagnosticRequest",
    "DecodedResponse",
    "NegativeResponse",
    "build_request",
    "build_payload",
]


@runtime_checkable
class DiagnosticTransport(Protocol):
    """Send one diagnostic payload to one ECU and return its response."""

    def request(
        self,
        payload: bytes,
        *,
        dst: int,
        timeout: Optional[float] = None,
    ) -> bytes:
        ...                             # pragma: no cover


@runtime_checkable
class ObdPidReader(Protocol):
    """
    Read a set of OBD Mode 01 PIDs, batching however the ECU allows.

    Standard OBD is the one protocol where the wire framing is not one
    request per mapped request: an ECU may answer six PIDs at once, and
    may stop doing so mid-drive. That negotiation belongs to the OBD
    session in the application, not to the mapping engine, so the engine
    asks for PIDs through this interface and gets data bytes back.
    """

    def read(self, pids: List[int]) -> Dict[int, bytes]:
        ...                             # pragma: no cover


class UnresolvedTargetError(MappingError):
    """A request names a dynamic target nobody has resolved yet."""


class NegativeResponse(Exception):
    """
    The ECU answered, and said no: a UDS/KWP `7F <service> <NRC>`.

    A transport raises a subclass of this (rather than a bare error with
    the code buried in the message) so the code is DATA to whoever
    catches it - the fault recorder groups on it, and an identity probe
    reports "NRC 0x31 to 22 F3 03" instead of "failed".
    """

    def __init__(self, service: int, nrc: int, message: Optional[str] = None):
        self.service = service
        self.nrc = nrc
        super().__init__(
            message or f"negative response to 0x{service:02X}: NRC 0x{nrc:02X}"
        )


@dataclass(frozen=True)
class DiagnosticRequest:
    """A request definition bound to a concrete address and payload."""

    request_id: str
    payload: bytes
    dst: int
    timeout: Optional[float] = None
    expect_prefix: bytes = b""
    min_length: int = 0

    def describe(self) -> str:
        return (
            f"{self.request_id} -> 0x{self.dst:02X} "
            f"[{self.payload.hex(' ')}]"
        )


@dataclass
class DecodedResponse:
    """What one exchange produced."""

    request_id: str
    raw: bytes
    #: Usable values only - what a caller that cannot carry a quality
    #: label should look at. Unchanged since before quality existed.
    values: Dict[str, Any] = field(default_factory=dict)
    #: Every signal the response carried, as key -> Reading, including the
    #: ones `values` leaves out because they are not measurements. This is
    #: what lets storage record that the ECU answered and said no-value.
    readings: Dict[str, Any] = field(default_factory=dict)
    #: When this exchange completed, as wall clock. Requests in one poll
    #: cycle are executed SEQUENTIALLY, so they do not share an instant -
    #: and a paired actual/setpoint stamped with one cycle timestamp
    #: would report a gap of exactly zero no matter how far apart the two
    #: reads really were. Recording per response keeps the separation
    #: observable instead of erasing it.
    at: float = 0.0


def build_payload(request: RequestDef) -> bytes:
    """
    Turn a request definition into the bytes that go on the wire.

    An explicit `payload:` always wins, which is the escape hatch for any
    proprietary job that does not fit service+identifier. Otherwise the
    protocol decides the shape - and note that nothing here assumes UDS
    0x22, or UDS at all.
    """
    if request.payload is not None:
        return bytes(request.payload)

    if request.service is None:
        raise MappingError(
            f"request {request.id!r} has neither a service nor a payload"
        )

    out = bytearray([request.service & 0xFF])

    if request.protocol == "obd":
        if request.pid is None:
            raise MappingError(f"obd request {request.id!r} has no pid")

        out.append(request.pid & 0xFF)
    elif request.protocol == "uds":
        if request.did is None:
            raise MappingError(f"uds request {request.id!r} has no did")

        out.append((request.did >> 8) & 0xFF)
        out.append(request.did & 0xFF)
    elif request.pid is not None:
        out.append(request.pid & 0xFF)

    return bytes(out)


def build_request(
    request: RequestDef,
    targets: Optional[Dict[str, int]] = None,
) -> DiagnosticRequest:
    """Bind a request definition to a concrete ECU address."""
    dst = request.target.resolve(targets or {})

    if dst is None:
        raise UnresolvedTargetError(
            f"request {request.id!r} targets {request.target.describe()!r}, "
            "which has not been resolved"
        )

    spec = request.response

    return DiagnosticRequest(
        request_id=request.id,
        payload=build_payload(request),
        dst=dst,
        timeout=request.timeout,
        expect_prefix=bytes(spec.prefix),
        min_length=max(spec.min_length, spec.total_length or 0),
    )
