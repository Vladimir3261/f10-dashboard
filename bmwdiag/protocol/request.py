"""
Generic diagnostic request representation and transport interface.

`DiagnosticTransport` is the only thing the mapping engine needs in order
to talk to a vehicle. The application's HSFZ client is adapted to it; the
tests substitute a dictionary.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

try:                                    # pragma: no cover - 3.8+
    from typing import Protocol, runtime_checkable
except ImportError:                     # pragma: no cover
    Protocol = object                   # type: ignore

    def runtime_checkable(cls):         # type: ignore
        return cls

from ..errors import NegativeResponse as _NegativeResponse
from ..mapping.errors import MappingError
from ..mapping.model import RequestDef
from .correlate import ResponseExpectation, declared_response

__all__ = [
    "DiagnosticTransport",
    "ObdPidReader",
    "ObdExchange",
    "ObdReadReport",
    "DiagnosticRequest",
    "DecodedResponse",
    "NegativeResponse",
    "build_request",
    "build_payload",
]


@runtime_checkable
class DiagnosticTransport(Protocol):
    """
    Send one diagnostic payload to one ECU and return its response.

    `expect` says what the answer must look like - service id, echoed
    identifier, minimum length - as plain data the mapping layer built
    from the request definition. A transport uses it to CORRELATE: a
    frame that does not fit is not this request's answer and must not
    be returned as one, however plausible it looks. None means "apply
    the protocol's own echo rule" (bmwdiag.protocol.correlate), which
    is what the setup frames of a dynamic read and every ad-hoc probe
    get. Nothing here is HSFZ-specific: the expectation describes the
    diagnostic payload, not the framing around it.
    """

    def request(
        self,
        payload: bytes,
        *,
        dst: int,
        timeout: Optional[float] = None,
        expect: Optional[ResponseExpectation] = None,
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


@dataclass
class ObdExchange:
    """
    One physical Mode 01 exchange, as the reader saw it.

    A reader batches several logical requests into one frame, so the
    executor cannot count the wire from the requests it handed over. The
    reader tells it instead: which PIDs rode in this frame, whether the
    far side answered (a negative response and a NACK are answers; a
    timeout is not), which PIDs the answer actually carried, and the
    exception when the exchange failed - the real one, so the fault
    keeps its kind (`transport_timeout`, `negative_response`, ...)
    instead of collapsing into "no response". `started`/`finished` are
    monotonic; the latency is the difference.

    `answered` is liveness, not a frame count: a pending timeout is
    answered (the ECU said wait, so the link is alive) although no
    answer ever arrived. When `error` is set the executor derives what
    the exchange received from the error itself - a pending timeout is
    its `pending` frames and no latency sample, an NRC is one timed
    frame, a NACK one untimed gateway frame, a plain timeout nothing -
    and reads `answered` only when there is no error.
    """

    pids: Tuple[int, ...]
    started: float
    finished: float
    answered: bool = True
    returned: Tuple[int, ...] = ()
    error: Optional[BaseException] = None


@dataclass
class ObdReadReport:
    """
    What one `ObdPidReader.read()` call did on the wire.

    Optional on a reader - `MappingExecutor` looks for it as
    `reader.last_report` after each read and, absent one, assumes the
    read was a single answered exchange carrying every PID it asked for.
    `retired_now` names PIDs the reader gave up on DURING this read (so
    the retirement can be reported once); `retired` is every PID it will
    no longer attempt, which the executor consults BEFORE asking so a
    retired PID is never counted as submitted.
    """

    exchanges: List[ObdExchange] = field(default_factory=list)
    retired_now: Set[int] = field(default_factory=set)
    retired: Set[int] = field(default_factory=set)
    #: How many faults each PID retired in `retired_now` had accumulated.
    strikes: Dict[int, int] = field(default_factory=dict)


class UnresolvedTargetError(MappingError):
    """A request names a dynamic target nobody has resolved yet."""


# The negative-response type lives in the shared taxonomy (bmwdiag.errors)
# so mapping-level failures and the application's transport exceptions can
# inherit from one hierarchy; it stays importable from here.
NegativeResponse = _NegativeResponse


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

    def expectation(self) -> ResponseExpectation:
        """
        What the transport must see before this request counts as
        answered: the mapping's declared prefix when it has one (the
        mapping knows when a protocol does not echo), the structural
        echo rule otherwise - labelled with the request id so a late
        answer can be attributed to the request that asked for it.
        """
        return declared_response(
            self.payload, self.expect_prefix, self.min_length,
            label=self.request_id,
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
