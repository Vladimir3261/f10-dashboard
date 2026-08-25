"""
Importer: TestO Datalogger `customjobs.xml` (BMW-XDFs).

Source: MotorMouth93/BMW-XDFs, Me7.2/Datalogger/TestO Datalogger/config/
customjobs.xml, pinned at commit 54a7ce420867452609e1116adc0afb8fe8a395ba.
(The originally-referenced zarboz/BMW-XDFs repository no longer exists;
this repository carries the same path. No license is attached.)

What one <customjob> entry establishes: an SGBD variant name (e.g.
`D71N47A0`), a job name (`STATUS_MESSWERTBLOCK_LESEN`), and a set of
numeric identifiers someone logged with. Nothing else - not the meaning
of any identifier, not the response layout, not the scaling, not F10
compatibility. Tier C, `discovered`, and it stays that way.
"""

import xml.etree.ElementTree as ET
from typing import List, Tuple

from ..model import ResearchRecord

__all__ = ["import_customjobs", "SOURCE_ID"]

SOURCE_ID = "bmw-xdfs-testo"


def import_customjobs(
    text: str,
    ecu_filter: str = "N47",
) -> Tuple[List[ResearchRecord], List[str]]:
    """
    Parse customjobs.xml; keep entries whose ECU name contains
    `ecu_filter`. Deterministic, document order.
    """
    problems: List[str] = []
    records: List[ResearchRecord] = []
    root = ET.fromstring(text)

    for job in root.iter("customjob"):
        ecu = job.get("ecu", "")

        if ecu_filter not in ecu:
            continue

        jobname = (job.findtext("jobname") or "").strip()
        argument = (job.findtext("argument") or "").strip()
        virtual = (job.findtext("virtualname") or "").strip()

        if not jobname:
            problems.append(f"customjob for {ecu} without a jobname")
            continue

        parts = [p for p in argument.split(";") if p]
        identifiers = []

        for part in parts:
            if part.lower().startswith("0x"):
                try:
                    identifiers.append(f"0x{int(part, 16):04X}")
                except ValueError:
                    problems.append(f"{ecu}: unparseable identifier {part!r}")

        records.append(ResearchRecord(
            record_id=f"testo.{ecu}.{jobname}.{virtual or 'SET'}",
            record_type="job_definition",
            source_id=SOURCE_ID,
            evidence_tier="C",
            verification="discovered",
            safety="read_only_telemetry_candidate",
            fact_labels=("source_claim",),
            category="job_definition",
            source={
                "sgbd": ecu,
                "job": jobname,
                "argument_raw": argument,
                "identifiers": identifiers,
            },
            applicability={
                "engine_family": "N47",
                "sgbd": ecu,
                "source_chassis": [],
                "protocol_family": "ediabas_job",
                "target_chassis_status": "unverified",
            },
            data={},
            request={
                "completeness": "unknown",
                "target": "unknown",
                "sequence": "unknown",
                "session": "unknown",
                "matcher": None,
                "payload_location": "unknown",
                "response_length": "unknown",
            },
            license={"source_license": "unknown"},
        ))

    return records, problems
