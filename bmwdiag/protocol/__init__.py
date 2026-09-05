"""
The seam between the mapping engine and whatever moves bytes.

Keeping this interface tiny is deliberate: the mapping subsystem must be
testable, and diffable against captured traffic, without a socket, a
gateway or a car anywhere in the picture.
"""

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
