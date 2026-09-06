#!/usr/bin/env python3
"""
dtc.py - on-demand, read-only fault-memory readout with a tracked artifact.

    python3 tools/dtc.py                      # engine ECU (discovered), 19 02 FF
    python3 tools/dtc.py --ecu 0x18           # the EGS
    python3 tools/dtc.py --mask 0x0C          # what ISTA's FS_LESEN asks for
    python3 tools/dtc.py --detail             # + snapshot (19 04) and extended
                                              #   data (19 06) per reported DTC
    python3 tools/dtc.py --count              # 19 01 - the number Mode 01
                                              #   PID 0x01 should agree with

Issue #15, domain 7: fault context is an ON-DEMAND read, not polled
telemetry. The lake's narrow samples table is the wrong shape for a
list of codes, and a stored code changes a few times a year; what the
polled set needs is the one-byte MIL/DTC-count flag from standard PID
0x01 (mappings/candidates/obd/engine_sae_extra.yaml), and this tool is
what that flag is checked against.

READ-ONLY, enforced twice: every payload comes from `bmwdiag.dtc`, whose
builders run the observational gate before returning, and `live.HsfzClient`
gates every frame again on the way out. Service 0x14
(ClearDiagnosticInformation) is on the refusal list of that gate and
this tool has no code path that builds it. Fault memory is never
cleared from here - clearing destroys evidence and is a write.

Output: a tracked, VIN-redacted artifact under validation-runs/
<UTC>-dtc/ (run.json, frames.ndjson, summary.md) and a raw copy under
gitignored local/validation-runs-raw/, exactly like validate_candidate.py
- reusing its RunArtifacts so the artifact format has one definition.

live.py must NOT be running: the ZGW serves one HSFZ client at a time.
"""

import argparse
import importlib.util
import os
import sys
from typing import Any, Callable, Dict, List, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bmwdiag import dtc as udsdtc                         # noqa: E402
from bmwdiag.protocol import NegativeResponse             # noqa: E402
from bmwdiag.protocol.safety import assert_observational  # noqa: E402


