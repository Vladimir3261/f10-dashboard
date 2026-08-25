"""
The seam between the mapping engine and whatever moves bytes.

Keeping this interface tiny is deliberate: the mapping subsystem must be
testable, and diffable against captured traffic, without a socket, a
gateway or a car anywhere in the picture.
"""

from .request import (
    DecodedResponse,
    DiagnosticRequest,
    DiagnosticTransport,
    ObdPidReader,
    build_request,
)

__all__ = [
    "DecodedResponse",
    "DiagnosticRequest",
    "DiagnosticTransport",
    "ObdPidReader",
    "build_request",
]
