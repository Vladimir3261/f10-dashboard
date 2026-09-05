"""
Structured diagnostic failures - one vocabulary for every layer.

A request can fail in ways that call for opposite reactions, and until
these types existed the runtime could only tell them apart by reading
exception prose: the executor matched class names ending in ``Nack``, the
validation tool searched ``str(exc)`` for ``"NRC"``, and the reconnect
path looked for the word ``"closed"``. A negative UDS response - the ECU
answering, in order to say no - was a generic ``HsfzError`` and therefore
counted as a dead link, which tore the session down and split the drive
into a new run.

The taxonomy separates what actually happened:

    DiagnosticError                        kind
      TransportError
        LinkError        (ConnectionError) transport_link     socket gone
        RoutingNack                        transport_nack     gateway refused
        RequestTimeout   (TimeoutError)    transport_timeout  nobody answered
      NegativeResponse                     negative_response  7F <sid> <nrc>
      ResponseMismatch                     response_mismatch  wrong shape
      (DecodeError lives in mapping.errors) decode            bytes -> value

Two properties matter:

* **The category is the type.** Policy (skip one exchange, rest a
  request, reconnect the link) tests ``isinstance``; storage and the
  diagnostics view read ``.kind``. Nothing inspects text.
* **The evidence is data.** A negative response carries its service
  byte, NRC and raw bytes as fields; a routing NACK carries the target;
  a link error carries why. ``detail()`` returns those as a plain dict so
  a validation artifact can record ``nrc: 49`` rather than a sentence.

Protocol-neutral by construction: nothing here knows about HSFZ framing,
sockets or BMW. ``LinkError`` and ``RequestTimeout`` also inherit the
standard-library categories they refine, so ``except ConnectionError``
and ``except TimeoutError`` keep meaning what they always meant.
"""

from typing import Any, Dict, Optional

__all__ = [
    "DiagnosticError",
    "TransportError",
    "LinkError",
    "RoutingNack",
    "RequestTimeout",
    "NegativeResponse",
    "ResponseMismatch",
    "NRC_NAMES",
    "nrc_name",
]


#: ISO 14229-1 negative response codes, by number. Standard UDS, not
#: vehicle data. Only the ones a read-only tester can plausibly receive;
#: anything else reports as ``unknown`` and keeps its number.
NRC_NAMES: Dict[int, str] = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x14: "responseTooLong",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x78: "requestCorrectlyReceivedResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}


def nrc_name(nrc: int) -> str:
    return NRC_NAMES.get(int(nrc), "unknown")


class DiagnosticError(Exception):
    """
    Base of every classified diagnostic failure.

    ``kind`` is the stable name storage groups by; ``detail()`` is the
    structured evidence. Subclasses set both. The base itself is the
    "something diagnostic went wrong, unclassified" case - it exists so
    an application can subclass it for its own failures (discovery found
    no ECU, say) and still be recognised as a diagnostic error rather
    than a programming one.
    """

    kind: str = "other"

    def detail(self) -> Dict[str, Any]:
        return {}


class TransportError(DiagnosticError):
    """The bytes did not get to the ECU, or nothing came back."""

    kind = "transport"


class LinkError(TransportError, ConnectionError):
    """
    The link itself is gone: socket closed, reset, never connected, or
    the byte stream desynchronised. Every later request would fail the
    same way, so this is the one category that means *reconnect*.

    ``reason`` says which: ``closed`` (orderly EOF from the gateway),
    ``reset`` (RST - on this car, another tool taking the ZGW's single
    HSFZ slot), ``broken_pipe``, ``refused``, ``not_connected``,
    ``framing`` (an impossible length in the stream), or ``socket`` for
    any other OS-level failure.
    """

    kind = "transport_link"

    def __init__(self, message: str, reason: str = "socket"):
        super().__init__(message)
        self.reason = reason

    def __reduce__(self):
        return (type(self), (str(self), self.reason))

    def detail(self) -> Dict[str, Any]:
        return {"reason": self.reason}