def _load_validate_candidate():
    """The sibling tool, for connect_engine/RunArtifacts/nrc handling."""
    spec = importlib.util.spec_from_file_location(
        "validate_candidate", os.path.join(_ROOT, "tools", "validate_candidate.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


#: A request function: (payload, dst, timeout) -> response bytes. The
#: real one wraps live.HsfzClient.request_safe; tests inject a fake.
RequestFn = Callable[[bytes, int, float], bytes]


def read_faults(request: RequestFn, addr: int, *, mask: int = udsdtc.MASK_ALL,
                count: bool = False, detail: bool = False,
                timeout: float = 3.0,
                nrc_fields: Optional[Callable[[Exception], Dict[str, Any]]] = None,
                negative_type: type = NegativeResponse) -> List[Dict[str, Any]]:
    """
    The whole readout as artifact records, sending only 0x19 frames.

    One record per exchange: `request` (the hex payload), `frames` (tx/rx
    with NRC fields when the ECU refused), `outcome`, and the parsed
    result (`dtcs` / `count` / `detail`) when the reply parsed. A
    negative response (`negative_type`, the transport's NRC exception)
    is recorded as data, not raised - an ECU that refuses 19 04 for a
    code is itself a finding. Any other failure (timeout, link down) is
    recorded as `no_response` with the exception, so the two are never
    confused in the artifact.
    """
    records: List[Dict[str, Any]] = []

    def exchange(payload: bytes, kind: str) -> Optional[bytes]:
        #
        # Belt and braces: the builder already ran the gate, the
        # transport will run it again. Running it here too means a
        # record can never describe a frame that was not observational.
        #
        assert_observational(payload)

        frame: Dict[str, Any] = {"dst": f"0x{addr:02X}", "tx": payload.hex(" "),
                                 "rx": None, "nrc": None}
        record: Dict[str, Any] = {
            "kind": kind, "request": payload.hex(" "),
            "ecu_addr": f"0x{addr:02X}", "frames": [frame],
        }
        records.append(record)

        try:
            response = request(payload, addr, timeout)
        except negative_type as exc:
            frame.update(nrc_fields(exc) if nrc_fields else {"nrc": "negative"})
            record["outcome"] = "negative_response"
            record["nrc"] = frame.get("nrc")
            record["nrc_name"] = frame.get("nrc_name")
            record["service"] = frame.get("service")

            return None
        except Exception as exc:
            record["outcome"] = f"no_response: {type(exc).__name__}: {exc}"

            return None

        frame["rx"] = response.hex(" ")

        return response

    reported: List[udsdtc.Dtc] = []

    if count:
        response = exchange(udsdtc.report_number_by_status_mask(mask), "dtc_count")

        if response is not None:
            record = records[-1]

            try:
                parsed = udsdtc.parse_number(response)
            except udsdtc.DtcParseError as exc:
                record["outcome"] = f"unparsed: {exc}"
            else:
                record["outcome"] = "ok"
                record["count"] = parsed.count
                record["availability_mask"] = f"0x{parsed.availability_mask:02X}"
                record["format_identifier"] = parsed.format_identifier
                record["signals"] = {"dtc_count": parsed.count}

    response = exchange(udsdtc.report_by_status_mask(mask), "dtc_report")

    if response is not None:
        record = records[-1]

        try:
            report = udsdtc.parse_report(response)
        except udsdtc.DtcParseError as exc:
            record["outcome"] = f"unparsed: {exc}"
        else:
            record["outcome"] = "ok"
            record["availability_mask"] = f"0x{report.availability_mask:02X}"
            record["dtcs"] = [
                {"fault": f"0x{d.fault:04X}", "ftb": f"0x{d.ftb:02X}",
                 "status": f"0x{d.status:02X}", "severity": d.severity,
                 "flags": d.flags}
                for d in report.dtcs
            ]
            record["signals"] = {
                f"0x{d.fault:04X}/0x{d.ftb:02X}": f"{d.severity} [{' '.join(d.flags) or '-'}]"
                for d in report.dtcs
            }
            record["plausibility_note"] = (
                "codes are BMW-internal (fault number / failure-type byte); "
                "cross-reference against ISTA for text. 'not-run' is a "
                "monitor that has not completed, not a present fault."
            )
            reported = list(report.dtcs)

    if detail:
        for d in reported:
            for builder, kind, sub in (
                (udsdtc.snapshot_by_dtc, "dtc_snapshot", udsdtc.SUBFN_SNAPSHOT_BY_DTC),
                (udsdtc.extended_data_by_dtc, "dtc_extended", udsdtc.SUBFN_EXTENDED_BY_DTC),
            ):
                response = exchange(builder(d.code), kind)

                if response is None:
                    continue

                record = records[-1]

                try:
                    parsed = udsdtc.parse_detail(response, sub)
                except udsdtc.DtcParseError as exc:
                    record["outcome"] = f"unparsed: {exc}"
                    continue

                record["outcome"] = "ok"
                record["dtc"] = f"0x{parsed.dtc.fault:04X}/0x{parsed.dtc.ftb:02X}"
                record["status"] = f"0x{parsed.dtc.status:02X}"
                record["record_number"] = parsed.record_number
                record["body"] = parsed.body.hex(" ")
                record["body_bytes"] = len(parsed.body)
                record["plausibility_note"] = (
                    "body kept raw: the identifiers inside a snapshot/extended "
                    "record and their lengths are ECU-specific and unsourced."
                )

    return records


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="on-demand READ-ONLY fault-memory readout (UDS 0x19)",
    )
    ap.add_argument("--ip", default=None, help="gateway IP, skips discovery")
    ap.add_argument("--local-ip", default=None, help="local 169.254.x.x address")
    ap.add_argument("--vin", default=None)
    ap.add_argument("--ecu", type=lambda s: int(s, 0), default=None,
                    help="ECU address to read (default: the discovered engine "
                         "ECU; 0x18 for the EGS)")
    ap.add_argument("--mask", type=lambda s: int(s, 0), default=udsdtc.MASK_ALL,
                    help="status mask for 19 02 (default 0xFF = everything; "
                         "0x0C = ISTA's FS_LESEN default, pending+confirmed)")
    ap.add_argument("--count", action="store_true",
                    help="also send 19 01 (reportNumberOfDTCByStatusMask)")
    ap.add_argument("--detail", action="store_true",
                    help="also send 19 04 / 19 06 for every reported DTC")
    ap.add_argument("--timeout", type=float, default=3.0)
    ap.add_argument("--scan-timeout", type=float, default=0.3)
    ap.add_argument("--scan-full", action="store_true")

    return ap


def discovery_args(args) -> argparse.Namespace:
    """
    The arguments discovery gets: everything the operator passed EXCEPT
    `--ecu`, which is the READ address only.

    `live.connect_and_discover` treats `args.ecu` as a forced engine ECU
    and probes it with OBD `01 00` - which the EGS never answers
    (tools/egs.py), so `--ecu 0x18` would abort with "forced ECU 0x18
    did not answer" before a single 0x19 frame was sent. Every EGS
    artifact so far was made the other way round: the engine discovered
    by capability at 0x12, the request *routed* to 0x18. This keeps it
    that way.
    """
    return argparse.Namespace(**{**vars(args), "ecu": None})


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    vc = _load_validate_candidate()

    print("=" * 60)
    print("fault memory readout - READ-ONLY (UDS 0x19 only, never 0x14)")
    print("=" * 60)
    print("[i] live.py must NOT be running: the ZGW serves one HSFZ "
          "client at a time.\n")

    client, engine = vc.connect_engine(discovery_args(args))
    addr = args.ecu if args.ecu is not None else engine.addr

    artifacts = vc.RunArtifacts("dtc")
    # The artifact class is shared with the validation tool and stamps
    # that tool's name and 0x22/0x2C allowlist by default; this readout
    # is a different tool with a narrower allowlist. Say so in the meta.
    artifacts.meta["tool"] = "tools/dtc.py"
    artifacts.meta["allowlist"] = [hex(udsdtc.SERVICE)]
    artifacts.set_environment(
        gateway=client.ip, ecu=f"0x{addr:02X}",
        engine_ecu=engine.label(),
        supported_pid_count=len(engine.supported),
        mask=f"0x{args.mask:02X}",
    )

    def request(payload: bytes, dst: int, timeout: float) -> bytes:
        return client.request_safe(payload, timeout=timeout, dst=dst)

    try:
        records = read_faults(
            request, addr, mask=args.mask, count=args.count,
            detail=args.detail, timeout=args.timeout,
            nrc_fields=vc.nrc_fields, negative_type=vc.NegativeResponse,
        )
    finally:
        client.close()

    for record in records:
        artifacts.add(record)

    for record in records:
        print(f"[{record['kind']}] {record['request']} -> {record.get('outcome')}")

        for line in record.get("dtcs", []):
            print(f"    {line['fault']}/{line['ftb']}  {line['severity']:<9} "
                  f"{line['status']}  {' '.join(line['flags'])}")

        if "count" in record:
            print(f"    count = {record['count']}")

    tracked, raw = artifacts.write()
    print(f"\n[+] artifacts: {tracked}/  (redacted, tracked)")
    print(f"[+] raw copy:  {raw}/  (gitignored)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
