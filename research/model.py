"""
The normalized research evidence model.

A research record stores what a source CLAIMS, at whatever completeness
the source actually supports. It is deliberately allowed to be partial:
an identifier with a scale but no request sequence is valid research
data even though it can never be polled. Forcing it into an executable
mapping would mean inventing the missing bytes, which is the one thing
this pipeline exists to prevent.

Every value is labelled with how it was obtained (`FACT_LABELS`), every
record names its manifest source, and nothing is merged across variants:
`d71`, `d72` and `d73` records coexist under distinct record ids.
"""

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "EVIDENCE_TIERS",
    "VERIFICATION_STATES",
    "SAFETY_CLASSES",
    "FACT_LABELS",
    "RECORD_TYPES",
    "REQUEST_COMPLETENESS",
    "MATCHER_STRATEGIES",
    "EVIDENCE_RELATIONSHIPS",
    "ResearchError",
    "ResearchRecord",
    "record_to_json",
    "records_to_jsonl",
    "validate_record",
]

#: Evidence tiers. Tier D may exist as a research lead but must never
#: generate an executable mapping (enforced by research.gate).
EVIDENCE_TIERS = ("A", "B", "C", "D")

#: Verification lifecycle. Only `locally_verified` means verified on our
#: F10; `externally_verified` means the SOURCE has credible on-car or
#: wire evidence for the SOURCE vehicle.
VERIFICATION_STATES = (
    "discovered",
    "candidate",
    "externally_verified",
    "cross_source_confirmed",
    "locally_verified",
    "rejected",
)

#: Safety classification. Only `read_only_telemetry` may enter automatic
#: polling; `unknown` is excluded from executable mappings entirely.
SAFETY_CLASSES = (
    "read_only_telemetry",
    "read_only_telemetry_candidate",
    "diagnostic_read",
    "service_operation",
    "write_or_control",
    "unknown",
)

#: How a stored value was obtained. `wire_observation` beats everything;
#: `speculation` and bare `source_claim` never justify a request byte.
FACT_LABELS = (
    "wire_observation",
    "sgbd_derived",
    "source_claim",
    "inference",
    "speculation",
)

RECORD_TYPES = (
    "signal_definition",
    "request_evidence",
    "job_definition",
    "raw_exchange",
)

REQUEST_COMPLETENESS = ("complete", "incomplete", "unknown")

#: Response-matching strategies a source may describe. They all map onto
#: the runtime's declared-prefix mechanism; the names record what a
#: source actually established about the reply shape.
MATCHER_STRATEGIES = (
    "echo_full",
    "service_and_identifier",
    "service_sub_only",
    "service_only",
    "fixed_prefix",
    "length_only_with_source_guard",
)

#: How two pieces of evidence relate. Two exports of the same BMW table
#: are `same_primary_source` - useful parser validation, NOT independent
#: confirmation.
EVIDENCE_RELATIONSHIPS = (
    "derived_from",
    "copied_from",
    "same_primary_source",
    "independent",
)


class ResearchError(Exception):
    """A research record or manifest is malformed."""


@dataclass
class ResearchRecord:
    """
    One normalized claim from one source.

    `data`, `request`, `applicability` and `source` are free-shape dicts
    whose conventions are enforced by `validate_record` - the flexibility
    is deliberate, because sources disagree about what they can state.
    Unknown stays the string "unknown", never a guessed value.
    """

    record_id: str
    record_type: str
    source_id: str                      # manifest key of the source
    evidence_tier: str
    verification: str
    safety: str
    source: Dict[str, Any] = field(default_factory=dict)
    applicability: Dict[str, Any] = field(default_factory=dict)
    data: Dict[str, Any] = field(default_factory=dict)
    request: Dict[str, Any] = field(default_factory=dict)
    normalized_signal: Optional[str] = None
    fact_labels: Tuple[str, ...] = ()
    license: Dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    category: str = "unknown"           # import category, see importers

    def as_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["fact_labels"] = list(self.fact_labels)
        return out


def validate_record(record: ResearchRecord) -> List[str]:
    """
    Return every problem with a record, empty when it is well-formed.

    Well-formed does NOT mean executable - a record with an unknown
    request is fine. It means: vocabulary values are legal, provenance
    is present, and nothing claims more than its labels support.
    """
    problems: List[str] = []

    if not record.record_id:
        problems.append("record_id is empty")

    if record.record_type not in RECORD_TYPES:
        problems.append(f"unknown record_type {record.record_type!r}")

    if not record.source_id:
        problems.append("source_id is empty (every record needs provenance)")

    if record.evidence_tier not in EVIDENCE_TIERS:
        problems.append(f"unknown evidence tier {record.evidence_tier!r}")

    if record.verification not in VERIFICATION_STATES:
        problems.append(f"unknown verification state {record.verification!r}")

    if record.safety not in SAFETY_CLASSES:
        problems.append(f"unknown safety class {record.safety!r}")

    for label in record.fact_labels:
        if label not in FACT_LABELS:
            problems.append(f"unknown fact label {label!r}")

    if "source_license" not in record.license:
        problems.append("license.source_license is missing (use 'unknown')")

    completeness = record.request.get("completeness")

    if record.record_type == "signal_definition" and completeness not in (
        REQUEST_COMPLETENESS
    ):
        problems.append(
            f"request.completeness must be one of {REQUEST_COMPLETENESS}, "
            f"got {completeness!r}"
        )

    matcher = record.request.get("matcher")

    if matcher is not None and matcher not in MATCHER_STRATEGIES:
        problems.append(f"unknown matcher strategy {matcher!r}")

    return problems


def record_to_json(record: ResearchRecord) -> str:
    """One record as a stable, key-sorted JSON line."""
    return json.dumps(record.as_dict(), sort_keys=True, ensure_ascii=False)


def records_to_jsonl(records: List[ResearchRecord]) -> str:
    """
    Records as deterministic JSONL, sorted by record id.

    Sorting is what makes a re-import diffable: the same sources always
    produce the identical file, byte for byte.
    """
    lines = sorted(record_to_json(r) for r in records)
    return "\n".join(lines) + ("\n" if lines else "")
