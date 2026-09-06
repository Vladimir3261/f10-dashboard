"""
UDS 0x19 ReadDTCInformation - request builders and reply parsers.

Fault context is read ON DEMAND, never polled (issue #15, domain 7):
a stored code changes a few times per year, the readout is one exchange
per subfunction, and the answer is a list, not a time series - the lake's
narrow `samples` table is the wrong shape for it. What the polled set
needs is only "was a fault stored during this run", which the standard
Mode 01 PID 0x01 gives as one byte (mappings/candidates/obd/
engine_sae_extra.yaml). This module is the read side of the on-demand
path; `tools/dtc.py` is the operator command that uses it.

Everything here is builders and parsers over `bytes`; no socket is ever
opened. Every builder emits service 0x19 and only 0x19 - the
observational allowlist (`bmwdiag.protocol.safety`) admits 0x19 and
refuses 0x14 ClearDiagnosticInformation, and `tests/test_dtc.py` pins
both facts so a future subfunction cannot quietly become a clear.

Code numbering is BMW's: the three DTC bytes are a 16-bit BMW fault
number followed by the failure-type byte (FTB). They are reported as
such, not forced into SAE Pxxxx form - that translation would be an
inference the wire does not support.

Snapshot (0x04) and extended-data (0x06) replies are parsed only as far
as the standard fixes their layout: DTC + status, then record number(s).
The identifiers inside a snapshot record and their lengths are ECU-
specific (the DIDs are the same F-series sources the d72 mappings read,
but the standard does not say which or how long), so the body is kept as
raw bytes for the artifact rather than guessed at.
"""

from typing import List, NamedTuple, Optional, Sequence, Tuple

from bmwdiag.protocol.safety import assert_observational

__all__ = [
    "SERVICE",
    "POSITIVE",
    "SUBFN_NUMBER_BY_STATUS",
    "SUBFN_BY_STATUS_MASK",
    "SUBFN_SNAPSHOT_BY_DTC",
    "SUBFN_EXTENDED_BY_DTC",
    "SUBFN_SUPPORTED",
    "STATUS_BITS",
    "MASK_ALL",
    "MASK_CONFIRMED",
    "MASK_PENDING_OR_CONFIRMED",
    "Dtc",
    "DtcReport",
    "DtcCount",
    "DtcDetail",
    "DtcParseError",
    "severity",
    "status_flags",
    "report_by_status_mask",
    "report_number_by_status_mask",
    "snapshot_by_dtc",
    "extended_data_by_dtc",
    "supported_dtcs",
    "parse_report",
    "parse_number",
    "parse_detail",
]

SERVICE = 0x19
POSITIVE = 0x59

#: ISO 14229-1 subfunctions used here. All are reads.
SUBFN_NUMBER_BY_STATUS = 0x01     # reportNumberOfDTCByStatusMask
SUBFN_BY_STATUS_MASK = 0x02       # reportDTCByStatusMask
SUBFN_SNAPSHOT_BY_DTC = 0x04      # reportDTCSnapshotRecordByDTCNumber
SUBFN_EXTENDED_BY_DTC = 0x06      # reportDTCExtDataRecordByDTCNumber
SUBFN_SUPPORTED = 0x0A            # reportSupportedDTC

#: ISO 14229-1 DTC status byte.
STATUS_BITS: Tuple[Tuple[int, str], ...] = (
    (0x01, "testFailed"),
    (0x02, "failedThisCycle"),
    (0x04, "pending"),
    (0x08, "confirmed"),
    (0x10, "notCompletedSinceClear"),
    (0x20, "failedSinceClear"),
    (0x40, "notCompletedThisCycle"),
    (0x80, "warningIndicator"),
)

MASK_ALL = 0xFF
MASK_CONFIRMED = 0x08
#: What ISTA's "Fehlerspeicher lesen" asks for by default on this
#: family: FS_LESEN in the d72n47a0 table sends `19 02 0C` (pending +
#: confirmed). Quoted from the same pinned table the candidates cite.
MASK_PENDING_OR_CONFIRMED = 0x0C


class DtcParseError(ValueError):
    """The reply is not a well-formed 0x59 response for the subfunction."""


class Dtc(NamedTuple):
    code: int        # 24-bit as sent: BMW fault number << 8 | FTB
    status: int      # ISO 14229 status byte

    @property
    def fault(self) -> int:
        """The 16-bit BMW fault number."""
        return self.code >> 8

    @property
    def ftb(self) -> int:
        """The failure-type byte."""
        return self.code & 0xFF

    @property
    def severity(self) -> str:
        return severity(self.status)

    @property
    def flags(self) -> List[str]:
        return status_flags(self.status)

    @property
    def text(self) -> str:
        """`0x1234/0x56 stored [confirmed failedSinceClear]`."""
        return (f"0x{self.fault:04X}/0x{self.ftb:02X} {self.severity} "
                f"[{' '.join(self.flags) or '-'}]")


class DtcReport(NamedTuple):
    subfunction: int
    availability_mask: int   # which status bits this ECU implements
    dtcs: Tuple[Dtc, ...]


class DtcCount(NamedTuple):
    availability_mask: int
    format_identifier: int   # 0x00 ISO15031-6, 0x01 ISO14229-1, 0x02 SAE J1939-73, 0x03 ISO11992-4
    count: int


