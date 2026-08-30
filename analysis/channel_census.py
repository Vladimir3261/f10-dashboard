#!/usr/bin/env python3
"""
Channel census: what each channel costs against what it tells us.

Joins the mapping definitions (what a channel is, which ECU answers it, how
often it is asked for) with the lake (how many rows it has produced, and how
many of them carry a value we had not already seen).

The number that matters is the last column. A channel with 124,485 samples
and one distinct value is paying storage forever to re-state a constant; one
with 205 distinct values in 129,781 samples is a real signal sampled hard.
Neither is automatically wrong - `gear` SHOULD repeat - but the ratio tells
you where sampling effort is going and whether it is buying anything.

    python3 -m analysis.channel_census                  # markdown table
    python3 -m analysis.channel_census --days 7
    python3 -m analysis.channel_census --host root@1.2.3.4

Read-only. Needs SSH to the analytics server; the lake is not exposed
outside the tunnel.
"""

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bmwdiag.mapping.loader import load_tree                      # noqa: E402
from bmwdiag.mapping.modes import apply_mode, load_modes  # noqa: E402
from bmwdiag.mapping.polling import resolve_classes  # noqa: E402
from bmwdiag.mapping.model import PollingClassDef  # noqa: E402

#: What the ECU behind a target actually is, for the "answered by" column.
FAMILY_LABEL = {
    "engine": "engine (DDE 0x12)",
    "transmission": "transmission (EGS 0x18)",
    "kombi": "instrument cluster (0x63)",
}


def effective_hz(cls: PollingClassDef, members: int) -> Optional[float]:
    """
    How often ONE request in this class actually reaches the wire.

    Staggered classes fire a single member per due-cycle, round-robin, so a
    class of 22 requests at {seconds: 0.5} refreshes each one every 11 s,
    not every 0.5 s. Ignoring that overstates proprietary polling by 22x.
    """
    rate = cls.hz

    if not rate:
        return None

    return rate / members if cls.stagger and members > 1 else rate


def from_mappings(mapping_dir: str, mode: str = "normal") -> Dict[str, Dict[str, Any]]:
    """
    Everything the census knows without touching the lake.

    Rates are the ones the mappings declare, scaled by `mode`.
    """
    mappings = list(load_tree(mapping_dir, production_only=False))
    classes = apply_mode(
        resolve_classes([c for m in mappings for c in m.polling_classes]),
        load_modes().get(mode),
    )

    # How many requests share each class - needed for stagger arithmetic.
    members: Dict[str, int] = {}
    for m in mappings:
        for r in m.requests:
            members[r.polling_class] = members.get(r.polling_class, 0) + 1

    out: Dict[str, Dict[str, Any]] = {}

    for m in mappings:
        if m.ecu.family == "example":          # the synthetic fixture
            continue

        for request in m.requests:
            cls = classes.get(request.polling_class)
            hz = effective_hz(cls, members.get(request.polling_class, 1)) if cls else None

            for signal in request.signals:
                out[signal.key] = {
                    "request_id": request.id,
                    "purpose": signal.label,
                    "ecu": FAMILY_LABEL.get(m.ecu.family, m.ecu.family),
                    "how": request.protocol.upper(),
                    "class": request.polling_class,
                    "hz": hz,
                    "logged": signal.log,
                }

        for derived in m.derived:
            out[derived.key] = {
                "purpose": derived.label,
                "ecu": "computed locally",
                "how": "derived",
                "class": "-",
                "hz": None,
                "logged": derived.log,
            }

    return out


def from_lake(host: str, days: int) -> Dict[str, Dict[str, int]]:
    """Per-channel sample and distinct-value counts."""
    # Deliberately one line: newlines do not survive the shell quoting on
    # the way through ssh, and the query silently returns nothing.
    query = (
        "SELECT channel_raw, count() AS samples, "
        "uniqExact(value) AS distinct_values FROM telemetry.samples "
        "WHERE vehicle_id NOT LIKE 'DEM%' "
        f"AND ts > now() - INTERVAL {int(days)} DAY "
        "GROUP BY channel_raw FORMAT JSONEachRow"
    )
    remote = (
        "cd /opt/f10-dashboard/infra && "
        "U=$(sed -n 's/^CH_USER=//p' .env | head -1) && "
        "P=$(sed -n 's/^CH_PASS=//p' .env | head -1) && "
        'docker compose exec -T clickhouse clickhouse-client '
        '--user "$U" --password "$P" -q ' + json.dumps(query)
    )
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", host, remote],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        sys.exit(f"error: querying the lake failed\n{result.stderr.strip()}")

    rows: Dict[str, Dict[str, int]] = {}

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        row = json.loads(line)
        rows[row["channel_raw"]] = {
            "samples": int(row["samples"]),
            "distinct": int(row["distinct_values"]),
        }

    return rows


