"""
What went wrong, as a TYPE - the diagnostic error taxonomy.

Every failure between "we sent a request" and "we have a decoded value"
falls into one of a small number of categories, and the categories are
what policy is made of: a routing NACK is the gateway answering, so the
link is fine and only this request failed; a dead socket is the link,
so everything in flight is lost and we reconnect; a negative response is
the ECU answering, so its code is data worth keeping; a decode failure
is our mapping, not the car. Until now some of those distinctions were
made by looking at a class name's suffix or grepping "NRC" out of an
exception's text. This module makes them inheritance instead, so the
executor, the validation tool and the diagnostics view all decide the
same way by `isinstance`, and carry the same structured detail.

Layering: this file imports nothing from the rest of the package, so both
`bmwdiag.protocol` (the transport seam) and `bmwdiag.mapping.errors`
(decode failures) can inherit from it without a cycle. The public names
are re-exported by `bmwdiag.protocol`; the application's HSFZ exceptions
inherit from these (`live.HsfzNack` is a `RoutingNack`, ...), which is
how HSFZ detail stays out of the generic code: the executor never learns
what a gateway is, only that the answer came from before the ECU.

Each class carries three class-level facts the policy code reads:

  kind      the stable fault kind recorded per request and shipped to
            the lake (`telemetry.channel_errors.kind`). These strings
            are history - renaming one splits a dataset - so they are
            fixed here and new distinctions go into `detail()` instead.
  scope     "request" - this request failed, the link is usable; or
            "link" - nothing more can be sent on this connection.
  answered  True when the far side (ECU or gateway) demonstrably
            replied. A fault with `answered` is evidence the link is
            alive and must not count toward a "link dead" budget.

`detail()` returns a small JSON-safe dict of structured fields (service,
NRC, target address, elapsed time...) - the part that used to live only
in the message text.
"""

from typing import Any, Dict, Optional, Tuple

__all__ = [
    "DiagnosticError",
    "TransportError",
    "LinkError",
    "RoutingNack",
    "RequestTimeout",
    "PendingTimeout",
    "NegativeResponse",
    "DecodeFailure",
    "ResponseMismatch",
    "NRC_NAMES",
    "nrc_name",
    "classify_exception",
]


# ISO 14229-1 (UDS) negative response codes. Standard names, not vehicle
# data: the ECU sends the number, the name is the specification's. Codes
# outside this table render as "unknown" - never guessed.
NRC_NAMES: Dict[int, str] = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x14: "responseTooLong",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x25: "noResponseFromSubnetComponent",
    0x26: "failurePreventsExecutionOfRequestedAction",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted",
    0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter",
    0x78: "requestCorrectlyReceivedResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
    0x81: "rpmTooHigh",
    0x82: "rpmTooLow",
    0x83: "engineIsRunning",
    0x84: "engineIsNotRunning",
    0x85: "engineRunTimeTooLow",
    0x86: "temperatureTooHigh",
    0x87: "temperatureTooLow",
    0x88: "vehicleSpeedTooHigh",
    0x89: "vehicleSpeedTooLow",
    0x8A: "throttlePedalTooHigh",
    0x8B: "throttlePedalTooLow",
    0x8C: "transmissionRangeNotInNeutral",
    0x8D: "transmissionRangeNotInGear",
    0x8F: "brakeSwitchesNotClosed",
    0x90: "shifterLeverNotInPark",
    0x91: "torqueConverterClutchLocked",
    0x92: "voltageTooHigh",
    0x93: "voltageTooLow",
}


def nrc_name(nrc: int) -> str:
    """The ISO 14229 name of a negative response code, or "unknown"."""
    return NRC_NAMES.get(nrc, "unknown")


class DiagnosticError(Exception):
    """
    Base of every classified diagnostic failure.

    Subclasses set `kind`, `scope` and `answered`; instances may override
    `detail()` to expose their structured fields. The base is deliberately
    conservative: an unclassified diagnostic error is treated as a link
    fault (scope "link", nothing answered), which is what an unknown
    failure always meant to the executor.
    """

    kind: str = "other"
    scope: str = "link"
    answered: bool = False

    def detail(self) -> Dict[str, Any]:
        """Structured, JSON-safe fields describing this failure."""
        return {}


class TransportError(DiagnosticError):
    """The failure is between us and the ECU: link, gateway or silence."""


class LinkError(TransportError, ConnectionError):
    """
    The connection itself is gone: reset, closed by the gateway, or never
    established. Everything in flight is lost; the only recovery is a
    reconnect. Also a `ConnectionError`, so code that already handles
    socket-level resets handles this identically.
    """

    kind = "transport_link"
    scope = "link"
    answered = False

    def __init__(self, message: str = "connection lost", reason: str = "lost"):
        self.reason = reason
        super().__init__(message)

    def detail(self) -> Dict[str, Any]:
        return {"reason": self.reason}