class DtcDetail(NamedTuple):
    """A 0x04 / 0x06 reply: header parsed, body kept raw (see module doc)."""
    subfunction: int
    dtc: Dtc
    record_number: Optional[int]   # first record number, if any body
    body: bytes                    # everything after the status byte


def severity(status: int) -> str:
    """One word for a status byte, in the order a human triages."""
    if status & 0x01:
        return "ACTIVE"
    if status & 0x08:
        return "stored"
    if status & 0x04:
        return "pending"
    if status & 0x20:
        return "historic"
    if status & 0x50 == 0x50:
        return "not-run"
    return "-"


def status_flags(status: int) -> List[str]:
    return [name for bit, name in STATUS_BITS if status & bit]


# -------------------------------------------------------------- builders
#
# Every builder returns the exact payload and runs it through the
# observational gate before handing it back - a builder that produced
# anything but a read would fail here, in the caller's process, with
# nothing sent.


def _built(payload: bytes) -> bytes:
    assert_observational(payload)

    if payload[0] != SERVICE:
        raise AssertionError("dtc builder produced a non-0x19 payload")

    return payload


def report_by_status_mask(mask: int = MASK_ALL) -> bytes:
    """`19 02 <mask>` - every DTC whose status matches any bit of `mask`."""
    return _built(bytes([SERVICE, SUBFN_BY_STATUS_MASK, mask & 0xFF]))


def report_number_by_status_mask(mask: int = MASK_ALL) -> bytes:
    """`19 01 <mask>` - just the count; what Mode 01 PID 0x01 is checked against."""
    return _built(bytes([SERVICE, SUBFN_NUMBER_BY_STATUS, mask & 0xFF]))


def _dtc_bytes(code: int) -> bytes:
    if not 0 <= code <= 0xFFFFFF:
        raise ValueError(f"DTC code out of 24-bit range: {code!r}")

    return code.to_bytes(3, "big")


def snapshot_by_dtc(code: int, record: int = 0xFF) -> bytes:
    """`19 04 <dtc:3> <record>` - freeze-frame data; 0xFF = every record."""
    return _built(bytes([SERVICE, SUBFN_SNAPSHOT_BY_DTC])
                  + _dtc_bytes(code) + bytes([record & 0xFF]))


def extended_data_by_dtc(code: int, record: int = 0xFF) -> bytes:
    """`19 06 <dtc:3> <record>` - occurrence counters, ageing; 0xFF = all."""
    return _built(bytes([SERVICE, SUBFN_EXTENDED_BY_DTC])
                  + _dtc_bytes(code) + bytes([record & 0xFF]))


def supported_dtcs() -> bytes:
    """`19 0A` - every DTC the ECU can raise, with its current status."""
    return _built(bytes([SERVICE, SUBFN_SUPPORTED]))


# --------------------------------------------------------------- parsers


def _positive(resp: Sequence[int], subfunction: Optional[int]) -> bytes:
    data = bytes(resp)

    if len(data) < 2 or data[0] != POSITIVE:
        raise DtcParseError(
            f"not a 0x59 positive response: {data.hex(' ') or '(empty)'}"
        )

    if subfunction is not None and data[1] != subfunction:
        raise DtcParseError(
            f"reply is for subfunction 0x{data[1]:02X}, "
            f"expected 0x{subfunction:02X}"
        )

    return data


def parse_report(resp: Sequence[int],
                 subfunction: int = SUBFN_BY_STATUS_MASK) -> DtcReport:
    """
    `59 <sub> <availabilityMask> (<dtc:3> <status>)*` for subfunctions
    0x02 and 0x0A. A trailing partial record is an error, not ignored -
    a truncated frame must not read as "fewer faults".
    """
    data = _positive(resp, subfunction)

    if len(data) < 3:
        raise DtcParseError("reply too short for an availability mask")

    body = data[3:]

    if len(body) % 4:
        raise DtcParseError(
            f"DTC list is {len(body)} bytes, not a multiple of 4"
        )

    dtcs = tuple(
        Dtc(int.from_bytes(body[i:i + 3], "big"), body[i + 3])
        for i in range(0, len(body), 4)
    )

    return DtcReport(data[1], data[2], dtcs)


def parse_number(resp: Sequence[int]) -> DtcCount:
    """`59 01 <availabilityMask> <formatIdentifier> <count:2>`."""
    data = _positive(resp, SUBFN_NUMBER_BY_STATUS)

    if len(data) != 6:
        raise DtcParseError(
            f"reportNumberOfDTC reply is {len(data)} bytes, expected 6"
        )

    return DtcCount(data[2], data[3], int.from_bytes(data[4:6], "big"))


def parse_detail(resp: Sequence[int], subfunction: int) -> DtcDetail:
    """
    `59 <04|06> <dtc:3> <status> [<recordNumber> ...]`. Only the fixed
    header is interpreted; `body` is whatever followed the status byte.
    """
    if subfunction not in (SUBFN_SNAPSHOT_BY_DTC, SUBFN_EXTENDED_BY_DTC):
        raise DtcParseError(
            f"subfunction 0x{subfunction:02X} is not a per-DTC detail read"
        )

    data = _positive(resp, subfunction)

    if len(data) < 6:
        raise DtcParseError("detail reply too short for DTC + status")

    dtc = Dtc(int.from_bytes(data[2:5], "big"), data[5])
    body = data[6:]

    return DtcDetail(data[1], dtc, body[0] if body else None, body)
