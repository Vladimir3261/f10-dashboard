"""
Importer: Deep OBD `.ccpage` configuration (EdiabasLib).

Source: uholeschak/ediabaslib, BmwDeepObd/Xml/E90/Motor.ccpage, pinned at
commit a7cef80490412115b16d700901573ec821f01ec8 (GPL-3.0). The file is a
display configuration for an E90 diesel: it names the SGBD group
(`d_motor`), the job (`STATUS_MESSWERTBLOCK_LESEN`), the block of ARG
names the job is called with, and the `STAT_..._WERT` result names each
display row consumes.

That is PARTIAL knowledge, and it is imported as exactly that: a
`job_definition` record plus per-result `signal_definition` records
whose request block is entirely unknown. A result name proves the
measurement exists in the E90 DDE's vocabulary; it establishes no
identifier, no scaling, no response layout and no F10 compatibility.
The value of these records is cross-referencing: the same ARG names
(ITMOT, ITOEL, ...) appear in the D71/D72/D73 tables with per-variant
identifiers and scales.
"""

import re
import xml.etree.ElementTree as ET
from typing import List, Tuple

from ..model import ResearchRecord

__all__ = ["import_ccpage", "SOURCE_ID"]

SOURCE_ID = "ediabaslib"

_NS = "{http://www.holeschak.de/BmwDeepObd}"


def _strip_ns(tag: str) -> str:
    return tag[len(_NS):] if tag.startswith(_NS) else tag


def import_ccpage(text: str) -> Tuple[List[ResearchRecord], List[str]]:
    """
    Parse one .ccpage; returns (records, problems).

    Deterministic: records follow document order of the display rows.
    """
    problems: List[str] = []
    records: List[ResearchRecord] = []

    root = ET.fromstring(text)
    jobs = root.iter(f"{_NS}job")

    for job in jobs:
        job_name = job.get("name", "")
        parent_sgbd = None

        #
        # ElementTree has no parent pointers; find the enclosing <jobs>
        # sgbd attribute by scanning again. Fine for a config this size.
        #
        for jobs_el in root.iter(f"{_NS}jobs"):
            if job in list(jobs_el):
                parent_sgbd = jobs_el.get("sgbd")

        args = job.get("args", "")
        arg_names = [a for a in args.split(";") if a and a not in ("JA", "NEIN")]

        records.append(ResearchRecord(
            record_id=f"deepobd.e90.{job_name}",
            record_type="job_definition",
            source_id=SOURCE_ID,
            evidence_tier="C",
            verification="discovered",
            safety="read_only_telemetry_candidate",
            fact_labels=("source_claim",),
            category="job_definition",
            source={
                "sgbd_group": parent_sgbd or "unknown",
                "job": job_name,
                "args": arg_names,
            },
            applicability={
                "engine_family": "N47",
                "sgbd": "unknown",       # d_motor group; exact variant unresolved
                "source_chassis": ["E90"],
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
            license={"source_license": "GPL-3.0"},
        ))

        for display in job.iter(f"{_NS}display"):
            result = display.get("result", "")

            if not result:
                problems.append(f"display row without result in {job_name}")
                continue

            #
            # STAT_MOTOROEL_TEMPERATUR_WERT -> a stable id fragment.
            #
            frag = re.sub(r"^STAT_|_WERT$", "", result)

            records.append(ResearchRecord(
                record_id=f"deepobd.e90.{job_name}.{frag}",
                record_type="signal_definition",
                source_id=SOURCE_ID,
                evidence_tier="C",
                verification="discovered",
                safety="read_only_telemetry_candidate",
                fact_labels=("source_claim",),
                category="partial_signal_definition",
                source={
                    "source_result_name": result,
                    "job": job_name,
                    "display_label": display.get("name", ""),
                },
                applicability={
                    "engine_family": "N47",
                    "sgbd": "unknown",
                    "source_chassis": ["E90"],
                    "protocol_family": "ediabas_job",
                    "target_chassis_status": "unverified",
                },
                data={
                    "raw_type": "unknown",
                    "byte_order": "unknown",
                    "mul": "unknown",
                    "div": "unknown",
                    "add": "unknown",
                    "unit": "unknown",
                },
                request={
                    "completeness": "unknown",
                    "target": "unknown",
                    "sequence": "unknown",
                    "session": "unknown",
                    "matcher": None,
                    "payload_location": "unknown",
                    "response_length": "unknown",
                },
                license={"source_license": "GPL-3.0"},
            ))

    return records, problems
