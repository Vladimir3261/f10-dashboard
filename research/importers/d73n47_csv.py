"""
Importer: the MorGuux `D73N47A0 (BMW N47 DDE).csv` gist.

Source: https://gist.github.com/MorGuux/832054bcbe6c1207b1f3075d5ecf6a4a
Pinned revision 074bac9c7700fdc845bbdf4cd7784dd6be685ba2 (2023-03-14),
sha256 105fd0efc1f8fadee7987fa86d83626067e9b9eb00ae0a72c19634c26b35746f,
1644 data rows. No license is attached to the gist -> `unknown`.

What the CSV establishes (Tier B, sgbd_derived shape): per measurement a
Title (the EDIABAS ARG), a 16-bit identifier, a RESULTNAME, a data type,
a unit, MUL and ADD factors, a label and a description. The column set
matches BMW's SG_FUNKTIONEN / MESSWERTETAB convention minus DIV, SG_ADR
and SERVICE.

What it does NOT establish - and what therefore stays "unknown" in every
record: the wire service, the request sequence, the response prefix and
echo behaviour, the diagnostic session, the byte order of multi-byte
integers, and any compatibility with a chassis other than the E84 this
SGBD belongs to. `D73N47A0` is the E84/X1 KWP2000-family variant
(ediabasx-docs-sgbd index; klartext sgbd-findings). Nothing here is an
F10 mapping.
"""

import csv
import io
from typing import Dict, List, Optional, Tuple

from ..model import ResearchRecord

__all__ = ["import_csv", "ImportSummary", "SOURCE_ID", "PINNED_SHA256"]

SOURCE_ID = "morguux-d73n47a0"
PINNED_REVISION = "074bac9c7700fdc845bbdf4cd7784dd6be685ba2"
PINNED_SHA256 = "105fd0efc1f8fadee7987fa86d83626067e9b9eb00ae0a72c19634c26b35746f"

EXPECTED_HEADER = [
    "Title", "ID", "Result Name", "Data Type", "Unit",
    "MUL Factor", "ADD Factor", "Label", "Description",
]

#: Source data type -> (internal research type, width in bytes).
#: "motorola float" names big-endian IEEE-754 by BMW convention; the
#: byte-order implication for the FLOAT type is recorded, the byte order
#: of the multi-byte INTEGER types is not established by this CSV.
DATA_TYPES: Dict[str, Tuple[str, int]] = {
    "unsigned char": ("uint8", 1),
    "unsigned int": ("uint16", 2),
    "unsigned long": ("uint32", 4),
    "motorola float": ("float32", 4),
}

#: Curated ARG/Title -> normalized signal name. This mapping is OUR
#: judgement (fact label `inference`), applied narrowly: subtly different
#: source signals (mSot vs mSotMeas vs mSotSimCont, Com_ vs Gbx_ vs Tra_)
#: keep distinct or absent normalized names rather than being merged.
NORMALIZED: Dict[str, str] = {
    # DPF
    "PFltLd_mSotMeas": "dpf.soot_mass.measured",
    "PFltLd_mSotSimCont": "dpf.soot_mass.modelled",
    "PFlt_mSotSumMeasFlt_mp": "dpf.soot_mass.filtered",
    "IMRUP": "dpf.soot_mass.measured",       # PFltLd_mSot; alias - see report
    "IMPAS": "dpf.soot_mass.modelled",       # PFltLd_mSotSim; alias - see report
    "PFltRgn_ctRgnSucEEP_mp": "dpf.regeneration.count",
    "PFltRgn_tiSnceRgn": "dpf.time_since_regeneration",
    "IDSLRE": "dpf.distance_since_regeneration",
    "IPDIP": "dpf.differential_pressure",
    "ITAVP1": "dpf.exhaust_temperature.upstream",
    # engine
    "ITMOT": "engine.internal_temperature",
    "ITKUM": "engine.coolant_temperature",
    "ITOEL": "engine.oil_temperature",
    "ITKRS": "fuel.temperature",
    "IPLAD": "engine.boost.actual",
    "SPLAD": "engine.boost.requested",
    "IPRDR": "fuel.rail_pressure.actual",
    "SPRDR": "fuel.rail_pressure.requested",
    # drivetrain values received by the DDE
    "Com_nTSC": "transmission.turbine_speed",
    "Com_tGbxOil": "transmission.oil_temperature",
    "Tra_numGear": "transmission.actual_gear",
    "Twandler_sim": "transmission.converter_temperature.modelled",
    # standard OBD cross-references
    "OBD_PID0C_Epm_nEngRaw": "engine.rpm",
    "OBD_PID05_CEngDsT_tSens": "engine.coolant_temperature",
    "OBD_PID0F_Air_tSensTAFS": "engine.intake_air_temperature",
    "OBD_PID0B_Air_pSensPIntkVUs": "engine.manifold_pressure.absolute",
}


