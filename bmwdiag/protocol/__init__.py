"""
The seam between the mapping engine and whatever moves bytes.

Keeping this interface tiny is deliberate: the mapping subsystem must be
testable, and diffable against captured traffic, without a socket, a
gateway or a car anywhere in the picture.
"""

# Import order, not a dependency: `request` needs `mapping.errors`, and
# importing `bmwdiag.mapping` first runs `mapping.execute`, which needs
# `protocol.request` back. Loading the mapping package before this one's
# own modules lets either package be imported first.
from .. import mapping as _mapping  # noqa: F401  (import-order guard)
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
    NegativeResponse,
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
    "DiagnosticRequest",
    "DiagnosticTransport",
    "NegativeResponse",
    "ObdPidReader",
    "ObservationalTransport",
    "ResponseExpectation",
    "UnsafePayload",
    "assert_observational",
    "build_request",
    "classify",
    "declared_response",
    "expected_response",
]