class RoutingNack(TransportError):
    """
    The gateway answered in order to refuse one target address.

    Definitive and cheap - and positive evidence that the link is alive,
    which is why it must never count towards concluding the link is dead.
    """

    kind = "transport_nack"

    def __init__(
        self,
        target: int,
        control: Optional[int] = None,
        message: Optional[str] = None,
    ):
        self.target = int(target)
        self.control = None if control is None else int(control)
        super().__init__(
            message if message is not None
            else f"gateway will not route to 0x{self.target:02X}"
        )

    def __reduce__(self):
        return (type(self), (self.target, self.control, str(self)))

    def detail(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"target": self.target}

        if self.control is not None:
            out["control"] = self.control

        return out


class RequestTimeout(TransportError, TimeoutError):
    """
    One request got no answer within its deadline. Says nothing about
    the link - a sleeping ECU and a dead socket look identical from one
    exchange, which is what the executor's fault budget is for.
    """

    kind = "transport_timeout"

    def __init__(
        self,
        target: Optional[int] = None,
        timeout: Optional[float] = None,
        message: Optional[str] = None,
    ):
        self.target = None if target is None else int(target)
        self.timeout = None if timeout is None else float(timeout)

        if message is None:
            where = "" if self.target is None else f" from 0x{self.target:02X}"
            budget = "" if self.timeout is None else f" in {self.timeout:g}s"
            message = f"no response{where}{budget}"

        super().__init__(message)

    def __reduce__(self):
        return (type(self), (self.target, self.timeout, str(self)))

    def detail(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}

        if self.target is not None:
            out["target"] = self.target

        if self.timeout is not None:
            out["timeout_s"] = self.timeout

        return out


class NegativeResponse(DiagnosticError):
    """
    The ECU answered ``7F <service> <nrc>``.

    An answer, not silence: the link works, the ECU is awake, and it
    declined this particular request. Carries the service byte, the NRC
    and the raw response as fields - ``NRC 0x31`` is a value to display
    and store, never a substring to search for.
    """

    kind = "negative_response"

    def __init__(
        self,
        service: int,
        nrc: int,
        raw: bytes = b"",
        target: Optional[int] = None,
    ):
        self.service = int(service) & 0xFF
        self.nrc = int(nrc) & 0xFF
        self.raw = bytes(raw)
        self.target = None if target is None else int(target)
        super().__init__(
            f"negative response to 0x{self.service:02X}: "
            f"NRC 0x{self.nrc:02X} ({self.name})"
        )

    def __reduce__(self):
        return (type(self), (self.service, self.nrc, self.raw, self.target))

    @property
    def name(self) -> str:
        return nrc_name(self.nrc)

    @property
    def nrc_hex(self) -> str:
        return f"0x{self.nrc:02X}"

    def detail(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "service": self.service,
            "nrc": self.nrc,
            "nrc_hex": self.nrc_hex,
            "nrc_name": self.name,
            "raw": self.raw.hex(" "),
        }

        if self.target is not None:
            out["target"] = self.target

        return out


class ResponseMismatch(DiagnosticError):
    """
    Something came back, and it is not the shape this request expects:
    wrong service echo, too short, an OBD reply missing a PID it was
    asked for. An answer, so the link is alive; not one that can be
    decoded, so the request failed.
    """

    kind = "response_mismatch"

    def __init__(
        self,
        message: str,
        raw: bytes = b"",
        expected: Optional[str] = None,
        target: Optional[int] = None,
    ):
        self.raw = bytes(raw)
        self.expected = expected
        self.target = None if target is None else int(target)
        super().__init__(message)

    def __reduce__(self):
        return (
            type(self), (str(self), self.raw, self.expected, self.target)
        )

    def detail(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"raw": self.raw.hex(" ")}

        if self.expected is not None:
            out["expected"] = self.expected

        if self.target is not None:
            out["target"] = self.target

        return out
