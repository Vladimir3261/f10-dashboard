"""
Pipeline orchestrator: sources -> normalized records -> reports.

    python3 -m research.build

Reads the pinned source cache under local/research-cache/ (see
research/sources/README.md for how to populate it), runs every importer,
validates all records against the model and the manifest, runs the
candidate gate and conflict detection, and rewrites:

    research/normalized/n47/signals.jsonl
    research/normalized/n47/requests.jsonl
    research/normalized/n47/jobs.jsonl
    research/normalized/n47/evidence.jsonl
    research/reports/n47-coverage.md
    research/reports/n47-conflicts.md

Output is deterministic: identical inputs produce byte-identical files.
The narrative reports are hand-maintained and not touched here.
"""

import hashlib
import os
import sys
from typing import Dict, List

from bmwdiag.mapping import yamlsubset

from . import conflicts as conflicts_mod
from . import reports_gen
from .gate import candidate_gate
from .importers import (
    d73n47_csv,
    deep_obd_xml,
    klartext_f25,
    test_o_customjobs,
    wican_issue_fixture,
)
from .manifest import check_source_ids, load_manifest, load_relationships
from .model import ResearchRecord, records_to_jsonl, validate_record

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "local", "research-cache")
NORMALIZED = os.path.join(ROOT, "research", "normalized", "n47")
REPORTS = os.path.join(ROOT, "research", "reports")
EVIDENCE = os.path.join(ROOT, "research", "evidence", "n47")

CACHE_FILES = {
    "d73_csv": os.path.join(CACHE, "gists", "morguux", "D73N47A0.csv"),
    "motor_ccpage": os.path.join(CACHE, "ediabaslib", "Motor.ccpage"),
    "customjobs": os.path.join(CACHE, "bmwxdfs", "customjobs.xml"),
}

#: Hand-written narrative for the conflict report; the table below it is
#: generated. Kept here so one command rewrites the whole file coherently.
CONFLICT_NARRATIVE = """\
## The 0x0406 DPF soot overlap (required example)

Three sources touch the same semantic ground with irreconcilable wire
facts:

| source | variant | how soot is read | identifier(s) | scale |
|---|---|---|---|---|
| MorGuux CSV | `D73N47A0` (E84, KWP/BMW-FAST family) | unproven by the CSV | `0x0405` measured / `0x0406` continuously simulated (+ `0x03EA`/`0x03ED` legacy aliases at a DIFFERENT scale) | 0.01 g/bit (aliases: 0.015259) |
| WiCAN issue #752 | E90 `DDE7N47` (D71-family KWP heritage) | `2C 10 04 06` -> `6C 10 VV VV`, no identifier echo (raw capture) | `0x0406` | /100 g (= 0.01 g/bit) |
| Klartext F25 | `d72n47a0` (F-series UDS) | dynamic `F303` define + `22 F3 03` (pcap) | `0x44BE` measured / `0x44C1` simulated | 0.015259 / 0.01 g/bit |

Conclusion the evidence supports - and no more: **same or related
semantic domain, not wire-compatible without variant resolution.** The
`0x0406`/0.01 agreement between the D73 table and the E90 capture is
consistent with one Bosch DDE numbering family spanning E-series
variants, but the D71 KWP `$2C $10` framing (confirmed by the D71N47A0
SGBD's own job comment) versus the F-series `F303` indirection means an
identifier is only meaningful WITH its variant's request pattern.
Merging `d71`/`d72`/`d73` rows into one "N47 mapping" would manufacture
confident nonsense.

Note also the alias tension INSIDE `D73N47A0`: `IMRUP` (PFltLd_mSot,
0.015259 g/bit) vs `PFltLd_mSotMeas` (0.01 g/bit) are subtly different
soot estimates at different scales - imported unmerged, surfaced below.

## The 0x03EB / 0x0AF1 cross-family touchpoints (WiCAN claims vs D73 table)

The WiCAN issue's two UNcaptured claims land differently against the
D73 table:

* `0x03EB` - WiCAN claims distance-since-regen, 4 data bytes, `/1000`
  km. D73's `IDSLRE` is `0x03EB`, `unsigned long`, unit **metres**,
  MUL 1 - u32 metres IS /1000 km. **Full agreement** (identity, width,
  scale), but the sources are not proven independent of Bosch's shared
  DDE numbering, so this stays corroboration, not confirmation.
* `0x0AF1` - WiCAN claims engine temperature at `x0.01969` with no
  offset; D73's `ITMOT` is `0x0AF1` at `0.1*raw - 273.14` (deci-Kelvin,
  the same formula d72n47a0 uses for its `0x4BC3`). **Same identifier,
  same semantic, irreconcilable scales.** At a warm-engine raw ~3630
  they differ by ~18 degC - one real capture on an E-series DDE7 car
  settles it. Until then neither scale is promoted.

**Validation experiment:** resolve the F10's DDE SGBD on-car (d_motor
IDENT); then read `0x44BE`/`0x44C1` via the F303 sequence and, only if
the variant proves KWP-family, try `2C 10 04 06`. Compare against ISTA.

## The 0x586F oil-pressure decode disagreement

OBDb's BMW-5-Series signalset declares `22 586F` as an 8-bit scalar; the
obd-gauge-cluster F10 on-car scan proves a 16-bit big-endian millibar
value (159 samples, 144 distinct values, plausible absolute pressures).
On-car evidence wins for candidacy, but the OBDb row is kept as-is: the
disagreement is recorded, not overwritten.

## Static versus dynamic reads on d72n47a0

Klartext's own bytecode oracle emits `22 4517` for `STATUS_LESEN`, and
the real F25 DDE rejects it with `7F 22 31`; the working path is the
`F303` define. Recorded as a warning: a source's *implementation* of a
request is not evidence the ECU accepts it - only wire evidence is.

## Runtime constraint recorded for the F303 candidates

One dynamic DID holds one define at a time as far as the evidence shows
(each Klartext read re-armed the define). The runtime `setup:` sequence
arms once per session, so ONLY ONE F303-based request may be enabled at
a time until multi-source defines are proven on-car. The candidate file
carries the same warning.
"""