class RoutingNack(TransportError):
    """
    The gateway refused to forward the request to its target. The gateway
    answered - the link is healthy - but this ECU is unreachable through
    it (asleep, absent, or not on the routing table).
    """

    kind = "transport_nack"
    scope = "request"
    answered = True

    def __init__(
        self,
        target: Optional[int] = None,
        message: Optional[str] = None,
        control: Optional[int] = None,
    ):
        self.target = target
        self.control = control
        if message is None:
            if target is None:
                message = "gateway will not route this request"
            else:
                message = f"gateway will not route to 0x{target:02X}"
        super().__init__(message)

    def detail(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"target": self.target}
        if self.control is not None:
            out["control"] = self.control
        return out


class RequestTimeout(TransportError, TimeoutError):
    """
    No answer to THIS request within its deadline. The link may be
    perfectly healthy - the ECU may be slow, asleep, or not answering this
    one identifier - so a single timeout is a request fault; the executor's
    fault budget decides when repeated silence means the link is dead.
    `pending` counts the NRC 0x78 "response pending" holds seen before
    the deadline ran out: a pending timeout is the ECU saying "wait" and
    then never delivering, which is a different symptom from silence.
    """

    kind = "transport_timeout"
    scope = "request"
    answered = False

    def __init__(
        self,
        message: str = "no response within the deadline",
        elapsed: Optional[float] = None,
        pending: int = 0,
        expected: Optional[str] = None,
    ):
        self.elapsed = elapsed
        self.pending = pending
        self.expected = expected
        super().__init__(message)

    def detail(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"pending": self.pending}
        if self.elapsed is not None:
            out["elapsed_ms"] = int(round(self.elapsed * 1000))
        if self.expected is not None:
            out["expected"] = self.expected
        return out


class PendingTimeout(RequestTimeout):
    """
    The ECU said "wait" (NRC 0x78, `pending` times) and then never
    delivered before the absolute deadline. Its own kind, because "kept
    promising, then silent" and "silent" are different diagnoses of the
    same channel - and `answered`, because a 0x78 IS the ECU replying:
    the link is demonstrably alive and this must not count toward
    concluding it is dead.
    """

    kind = "pending_timeout"
    answered = True


class NegativeResponse(DiagnosticError):
    """
    The ECU answered, and said no: a UDS/KWP `7F <service> <NRC>`.

    The service byte, the code and the raw response are fields, not
    prose, so the fault recorder groups on the code, an identity probe
    reports "NRC 0x31 to 22 F3 03" instead of "failed", and a validation
    artifact records the number. The message keeps its historical shape
    ("negative response to 0x22: NRC 0x31") because logs and reports
    already read that way.
    """

    kind = "negative_response"
    scope = "request"
    answered = True

    def __init__(
        self,
        service: int,
        nrc: int,
        message: Optional[str] = None,
        raw: Optional[bytes] = None,
    ):
        self.service = service
        self.nrc = nrc
        self.raw = bytes(raw) if raw is not None else None
        super().__init__(
            message or f"negative response to 0x{service:02X}: NRC 0x{nrc:02X}"
        )

    @property
    def nrc_name(self) -> str:
        return nrc_name(self.nrc)

    @property
    def pending(self) -> bool:
        """NRC 0x78: not a refusal, a request to wait."""
        return self.nrc == 0x78

    def detail(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "service": self.service,
            "nrc": self.nrc,
            "nrc_name": self.nrc_name,
        }
        if self.raw is not None:
            out["raw"] = self.raw.hex(" ")
        return out


class DecodeFailure(DiagnosticError):
    """
    The car answered and the answer was delivered; turning it into a
    value failed. This is a mapping problem (or an ECU sending a shape
    the mapping did not anticipate), never a transport one - the link is
    fine and nothing needs resending.
    """

    kind = "decode"
    scope = "request"
    answered = True


class ResponseMismatch(DecodeFailure):
    """
    The response did not have the declared shape: wrong service echo,
    wrong identifier, too short. Recorded under the `decode` kind (the
    lake already groups it there); the detail says which it was.
    """

    def detail(self) -> Dict[str, Any]:
        return {"category": "response_mismatch"}


def classify_exception(exc: BaseException) -> Tuple[str, str, bool]:
    """
    (kind, scope, answered) for ANY exception, including the stdlib ones
    a raw socket raises before a transport had the chance to classify
    them. The taxonomy wins when the exception is part of it; otherwise a
    bare ConnectionError is the link and a bare TimeoutError is silence.
    """
    if isinstance(exc, DiagnosticError):
        return exc.kind, exc.scope, exc.answered
    if isinstance(exc, (ConnectionError, BrokenPipeError)):
        return "transport_link", "link", False
    if isinstance(exc, TimeoutError):
        return "transport_timeout", "request", False
    return "other", "link", False
