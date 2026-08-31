"""
The observational safety gate - one policy, one implementation.

This runtime observes a real car. The property the whole repository
promises is that nothing state-changing can ever reach the vehicle: no
writes, no actuator control, no session changes, no flashing, no fault
clearing. That promise is only worth something if it is enforced at a
choke point every outgoing frame must pass, rather than depending on
each call site remembering to check.

Until this module existed the hard gate lived only in
tools/validate_candidate.py, so the supervised validation tool was
protected while the normal in-car runtime was not - and mappings carry
explicit payloads (`setup:` frames, `payload:`, raw protocol), so an
accidentally edited --extra-mappings file could have put a write service
on the wire. This matters more now that the repository is public and
mapping data may be edited by contributors or by coding agents.

Terminology is deliberate: **observational**, not "read-only". Service
0x2C (DynamicallyDefineDataIdentifier) reconfigures which sources a
dynamic DID points at - session-scoped ECU state, re-armed on every
read, but state nonetheless. Calling that strictly read-only would be
comfortable rather than accurate.

The gate is transport-agnostic on purpose: a future CAN or serial
transport wraps itself in `ObservationalTransport` (or calls
`assert_observational` first thing in its `request`) and inherits the
same policy with no new decisions to make.
"""

from typing import Optional

__all__ = [
    "OBSERVATIONAL_SERVICES",
    "DDD_SUBFUNCTIONS",
    "WRITE_SERVICES",
    "UnsafePayload",
    "assert_observational",
    "ObservationalTransport",
]

#: Services the runtime may send, besides the special-cased 0x2C:
#:
#:   0x01 OBD current data                0x09 OBD vehicle information
#:   0x22 ReadDataByIdentifier           0x19 ReadDTCInformation
#:   0x3E TesterPresent (not currently sent; permitted intentionally so
#:        a future keep-alive does not need a policy change)
OBSERVATIONAL_SERVICES = frozenset({0x01, 0x09, 0x22, 0x19, 0x3E})

#: 0x2C is allowed only with these subfunctions. 0x01
#: (defineByIdentifier), 0x02 (defineByMemoryAddress) and 0x03
#: (clearDynamicallyDefinedDID) define/clear a *tester-local* DID so it
#: can be read with 0x22; they write nothing persistent in the ECU.
#: 0x10 is the DDE7 KWP local-identifier read, which is a read on those
#: ECUs despite sharing the service byte.
DDD_SUBFUNCTIONS = frozenset({0x01, 0x02, 0x03, 0x10})

#: Services that must NEVER go on the wire, named so a refusal can say
#: exactly what was about to happen rather than printing a bare number.
WRITE_SERVICES = {
    0x2E: "WriteDataByIdentifier",
    0x2F: "InputOutputControlByIdentifier",
    0x31: "RoutineControl",
    0x14: "ClearDiagnosticInformation",
    0x27: "SecurityAccess",
    0x10: "DiagnosticSessionControl",   # a mode change; never from here
    0x11: "ECUReset",
    0x34: "RequestDownload",
    0x35: "RequestUpload",
    0x36: "TransferData",
    0x37: "RequestTransferExit",
    0x28: "CommunicationControl",
    0x3D: "WriteMemoryByAddress",
    0x85: "ControlDTCSetting",
}


class UnsafePayload(Exception):
    """A payload is not on the observational allowlist. Nothing was sent."""


def assert_observational(payload: bytes) -> None:
    """
    Raise UnsafePayload unless `payload` is a permitted observational
    request. Anything not explicitly allowed is rejected - unknown
    services fail closed.
    """
    if not payload:
        raise UnsafePayload("empty payload")

    service = payload[0]

    if service in WRITE_SERVICES:
        raise UnsafePayload(
            f"service 0x{service:02X} ({WRITE_SERVICES[service]}) is a "
            "write/control service and is never sent by this runtime"
        )

    if service == 0x2C:
        if len(payload) < 2 or payload[1] not in DDD_SUBFUNCTIONS:
            sub = payload[1] if len(payload) > 1 else None
            raise UnsafePayload(
                f"service 0x2C subfunction "
                f"{('0x%02X' % sub) if sub is not None else '(none)'} is "
                "not a permitted define/clear/read subfunction "
                f"{sorted(hex(s) for s in DDD_SUBFUNCTIONS)}"
            )

        return

    if service not in OBSERVATIONAL_SERVICES:
        raise UnsafePayload(
            f"service 0x{service:02X} is not on the observational "
            f"allowlist "
            f"{sorted(hex(s) for s in OBSERVATIONAL_SERVICES | {0x2C})}"
        )


class ObservationalTransport:
    """
    Wraps any DiagnosticTransport and gates every frame.

    The wrapped transport is only reached AFTER the payload passes the
    allowlist, so an unsafe frame produces an UnsafePayload with zero
    bytes sent - there is no window where the frame is on the wire
    before the check happens.
    """

    def __init__(self, inner):
        self.inner = inner

    def request(self, payload: bytes, *, dst: int,
                timeout: Optional[float] = None) -> bytes:
        assert_observational(bytes(payload))

        return self.inner.request(payload, dst=dst, timeout=timeout)
