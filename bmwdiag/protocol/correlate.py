"""
Request/response correlation - what a frame must look like to be THE
answer to the request in flight.

Protocol knowledge, no I/O. A transport asks two questions of this
module: "what should the answer to this payload look like?" and "is
this frame that answer, a pending notice, a refusal, or somebody
else's?". Both are answered from bytes alone, so the rule is testable
against captured traffic and identical for every transport.

Why it exists
-------------
A transport that accepts any frame with the right response service id
cannot tell the answer to `22 12 34` from a late answer to `22 56 78`.
The decoder catches that one - the mapping's prefix does not match - but
it records the fault as a decode error, which is the label for a
mapping bug, and the real answer is still in flight to be mistaken for
the answer to the request after that.

The case that matters most cannot be caught by content at all: several
channels share one dynamic identifier (F303), redefined between reads,
so a late `62 F3 03 ..` from the previous definition is byte-for-byte
plausible under the next. That is why correlation here is one of three
layers, not the whole answer:

  1. this module: service id + echoed identifier + minimum length,
     from the protocol's own echo rule or from what the mapping
     declares (the mapping wins - it knows when a protocol does not
     echo);
  2. the transport: a timed-out request stays OUTSTANDING; the line is
     given a bounded quiet window before the next request to that ECU,
     and anything matching the outstanding expectation is attributed
     to it and discarded, never returned;
  3. the executor: a fault during a dynamic-identifier sequence
     disarms the definition, so the next read re-sends the clear and
     define - two exchanges the ECU answers in order, which sit between
     the old poll and the new one.

The residual - a response arriving after its timeout, after the quiet
window AND after the ECU has answered two later requests - assumes an
ECU that answers out of order, which ISO 14229's single-outstanding-
request model does not permit. It is bounded, stated, and counted
rather than assumed away.

Echo rules
----------
Positive responses echo part of the request, and which part is fixed
by the service:

    01 <pid..>            -> 41 <pid>            (SAE J1979, one of them)
    09 <pid..>            -> 49 <pid>
    22 <did..>            -> 62 <did>            (ISO 14229, one of them)
    2C <sub> [<did>]      -> 6C <sub> [<did>]    (on-car: 6C 03 F3 03)
    19 <sub>              -> 59 <sub>
    3E <sub>              -> 7E <sub>
    10 <sub>              -> 50 <sub>
    21 <lid>              -> 61 <lid>            (KWP local identifier)
    1A <lid>              -> 5A <lid>

Anything else is correlated on the service id alone, which is what the
transport always did. A mapping's declared `response.prefix` overrides
the rule - the KWP `2C 10 04 06 -> 6C 10 0E D7` exchange is exactly a
protocol that does NOT echo, and only the mapping can say so.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

__all__ = [
    "ResponseExpectation",
    "expected_response",
    "declared_response",
    "classify",
    "MATCH",
    "PENDING",
    "NEGATIVE",
    "FOREIGN",
    "NRC_RESPONSE_PENDING",
]

#: UDS/KWP negative response service id.
NEGATIVE_SID = 0x7F
#: NRC 0x78 requestCorrectlyReceived-ResponsePending: the ECU has the
#: request and asks for more time. Not a refusal.
NRC_RESPONSE_PENDING = 0x78

#: Outcomes of `classify`.
MATCH = "match"          # the answer to the request in flight
PENDING = "pending"      # 7F <service> 78 - keep waiting, within bounds
NEGATIVE = "negative"    # 7F <service> <nrc> - the ECU said no
FOREIGN = "foreign"      # not this request's - late, stray, or another ECU's

#: How many bytes after the service id each request carries as the
#: identifier that a positive response echoes. Services that take a LIST
#: of identifiers are in _LIST_SERVICES: a multi-PID/DID request is
#: answered starting with any one of them, so each is acceptable.
_ECHO_LENGTH = {
    0x01: 1, 0x09: 1,           # OBD: PID
    0x22: 2,                    # UDS ReadDataByIdentifier: DID
    0x19: 1,                    # UDS ReadDTCInformation: sub-function
    0x3E: 1,                    # UDS TesterPresent: sub-function
    0x10: 1,                    # UDS DiagnosticSessionControl (tool opt-in)
    0x21: 1, 0x1A: 1,           # KWP local identifier reads
    0x2C: 3,                    # UDS DDDI: sub-function + dynamic DID
}
_LIST_SERVICES = {0x01: 1, 0x09: 1, 0x22: 2}


@dataclass(frozen=True)
class ResponseExpectation:
    """
    The shape a frame must have to be accepted as the answer.

    Plain data - no callables - so a mapping can describe it, a test can
    construct it, and a future embedded runtime can hold it in a struct.

    `echo` is a set of alternatives: a positive response must start with
    `sid` followed by any one of them. Empty means "service id only",
    which is the weakest correlation the transport ever applies, and is
    what a mapping asks for by declaring a one-byte prefix.
    """

    #: The request's service id, for `7F <service> <nrc>` correlation.
    service: int
    #: The positive response service id (normally service + 0x40).
    sid: int
    echo: Tuple[bytes, ...] = ()
    min_length: int = 0
    #: "structural" (from the echo rule) or "declared" (from a mapping).
    origin: str = "structural"
    #: Who asked, for attributing a late answer: a request id when the
    #: executor built this, otherwise empty. Never used for matching.
    label: str = ""

    def matches_positive(self, body: bytes) -> bool:
        """Is `body` a positive response of this shape?"""
        if not body or body[0] != self.sid:
            return False

        if len(body) < self.min_length:
            return False

        if not self.echo:
            return True

        return any(
            body[1:1 + len(alt)] == alt for alt in self.echo
        )

    def indistinguishable_from(self, other: "ResponseExpectation") -> bool:
        """
        Would every frame this accepts also satisfy `other`?

        True for the F303 case - two reads of the same dynamic DID under
        different definitions - and for re-polling one DID after its own
        timeout. Those are the cases content cannot settle and the
        transport must bound another way.
        """
        if self.sid != other.sid:
            return False

        if not other.echo:
            return True                 # the other accepts any echo

        if not self.echo:
            return False                # this accepts more than the other

        return set(self.echo) <= set(other.echo)

    def describe(self) -> str:
        head = f"{self.sid:02X}"

        if not self.echo:
            return head

        if len(self.echo) == 1:
            return f"{head} {self.echo[0].hex(' ')}".strip()

        return head + " (" + "|".join(alt.hex(' ') for alt in self.echo) + ")"


def expected_response(payload: bytes) -> ResponseExpectation:
    """
    The structural expectation for a request payload: service id plus
    whatever the protocol echoes for that service.
    """
    payload = bytes(payload)

    if not payload:
        raise ValueError("empty payload has no expected response")

    service = payload[0]
    sid = (service + 0x40) & 0xFF
    body = payload[1:]
    echo: Tuple[bytes, ...] = ()

    if service in _LIST_SERVICES:
        width = _LIST_SERVICES[service]
        alternatives = [
            body[i:i + width]
            for i in range(0, len(body) - width + 1, width)
        ]
        echo = tuple(dict.fromkeys(a for a in alternatives if a))
    elif service in _ECHO_LENGTH:
        want = min(_ECHO_LENGTH[service], len(body))

        if want:
            echo = (body[:want],)

    return ResponseExpectation(
        service=service, sid=sid, echo=echo,
        min_length=1 + (len(echo[0]) if echo else 0),
    )


def declared_response(
    payload: bytes,
    prefix: bytes,
    min_length: int = 0,
    label: str = "",
) -> ResponseExpectation:
    """
    The expectation a mapping declares through `response.prefix`.

    A declared prefix is exactly what the transport must see, no more:
    a one-byte prefix means the mapping is saying "this protocol does
    not echo", and the structural rule must not be applied on top of it.
    An EMPTY prefix declares nothing, and the structural rule applies.
    """
    prefix = bytes(prefix)

    if not prefix:
        base = expected_response(payload)

        return ResponseExpectation(
            service=base.service, sid=base.sid, echo=base.echo,
            min_length=max(base.min_length, min_length),
            origin="structural", label=label,
        )

    return ResponseExpectation(
        service=bytes(payload)[0],
        sid=prefix[0],
        echo=(prefix[1:],) if len(prefix) > 1 else (),
        min_length=max(len(prefix), min_length),
        origin="declared",
        label=label,
    )


def classify(
    expectation: ResponseExpectation, body: bytes
) -> Tuple[str, Optional[int]]:
    """
    Sort one received body against the request in flight.

    Returns (outcome, nrc): the NRC is set for PENDING and NEGATIVE, None
    otherwise. A `7F` naming a DIFFERENT service is FOREIGN - a refusal
    of somebody else's request is not a refusal of this one.
    """
    body = bytes(body)

    if not body:
        return FOREIGN, None

    if body[0] == NEGATIVE_SID:
        if len(body) < 3 or body[1] != expectation.service:
            return FOREIGN, None

        nrc = body[2]

        if nrc == NRC_RESPONSE_PENDING:
            return PENDING, nrc

        return NEGATIVE, nrc

    if expectation.matches_positive(body):
        return MATCH, None

    return FOREIGN, None
