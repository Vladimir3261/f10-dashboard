"""
The executable-candidate gate.

A research record may become a candidate runtime mapping only when every
fact a poll loop would need is actually KNOWN - sourced, not inferred
from identifier shape or filled in from model knowledge. Anything less
stays in normalized research storage, which is a perfectly good place
for it to live.

The gate returns the list of missing requirements rather than a boolean,
because "what would it take to promote this" is the useful question.
"""

from typing import Any, Dict, List

from .model import ResearchRecord

__all__ = ["candidate_gate", "GATE_REQUIREMENTS"]

#: Everything an executable candidate must establish. Kept as data so the
#: reports can enumerate what each partial record still needs.
GATE_REQUIREMENTS = (
    "target",              # ECU target or a valid resolution strategy
    "request",             # complete request or request sequence
    "session",             # required diagnostic session, if any (or "none")
    "matcher",             # expected positive-response behaviour
    "payload_location",    # where the value sits in the reply
    "response_length",     # how long the data is
    "byte_order",          # endianness, established by the source
    "decoder",             # raw type
    "scaling",             # scale and offset (or an enum/map)
    "unit",                # engineering unit (or an established enum)
    "applicability",       # which variant/chassis the source ties this to
    "provenance",          # source id + citation
    "safety",              # read_only_telemetry classification
    "tier",                # evidence tier A-C (D never passes)
    "verification",        # a lifecycle state that is not rejected
    "license",             # source license metadata present
)


def _known(value: Any) -> bool:
    return value not in (None, "", "unknown")


def candidate_gate(record: ResearchRecord) -> List[str]:
    """
    Return the requirements this record does NOT meet (empty = eligible).

    The gate never fills gaps: a missing byte order stays missing even
    when "big-endian is likely". Likelihood is not evidence.
    """
    missing: List[str] = []
    req: Dict[str, Any] = record.request
    data: Dict[str, Any] = record.data

    if record.evidence_tier == "D":
        missing.append("tier")          # untraceable claims never execute

    if record.verification == "rejected":
        missing.append("verification")

    if record.safety not in ("read_only_telemetry", "read_only_telemetry_candidate"):
        missing.append("safety")

    if not _known(req.get("target")):
        missing.append("target")

    if req.get("completeness") != "complete":
        missing.append("request")

    if not _known(req.get("session")):
        missing.append("session")

    if not _known(req.get("matcher")):
        missing.append("matcher")

    if not _known(req.get("payload_location")):
        missing.append("payload_location")

    if not _known(req.get("response_length")):
        missing.append("response_length")

    #
    # A single byte has no byte order; a declared enum map is scaling.
    #
    width = data.get("width_bytes")
    single_byte = isinstance(width, int) and width == 1

    if not single_byte and not _known(data.get("byte_order")):
        missing.append("byte_order")

    if not _known(data.get("raw_type")):
        missing.append("decoder")

    has_scale = _known(data.get("mul")) or _known(data.get("enum"))

    if not has_scale:
        missing.append("scaling")

    if not _known(data.get("unit")) and not _known(data.get("enum")):
        missing.append("unit")

    if not _known(record.applicability.get("sgbd")) and not _known(
        record.applicability.get("source_ecu")
    ):
        missing.append("applicability")

    if not record.source_id or not record.source:
        missing.append("provenance")

    if not _known(record.license.get("source_license")):
        #
        # 'unknown' IS valid license metadata for research storage, but a
        # candidate must record what was actually determined - even when
        # that determination is "the source has no license".
        #
        if record.license.get("source_license") != "unknown":
            missing.append("license")

    return missing