def _load_yaml(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        return yamlsubset.loads(handle.read(), source=path)


def _obdb_records() -> List[ResearchRecord]:
    doc = _load_yaml(os.path.join(EVIDENCE, "obdb", "egs_dids.yaml"))
    out: List[ResearchRecord] = []

    for claim in doc["claims"]:
        complete = bool(claim["decode_complete"])
        out.append(ResearchRecord(
            record_id=claim["record_id"],
            record_type="signal_definition",
            source_id=doc["source_id"],
            evidence_tier=doc["evidence_tier"],
            verification=doc["verification"],
            safety=doc["safety"],
            fact_labels=("source_claim",),
            category=(
                "partial_signal_definition" if not complete
                else "complete_runtime_candidate"
            ),
            source={
                "source_record": claim["obdb_id"],
                "source_identifier": claim["request"].replace("22 ", "0x").replace(" ", ""),
                "source_label": claim["signal"],
                "fmt": claim["fmt"],
            },
            applicability={
                "engine_family": "unknown",
                "sgbd": "unknown",
                "source_ecu": f"0x{claim['ecu_address']:02X}",
                "source_chassis": ["F10", "F11", "G30"],
                "protocol_family": "uds_static_did",
                "target_chassis_status": "unverified",
            },
            data={
                "raw_type": "unknown" if not complete else "per-fmt",
                "byte_order": "unknown",
                "mul": "unknown",
                "div": "unknown",
                "add": "unknown",
                "unit": "unknown",
            },
            request={
                "completeness": "complete",
                "target": f"0x{claim['ecu_address']:02X}",
                "pattern": "uds_static_did",
                "sequence": [claim["request"]],
                "session": "unknown",
                "matcher": "echo_full",
                "payload_location": "after 3-byte echo",
                "response_length": "unknown",
            },
            license={"source_license": doc["citation"]["license"]},
        ))

    return out


def _f10_field_records() -> List[ResearchRecord]:
    doc = _load_yaml(os.path.join(EVIDENCE, "f10_field", "oil_pressure_586F.yaml"))
    dec = doc["decode"]

    records = [ResearchRecord(
        record_id=doc["record_id"],
        record_type="signal_definition",
        source_id=doc["source_id"],
        evidence_tier=doc["evidence_tier"],
        verification=doc["verification"],
        safety=doc["safety"],
        normalized_signal="engine.oil_pressure",
        fact_labels=("wire_observation", "inference"),
        category="complete_runtime_candidate",
        notes="on-car F10, but N55 petrol - engine family does not match N47",
        source={
            "source_record": "586F",
            "source_identifier": "0x586F",
            "source_label": dec["signal"],
        },
        applicability={
            "engine_family": "N55",
            "sgbd": "unknown",
            "source_ecu": "DME (7DF functional)",
            "source_chassis": ["F10"],
            "protocol_family": "uds_static_did",
            "target_chassis_status": "chassis matches; engine unverified",
        },
        data={
            "raw_type": dec["raw_type"],
            "width_bytes": 2,
            "byte_order": dec["byte_order"],
            "mul": str(dec["mul"]),
            "div": str(dec["div"]),
            "add": str(dec["add"]),
            "unit": dec["unit"],
        },
        request={
            "completeness": "complete",
            "target": "engine ECU via functional broadcast",
            "pattern": "uds_static_did",
            "sequence": [doc["exchange"]["request"]],
            "session": "none",
            "matcher": doc["exchange"]["matcher"],
            "prefix": doc["exchange"]["prefix"],
            "payload_location": "after 3-byte 62 58 6F echo",
            "response_length": 2,
        },
        license={"source_license": doc["citation"]["license"]},
    )]

    for lead in doc.get("unpinned_leads", []):
        ident = lead["request"].replace("22 ", "0x").replace(" ", "")
        records.append(ResearchRecord(
            record_id=f"f10field.n55.lead.{ident}",
            record_type="signal_definition",
            source_id=doc["source_id"],
            evidence_tier="C",
            verification="discovered",
            safety="read_only_telemetry_candidate",
            fact_labels=("wire_observation", "source_claim"),
            category="partial_signal_definition",
            notes=lead["note"],
            source={"source_identifier": ident, "source_record": ident},
            applicability={
                "engine_family": "N55",
                "sgbd": "unknown",
                "source_chassis": ["F10"],
                "protocol_family": "uds_static_did",
                "target_chassis_status": "unverified",
            },
            data={
                "raw_type": "unknown", "byte_order": "unknown",
                "mul": "unknown", "div": "unknown", "add": "unknown",
                "unit": "unknown",
            },
            request={
                "completeness": "complete",
                "target": "engine ECU via functional broadcast",
                "pattern": "uds_static_did",
                "sequence": [lead["request"]],
                "session": "none",
                "matcher": "echo_full",
                "payload_location": "unknown",
                "response_length": "unknown",
            },
            license={"source_license": doc["citation"]["license"]},
        ))

    return records


def collect_records(strict: bool = True) -> List[ResearchRecord]:
    records: List[ResearchRecord] = []

    # -- committed evidence (always available) ------------------------
    records += wican_issue_fixture.import_fixture()
    records += klartext_f25.import_evidence()
    records += _obdb_records()
    records += _f10_field_records()

    # -- cached sources ----------------------------------------------
    missing = [k for k, p in CACHE_FILES.items() if not os.path.isfile(p)]

    if missing and strict:
        raise SystemExit(
            f"[!] missing cached sources: {', '.join(missing)} - see "
            "research/sources/README.md for the fetch commands"
        )

    if not missing:
        with open(CACHE_FILES["d73_csv"], encoding="utf-8") as handle:
            text = handle.read()

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

        if digest != d73n47_csv.PINNED_SHA256:
            raise SystemExit(
                f"[!] cached D73 CSV hash {digest} does not match the "
                f"pinned {d73n47_csv.PINNED_SHA256}"
            )

        csv_records, summary = d73n47_csv.import_csv(text)
        records += csv_records
        print(f"[+] d73n47a0 csv: {summary.as_dict()}")

        with open(CACHE_FILES["motor_ccpage"], encoding="utf-8") as handle:
            ccpage_records, problems = deep_obd_xml.import_ccpage(handle.read())

        records += ccpage_records

        for problem in problems:
            print(f"[!] deep-obd: {problem}")

        with open(CACHE_FILES["customjobs"], encoding="utf-8") as handle:
            testo_records, problems = test_o_customjobs.import_customjobs(
                handle.read()
            )

        records += testo_records

        for problem in problems:
            print(f"[!] test-o: {problem}")

    return records


def main() -> int:
    sources = load_manifest()
    relationships = load_relationships()
    records = collect_records(strict=True)

    # -- validation ---------------------------------------------------
    problems: List[str] = []

    for record in records:
        for problem in validate_record(record):
            problems.append(f"{record.record_id}: {problem}")

    problems += check_source_ids(records, sources)

    if problems:
        for problem in problems:
            print(f"[!] {problem}", file=sys.stderr)

        return 1

    ids = [r.record_id for r in records]

    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        print(f"[!] duplicate record ids: {dupes}", file=sys.stderr)
        return 1

    # -- gate + conflicts --------------------------------------------
    gate_results: Dict[str, List[str]] = {
        r.record_id: candidate_gate(r)
        for r in records if r.record_type == "signal_definition"
    }
    found = conflicts_mod.detect_conflicts(records)
    confirmed = conflicts_mod.confirmations(records, relationships)

    # -- write normalized --------------------------------------------
    os.makedirs(NORMALIZED, exist_ok=True)

    buckets = {
        "signals.jsonl": [r for r in records if r.record_type == "signal_definition"],
        "requests.jsonl": [r for r in records if r.record_type == "request_evidence"],
        "jobs.jsonl": [r for r in records if r.record_type == "job_definition"],
        "evidence.jsonl": [r for r in records if r.record_type == "raw_exchange"],
    }

    for name, bucket in buckets.items():
        path = os.path.join(NORMALIZED, name)

        with open(path, "w", encoding="utf-8") as handle:
            handle.write(records_to_jsonl(bucket))

        print(f"[+] wrote {path} ({len(bucket)} records)")

    # -- write generated reports -------------------------------------
    os.makedirs(REPORTS, exist_ok=True)

    with open(os.path.join(REPORTS, "n47-coverage.md"), "w",
              encoding="utf-8") as handle:
        handle.write(reports_gen.coverage_report(records, gate_results))

    with open(os.path.join(REPORTS, "n47-conflicts.md"), "w",
              encoding="utf-8") as handle:
        handle.write(reports_gen.conflicts_report(found, CONFLICT_NARRATIVE))

    eligible = sorted(k for k, v in gate_results.items() if not v)

    print(f"[+] records: {len(records)}; conflicts: {len(found)}; "
          f"gate-eligible: {len(eligible)}")

    for record_id in eligible:
        print(f"    eligible: {record_id}")

    for signal, group in confirmed:
        print(f"    cross-source candidate: {signal} "
              f"({', '.join(r.source_id for r in group)})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
