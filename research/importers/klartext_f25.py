"""
Importer: the Klartext F25 d72n47a0 evidence.

Reads the two committed transcriptions under
research/evidence/n47/klartext_f25/ and produces:

  * the pcap-verified ITOEL oil-temperature record - the full dynamic
    F303 sequence with raw response bytes and the SG_FUNKTIONEN-sourced
    `0.01*raw - 100` scale (Tier A, externally_verified on an F25 X3);
  * the two DPF soot records (0x44BE measured / 0x44C1 simulated) read
    on the same car through the same sequence (Tier A), whose exact
    define frames are template applications labelled `inference`;
  * the ITMOT engine-temperature record, which klartext itself marks
    DERIVED FROM DISASSEMBLY pending on-car confirmation (Tier B,
    verification `candidate`);
  * a `request_evidence` record for the sequence template itself, and a
    `raw_exchange` record for the observed static-read REJECTION
    (`22 45 17` -> `7F 22 31`), which is what proves the sequence is
    required on this variant rather than merely sufficient.

Everything applies to SGBD d72n47a0 (F-series N47TUe/N57TUe family,
"F0x, F1x, F2x, F3x" per the SGBD's own ECU comment) - the family our
F10 plausibly belongs to, verified on an F25, and NOT yet verified on
our car.
"""

import os
from typing import List

from bmwdiag.mapping import yamlsubset

from ..model import ResearchRecord

__all__ = ["import_evidence", "EVIDENCE_DIR", "SOURCE_ID"]

SOURCE_ID = "klartext"

EVIDENCE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "evidence", "n47", "klartext_f25",
)

_APPLICABILITY = {
    "engine_family": "N47",
    "sgbd": "d72n47a0",
    "source_chassis": ["F25"],
    "sgbd_declared_usage": "F0x, F1x, F2x, F3x (UDS, MV, FlexRay)",
    "protocol_family": "uds_dynamic_measurement",
    "target_chassis_status": "unverified",
}

#: The §21 normalized names for the four measurements. Ours (inference).
_NORMALIZED = {
    "ITOEL": "engine.oil_temperature",
    "IMRUP": "dpf.soot_mass.measured",
    "IMPAS": "dpf.soot_mass.modelled",
    "ITMOT": "engine.internal_temperature",
}


def _signal_record(
    m: dict,
    sequence: dict,
    license_id: str,
    on_car: bool,
) -> ResearchRecord:
    labels = ["sgbd_derived"]

    if on_car:
        labels.insert(0, "wire_observation")

    if m.get("define_frame_label") == "inference":
        labels.append("inference")

    if m["arg"] in _NORMALIZED and "inference" not in labels:
        labels.append("inference")      # the normalized name is ours

    return ResearchRecord(
        record_id=m["record_id"],
        record_type="signal_definition",
        source_id=SOURCE_ID,
        evidence_tier=m["evidence_tier"],
        verification=m["verification"],
        safety="read_only_telemetry_candidate",
        normalized_signal=_NORMALIZED.get(m["arg"]),
        fact_labels=tuple(labels),
        category=(
            "complete_runtime_candidate" if m["verification"] != "rejected"
            else "rejected"
        ),
        source={
            "source_record": m["arg"],
            "source_identifier": m["source_identifier"],
            "source_result_name": m["result_name"],
            "source_label": m.get("label", ""),
        },
        applicability=dict(_APPLICABILITY),
        data={
            "raw_type": m["raw_type"],
            "width_bytes": 2,
            "byte_order": m["byte_order"],
            "mul": str(m["mul"]),
            "div": str(m["div"]),
            "add": str(m["add"]),
            "unit": m["unit"],
        },
        request={
            "completeness": "complete",
            "target": "engine_ecu_0x12",
            "pattern": "uds_dynamic_f303",
            "sequence": list(sequence["setup"]) + [sequence["poll"]],
            "session": "none",
            "matcher": sequence["matcher"],
            "prefix": sequence["prefix"],
            "payload_location": "after 3-byte 62 F3 03 echo",
            "response_length": 2,
        },
        license={"source_license": license_id},
    )


def import_evidence(directory: str = EVIDENCE_DIR) -> List[ResearchRecord]:
    records: List[ResearchRecord] = []

    # -- the pcap-verified oil-temperature sequence -------------------
    with open(os.path.join(directory, "oil_temp_sequence.yaml"),
              encoding="utf-8") as handle:
        oil = yamlsubset.loads(handle.read(), source="oil_temp_sequence.yaml")

    seq = oil["sequence"]
    meas = oil["source_measurement"]
    dec = oil["decode"]

    records.append(_signal_record(
        {
            "record_id": oil["record_id"],
            "arg": meas["arg"],
            "source_identifier": meas["source_identifier"],
            "result_name": meas["result_name"],
            "raw_type": dec["raw_type"],
            "byte_order": dec["byte_order"],
            "mul": dec["mul"],
            "div": dec["div"],
            "add": dec["add"],
            "unit": dec["unit"],
            "evidence_tier": oil["evidence_tier"],
            "verification": oil["verification"],
        },
        {
            "setup": seq["setup"],
            "poll": seq["poll"],
            "matcher": seq["matcher"],
            "prefix": seq["prefix"],
        },
        oil["citation"]["license"],
        on_car=True,
    ))

    records.append(ResearchRecord(
        record_id=f"{oil['record_id']}.static_rejected",
        record_type="raw_exchange",
        source_id=SOURCE_ID,
        evidence_tier="A",
        verification="externally_verified",
        safety="read_only_telemetry_candidate",
        fact_labels=("wire_observation",),
        category="raw_exchange",
        notes=(
            "the static form of the same read is REJECTED on d72n47a0: "
            "the source id is a define-source, not a readable DID"
        ),
        source=dict(oil["static_read_rejected"]),
        applicability=dict(_APPLICABILITY),
        data={},
        request={
            "completeness": "complete",
            "pattern": "uds_static_did",
            "matcher": "echo_full",
        },
        license={"source_license": oil["citation"]["license"]},
    ))

    # -- the sequence template as request evidence --------------------
    with open(os.path.join(directory, "dpf_and_engine_temp.yaml"),
              encoding="utf-8") as handle:
        dpf = yamlsubset.loads(handle.read(), source="dpf_and_engine_temp.yaml")

    template = dpf["sequence_template"]

    records.append(ResearchRecord(
        record_id="klartext.d72n47a0.sequence_template",
        record_type="request_evidence",
        source_id=SOURCE_ID,
        evidence_tier="A",
        verification="externally_verified",
        safety="read_only_telemetry_candidate",
        fact_labels=("wire_observation", "sgbd_derived"),
        category="request_evidence",
        source={
            "setup": list(template["setup"]),
            "poll": template["poll"],
            "prefix": template["prefix"],
        },
        applicability=dict(_APPLICABILITY),
        data={},
        request={
            "completeness": "complete",
            "pattern": "uds_dynamic_f303",
            "matcher": template["matcher"],
            "session": template["session_requirement"],
        },
        license={"source_license": "AGPL-3.0-or-later"},
    ))

    for m in dpf["measurements"]:
        records.append(_signal_record(
            m,
            {
                "setup": [
                    template["setup"][0],
                    m["define_frame"],
                ],
                "poll": template["poll"],
                "matcher": template["matcher"],
                "prefix": template["prefix"],
            },
            "AGPL-3.0-or-later",
            on_car=(m["verification"] == "externally_verified"),
        ))

    return records
