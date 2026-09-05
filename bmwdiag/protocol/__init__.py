"""
The seam between the mapping engine and whatever moves bytes.

Keeping this interface tiny is deliberate: the mapping subsystem must be
testable, and diffable against captured traffic, without a socket, a
gateway or a car anywhere in the picture.

The error taxonomy a transport raises into (bmwdiag.errors) is
re-exported here: a transport implementation subclasses `LinkError`,
`RoutingNack`, `RequestTimeout` and `NegativeResponse`, and the executor
decides policy by `isinstance` on those, never by message text.
"""

# Import order, not a dependency: `request` needs `mapping.errors`, and
# importing `bmwdiag.mapping` first runs `mapping.execute`, which needs
# `protocol.request` back. Loading the mapping package before this one's
# own modules lets either package be imported first.
from .. import mapping as _mapping  # noqa: F401  (import-order guard)
from ..errors import (
    DecodeFailure,
    DiagnosticError,
    LinkError,
    NegativeResponse,
    PendingTimeout,
    RequestTimeout,
    ResponseMismatch,
    RoutingNack,
    TransportError,
    classify_exception,
    nrc_name,
)
from .correlate import (
    ResponseExpectation,
    classify,
    declared_response,
    expected_response,
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
    "DecodeFailure",
    "DecodedResponse",
    "DiagnosticError",
    "DiagnosticRequest",
    "DiagnosticTransport",
    "LinkError",
    "NegativeResponse",
    "PendingTimeout",
    "RequestTimeout",
    "ResponseMismatch",
    "RoutingNack",
    "TransportError",
    "ObdPidReader",
    "ObservationalTransport",
    "ResponseExpectation",
    "UnsafePayload",
    "assert_observational",
    "build_request",
    "classify",
    "classify_exception",
    "declared_response",
    "expected_response",
    "nrc_name",
]