class ImportSummary:
    """What one import run produced, by category."""

    def __init__(self):
        self.rows = 0
        self.categories: Dict[str, int] = {}
        self.duplicate_ids: List[str] = []
        self.duplicate_normalized: List[str] = []
        self.problems: List[str] = []

    def count(self, category: str) -> None:
        self.categories[category] = self.categories.get(category, 0) + 1

    def as_dict(self):
        return {
            "rows": self.rows,
            "categories": dict(sorted(self.categories.items())),
            "duplicate_ids": self.duplicate_ids,
            "duplicate_normalized": self.duplicate_normalized,
            "problems": self.problems,
        }


def _normalize_id(text: str) -> Optional[str]:
    """'0x0406' / '0406' / '0X406' -> canonical '0x0406', or None."""
    t = text.strip()

    if t.lower().startswith("0x"):
        t = t[2:]

    try:
        value = int(t, 16)
    except ValueError:
        return None

    if not 0 <= value <= 0xFFFF:
        return None

    return f"0x{value:04X}"


def _category(title: str, raw_type: str, ident: Optional[str]) -> str:
    if ident is None or raw_type == "unknown":
        return "unknown"

    if title.startswith("OBD_PID"):
        return "standard_obd_crossref"

    return "partial_signal_definition"


def import_csv(text: str) -> Tuple[List[ResearchRecord], ImportSummary]:
    """
    Parse the full CSV into research records.

    Every original column is preserved verbatim under `source`; MUL/ADD
    are kept as their original decimal STRINGS so no precision is lost
    to float round-tripping. Every record's request block says exactly
    what the CSV supports: nothing.
    """
    summary = ImportSummary()
    reader = csv.reader(io.StringIO(text))
    header = next(reader)

    if header != EXPECTED_HEADER:
        summary.problems.append(f"unexpected header: {header}")

    records: List[ResearchRecord] = []
    seen_ids: Dict[str, str] = {}
    seen_normalized: Dict[str, str] = {}

    for row in reader:
        if not row or all(not cell.strip() for cell in row):
            continue

        summary.rows += 1

        if len(row) != len(EXPECTED_HEADER):
            summary.problems.append(f"row has {len(row)} fields: {row[:2]}")
            continue

        title, raw_id, result_name, data_type, unit, mul, add, label, desc = row
        ident = _normalize_id(raw_id)
        raw_type, width = DATA_TYPES.get(data_type.strip(), ("unknown", 0))

        if ident is not None:
            if ident in seen_ids:
                summary.duplicate_ids.append(ident)
            else:
                seen_ids[ident] = title

        normalized = NORMALIZED.get(title)
        alias = False

        if normalized is not None:
            if normalized in seen_normalized:
                summary.duplicate_normalized.append(normalized)
                alias = True
            else:
                seen_normalized[normalized] = title

        category = _category(title, raw_type, ident)

        if alias:
            category = "duplicate_alias"

        summary.count(category)

        labels = ["sgbd_derived", "source_claim"]

        if normalized is not None:
            labels.append("inference")     # the normalized name is ours

        data = {
            "raw_type": raw_type,
            "width_bytes": width or "unknown",
            "byte_order": (
                "big" if raw_type == "float32" else "unknown"
            ),
            "mul": mul.strip(),
            "div": "1",                     # the CSV carries no DIV column
            "add": add.strip(),
            "unit": unit.strip() or "unknown",
        }

        records.append(ResearchRecord(
            record_id=f"d73n47a0.{title}",
            record_type="signal_definition",
            source_id=SOURCE_ID,
            evidence_tier="B",
            verification="discovered",
            safety="read_only_telemetry_candidate",
            normalized_signal=normalized,
            fact_labels=tuple(labels),
            category=category,
            source={
                "source_record": title,
                "source_identifier": ident if ident else "unknown",
                "source_identifier_raw": raw_id,
                "source_result_name": result_name,
                "source_data_type": data_type,
                "source_unit": unit,
                "source_mul": mul,
                "source_add": add,
                "source_label": label,
                "source_description": desc,
            },
            applicability={
                "engine_family": "N47",
                "sgbd": "D73N47A0",
                "source_chassis": ["E84"],
                "protocol_family": "kwp_bmw_fast_candidate",
                "target_chassis_status": "unverified",
            },
            data=data,
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

    return records, summary