def errors_from_lake(host: str, days: int) -> Dict[str, int]:
    """
    Faults per request over the same window.

    Attributed by request, not channel: a request fails as a unit, so every
    channel it carries shares the fault. Empty when the table does not exist
    yet - an older lake simply has no fault history, which is not an error.
    """
    query = (
        "SELECT request_id, count() AS errors FROM telemetry.channel_errors "
        "WHERE vehicle_id NOT LIKE 'DEM%' "
        f"AND ts > now() - INTERVAL {int(days)} DAY "
        "GROUP BY request_id FORMAT JSONEachRow"
    )
    remote = (
        "cd /opt/f10-dashboard/infra && "
        "U=$(sed -n 's/^CH_USER=//p' .env | head -1) && "
        "P=$(sed -n 's/^CH_PASS=//p' .env | head -1) && "
        'docker compose exec -T clickhouse clickhouse-client '
        '--user "$U" --password "$P" -q ' + json.dumps(query)
    )
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", host, remote],
        capture_output=True, text=True,
    )

    out: Dict[str, int] = {}

    if result.returncode != 0:
        return out                     # table absent on an older lake

    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            row = json.loads(line)
            out[row["request_id"]] = int(row["errors"])

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--host", default=None,
                    help="ssh target for the analytics server "
                         "(default: from terraform output)")
    ap.add_argument("--mappings", default=os.path.join(ROOT, "mappings"))
    ap.add_argument("--mode", default="normal",
                    choices=sorted(load_modes().names()),
                    help="report the rates this drive mode produces "
                         "(default: %(default)s)")
    args = ap.parse_args()

    host = args.host
    if not host:
        try:
            ip = subprocess.run(
                ["terraform", f"-chdir={os.path.join(ROOT, 'infra', 'terraform')}",
                 "output", "-raw", "droplet_ip"],
                check=True, capture_output=True, text=True).stdout.strip()
            host = f"root@{ip}"
        except Exception:
            sys.exit("error: no --host given and terraform output unavailable")

    defs = from_mappings(args.mappings, args.mode)
    counts = from_lake(host, args.days)
    errors = errors_from_lake(host, args.days)

    rows: List[Dict[str, Any]] = []

    for key, count in counts.items():
        meta = defs.get(key, {})
        samples, distinct = count["samples"], count["distinct"]
        request_id = meta.get("request_id")
        failures = errors.get(request_id, 0) if request_id else 0
        attempts = samples + failures
        rows.append({
            "errors": failures,
            # Share of ATTEMPTS that failed. Samples are successes, so
            # attempts = successes + failures for the channels this request
            # carries.
            "err_pct": 100.0 * failures / attempts if attempts else 0.0,
            "channel": key,
            "purpose": meta.get("purpose", "(not in any loaded mapping)"),
            "ecu": meta.get("ecu", "?"),
            "how": meta.get("how", "?"),
            "hz": meta.get("hz"),
            "samples": samples,
            "distinct": distinct,
            "pct": 100.0 * distinct / samples if samples else 0.0,
        })

    rows.sort(key=lambda r: r["samples"], reverse=True)

    print(f"# Channel census — last {args.days} days\n")
    print(f"{len(rows)} channels, {sum(r['samples'] for r in rows):,} samples. "
          "`distinct %` is how much of the storage carries information that "
          "was not already there.\n")
    print("| channel | purpose | answered by | how | polled | samples | distinct | distinct % | errors | error % |")
    print("|---|---|---|---|---|---:|---:|---:|---:|---:|")

    for r in rows:
        hz = f"{r['hz']:.2f} Hz" if r["hz"] else "—"
        print(
            f"| `{r['channel']}` | {r['purpose']} | {r['ecu']} | {r['how']} | "
            f"{hz} | {r['samples']:,} | {r['distinct']:,} | {r['pct']:.3f}% | "
            f"{r['errors']:,} | {r['err_pct']:.2f}% |"
        )

    missing = [r["channel"] for r in rows if r["ecu"] == "?"]
    if missing:
        print(f"\n> Not found in any loaded mapping: {', '.join(missing)}. "
              "Recorded by an older mapping version, or renamed since.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
