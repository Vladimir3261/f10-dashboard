"""
The seam between the mapping engine and whatever moves bytes.

Keeping this interface tiny is deliberate: the mapping subsystem must be
testable, and diffable against captured traffic, without a socket, a
gateway or a car anywhere in the picture.
"""

from .errors import (
    DiagnosticError,
    LinkError,
    NegativeResponse,
    RequestTimeout,
    ResponseMismatch,
    RoutingNack,
    TransportError,
)
from .request import (
    DecodedResponse,
    DiagnosticRequest,
    DiagnosticTransport,
    ObdPidReader,
    build_request,
)
from .safety import (
    ObservationalTransport,
    UnsafePayload,
    assert_observational,
)

__all__ = [
    "DecodedResponse",
    "DiagnosticError",
    "DiagnosticRequest",
    "DiagnosticTransport",
    "LinkError",
    "NegativeResponse",
    "ObdPidReader",
    "ObservationalTransport",
    "RequestTimeout",
    "ResponseMismatch",
    "RoutingNack",
    "TransportError",
    "UnsafePayload",
    "assert_observational",
    "build_request",
]
