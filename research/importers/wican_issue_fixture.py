"""
Importer: the WiCAN issue #752 DDE7 exchange fixture.

Reads the committed transcription in
research/evidence/n47/wican_752/exchange.yaml (every byte of which is
quoted from the issue) and produces:

  * one Tier A `raw_exchange` record for the captured 0x0406 read;
  * one Tier A `signal_definition` for DPF soot via `2C 10 04 06`,
    complete enough to pass the candidate gate - the capture establishes
    the request, the non-echoing `6C 10` reply shape, the length, the
    byte order (0x0ED7 read high-first = 3799) and the /100 g scale;
  * Tier C `signal_definition` records for the two identifiers the issue
    CLAIMS but shows no raw frames for (0x03EB, 0x0AF1) - these stay
    incomplete and are labelled source_claim.

The source vehicle is an E90 N47D20C (DDE7N47). Its DDE belongs to the
E-series KWP-heritage family (the D71N47* SGBDs describe
STATUS_MESSWERTBLOCK_LESEN as "KWP2000: $2C DefineDataByLocalIdentifier
$10 RecordLocalIdentifier"). F10 applicability is unverified and stays
that way until proven on the car.
"""

import os
from typing import List, Tuple

from bmwdiag.mapping import yamlsubset

from ..model import ResearchRecord

__all__ = ["import_fixture", "EVIDENCE_PATH", "SOURCE_ID"]

SOURCE_ID = "wican-issue-752"

EVIDENCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "evidence", "n47", "wican_752", "exchange.yaml",
)


def import_fixture(path: str = EVIDENCE_PATH) -> List[ResearchRecord]:
    with open(path, "r", encoding="utf-8") as handle:
        doc = yamlsubset.loads(handle.read(), source=path)

    exchange = doc["exchange"]
    decode = doc["decode"]
    vehicle = doc["vehicle"]
    citation = doc["citation"]

    applicability = {
        "engine_family": "N47",
        "source_engine": vehicle["engine"],
        "source_ecu": vehicle["ecu"],
        "sgbd": "unknown",              # exact DDE7 SGBD variant unresolved
        "source_chassis": ["E90"],
        "protocol_family": "kwp_local_id_over_isotp",
        "target_chassis_status": "unverified",
    }

    records: List[ResearchRecord] = [
        ResearchRecord(
            record_id=f"{doc['record_id']}.exchange",
            record_type="raw_exchange",
            source_id=SOURCE_ID,
            evidence_tier="A",
            verification="externally_verified",
            safety="read_only_telemetry_candidate",
            fact_labels=("wire_observation",),
            category="raw_exchange",
            source={
                "request": exchange["request"],
                "response": exchange["response"],
                "request_can_id": vehicle["request_can_id"],
                "response_can_id": vehicle["response_can_id"],
                "url": citation["url"],
            },
            applicability=applicability,
            data={
                "raw": decode["raw"],
                "value": decode["value"],
                "unit": decode["unit"],
            },
            request={
                "completeness": "complete",
                "pattern": "kwp_local_id",
                "matcher": exchange["matcher"],
                "prefix": exchange["prefix"],
                "identifier_echo": exchange["identifier_echo"],
            },
            license={"source_license": citation["license"]},
        ),
        ResearchRecord(
            record_id=doc["record_id"],
            record_type="signal_definition",
            source_id=SOURCE_ID,
            evidence_tier="A",
            verification="externally_verified",
            safety="read_only_telemetry_candidate",
            normalized_signal="dpf.soot_mass.modelled",
            fact_labels=("wire_observation", "inference"),
            category="complete_runtime_candidate",
            notes=(
                "The issue labels 0x0406 'DPF soot mass'; the D73N47A0 "
                "table names 0x0406 PFltLd_mSotSimCont (continuously "
                "SIMULATED mass, same 0.01 g scale). The normalized name "
                "follows the table; the merge across D73<->this DDE7 is "
                "an inference recorded in the conflict report."
            ),
            source={
                "source_identifier": "0x0406",
                "source_label": decode["signal"],
                "scale_expression": decode["scale_expression"],
                "url": citation["url"],
            },
            applicability=applicability,
            data={
                "raw_type": "uint16",
                "width_bytes": 2,
                "byte_order": decode["byte_order"],
                "mul": "1",
                "div": "100",
                "add": "0",
                "unit": decode["unit"],
            },
            request={
                "completeness": "complete",
                "target": "source_vehicle_engine_ecu",
                "pattern": "kwp_local_id",
                "sequence": [exchange["request"]],
                "session": "none",
                "matcher": exchange["matcher"],
                "prefix": exchange["prefix"],
                "payload_location": "after 2-byte 6C 10 prefix",
                "response_length": 2,
            },
            license={"source_license": citation["license"]},
        ),
    ]

    for claim in doc.get("additional_claims", []):
        ident = claim["identifier"]
        records.append(ResearchRecord(
            record_id=f"wican752.dde7.claim.{ident}",
            record_type="signal_definition",
            source_id=SOURCE_ID,
            evidence_tier="C",
            verification="discovered",
            safety="read_only_telemetry_candidate",
            fact_labels=("source_claim",),
            category="partial_signal_definition",
            notes="claimed in the issue's table; no raw frames shown",
            source={
                "source_identifier": ident,
                "source_label": claim["signal"],
                "scale_expression": claim["scale_expression"],
                "response_shape": claim["response_shape"],
                "url": citation["url"],
            },
            applicability=applicability,
            data={
                "raw_type": "unknown",
                "byte_order": "unknown",
                "mul": "unknown",
                "div": "unknown",
                "add": "unknown",
                "unit": claim["unit"],
            },
            request={
                "completeness": "incomplete",
                "target": "unknown",
                "sequence": "unknown",
                "session": "unknown",
                "matcher": None,
                "payload_location": "unknown",
                "response_length": "unknown",
            },
            license={"source_license": citation["license"]},
        ))

    return records


def fixture_bytes(path: str = EVIDENCE_PATH) -> Tuple[bytes, bytes, float]:
    """(request, response, expected value) for the mapping-engine test."""
    with open(path, "r", encoding="utf-8") as handle:
        doc = yamlsubset.loads(handle.read(), source=path)

    request = bytes(int(p, 16) for p in doc["exchange"]["request"].split())
    response = bytes(int(p, 16) for p in doc["exchange"]["response"].split())

    return request, response, float(doc["decode"]["value"])
