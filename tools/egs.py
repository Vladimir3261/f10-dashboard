#!/usr/bin/env python3
"""
egs.py - read-only UDS explorer for finding BMW EGS (gearbox) data.

The EGS answers no standard OBD PIDs, and its live-data identifiers are
BMW-proprietary. This tool finds them empirically instead of guessing:

    python3 egs.py find                    # locate the EGS on the bus
    python3 egs.py dtc  --ecu 0x18         # read fault codes (standard 0x19)
    python3 egs.py scan --ecu 0x18         # which 0x22 DIDs return data
    python3 egs.py watch --ecu 0x18        # log those DIDs over time
    python3 egs.py correlate               # match DID fields to known signals

Only read services are ever sent: 0x22 ReadDataByIdentifier, 0x19
ReadDTCInformation, 0x3E TesterPresent. Nothing is coded or written.

`--session` additionally sends 0x10 0x03 (extended diagnostic session).
That is a MODE CHANGE, not a write - it is what any diagnostic tool does
on connect - but it is off by default so you opt in deliberately.
"""

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Tuple

#
# This tool reuses live.py's HSFZ client rather than reimplementing the
# transport. Locate it relative to this file, and put the repository root
# on sys.path first so live.py can import the bmwdiag package - otherwise
# the tool only works when run from the root.
#
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_spec = importlib.util.spec_from_file_location(
    "live", os.path.join(_ROOT, "live.py")
)
live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(live)


#
# Addresses BMW uses for the transmission across E/F platforms. `find`
# does not trust these - it sweeps everything - but they are reported
# first so an obvious hit is easy to spot.
#
EGS_HINTS = {
    0x10: "ZGW central gateway",
    0x12: "DDE engine (confirmed via OBD)",
    0x18: "EGS transmission (classic BMW address)",
}


def connect(args) -> live.HsfzClient:
    ip = args.ip
    local = args.local_ip or live.find_link_local_ip()

    if ip is None:
        if not local:
            sys.exit("no 169.254.x.x interface - pass --local-ip")

        ip, vin = live.discover(local)
        print(f"[+] gateway {ip} (VIN {vin})")

    client = live.HsfzClient(ip, local, timeout=args.timeout)
    client.connect()

    return client


def open_session(client: live.HsfzClient, addr: int, timeout: float = 2.0) -> bool:
    """0x10 0x03 - extended diagnostic session. Opt-in only."""
    try:
        resp = client.request(bytes([0x10, 0x03]), timeout=timeout, dst=addr)
        return bool(resp) and resp[0] == 0x50
    except Exception:
        return False


def probe_addr(client, addr, timeout, session) -> Optional[str]:
    """Any answer at all - positive or negative - proves an ECU is there."""
    notes = []

    for data, tag in (
        (bytes([0x3E, 0x00]), "TesterPresent"),
        (bytes([0x22, 0xF1, 0x90]), "VIN"),
        (bytes([0x22, 0xF1, 0x97]), "SysName"),
    ):
        try:
            resp = client.request(data, timeout=timeout, dst=addr)
        except live.HsfzNack:
            return None
        except live.HsfzError as exc:
            if "NRC" in str(exc):
                notes.append(f"{tag}:NRC")
            continue
        except Exception:
            continue

        text = "".join(chr(b) for b in resp[3:] if 32 <= b < 127).strip()
        notes.append(f"{tag}={text or resp.hex(' ')}")

    if not notes and session:
        if open_session(client, addr):
            notes.append("extended session accepted")

    return ", ".join(notes) if notes else None


def cmd_find(args) -> None:
    """
    Two passes. A single TesterPresent per address is fast and does not
    desync the link; the chattier identification requests are only spent
    on addresses that already answered.
    """
    client = connect(args)

    print(f"[+] pass 1: TesterPresent sweep 0x00-0xFF")

    live_addrs: List[int] = []

    for addr in range(0x100):
        if addr == client.src:
            continue

        if addr and addr % 64 == 0:
            print(f"    ... 0x{addr:02X}", flush=True)

        try:
            resp = client.request_safe(
                bytes([0x3E, 0x00]), timeout=args.scan_timeout, dst=addr
            )
        except live.HsfzNack:
            continue
        except Exception:
            continue

        if resp and resp[0] == 0x7E:
            live_addrs.append(addr)
            print(f"  0x{addr:02X}  alive", flush=True)

    print(f"\n[+] pass 2: identifying {len(live_addrs)} ECUs")

    hits = []

    for addr in live_addrs:
        notes = []

        for did, tag in ((0xF190, "VIN"), (0xF197, "SysName"),
                         (0xF18C, "Serial"), (0xF191, "HwNo")):
            try:
                resp = client.request_safe(
                    bytes([0x22, did >> 8, did & 0xFF]), timeout=1.0, dst=addr
                )
            except Exception:
                continue

            text = "".join(chr(b) for b in resp[3:] if 32 <= b < 127).strip()

            if text:
                notes.append(f"{tag}={text}")

        if not notes and args.session:
            if open_session(client, addr, 1.0):
                notes.append("extended session accepted")

        hint = EGS_HINTS.get(addr, "")
        line = ", ".join(notes) or "responds, no identification"
        print(f"  0x{addr:02X}  {line}   {hint}")
        hits.append((addr, line))

    client.close()

    print(f"\n{len(hits)} ECUs found.")


#
# ISO 14229 DTC status bits. Without decoding these, a "not yet run"
# monitor is indistinguishable from a live fault.
#
STATUS_BITS = [
    (0x01, "testFailed"),
    (0x02, "failedThisCycle"),
    (0x04, "pending"),
    (0x08, "confirmed"),
    (0x10, "notCompletedSinceClear"),
    (0x20, "failedSinceClear"),
    (0x40, "notCompletedThisCycle"),
    (0x80, "warningIndicator"),
]


def dtc_severity(status: int) -> str:
    if status & 0x01:
        return "ACTIVE"
    if status & 0x08:
        return "stored"
    if status & 0x04:
        return "pending"
    if status & 0x20:
        return "historic"
    if status & 0x50 == 0x50:
        return "not-run"
    return "-"


def cmd_dtc(args) -> None:
    """
    UDS 0x19 0x02 - report DTCs by status mask. The service is standard;
    the code numbering is BMW's, so the 3 bytes are shown as a BMW fault
    code plus a failure-type byte rather than forced into SAE Pxxxx form.
    """
    client = connect(args)

    if args.session:
        print(f"[+] extended session: {open_session(client, args.ecu)}")

    try:
        resp = client.request_safe(bytes([0x19, 0x02, 0xFF]), timeout=3.0, dst=args.ecu)
    except Exception as exc:
        client.close()
        sys.exit(f"no DTC response: {exc}")

    client.close()

    if len(resp) < 3 or resp[0] != 0x59:
        sys.exit(f"unexpected reply: {resp.hex(' ')}")

    body = resp[3:]
    records = [
        (int.from_bytes(body[i:i + 3], "big"), body[i + 3])
        for i in range(0, len(body) - 3, 4)
    ]

    order = {"ACTIVE": 0, "stored": 1, "pending": 2, "historic": 3, "not-run": 4, "-": 5}
    records.sort(key=lambda r: order[dtc_severity(r[1])])

    counts: Dict[str, int] = {}

    for _code, status in records:
        sev = dtc_severity(status)
        counts[sev] = counts.get(sev, 0) + 1

    print(f"[+] ECU 0x{args.ecu:02X}: {len(records)} DTC records")
    print("    " + ", ".join(f"{n} {k}" for k, n in
                             sorted(counts.items(), key=lambda kv: order[kv[0]])))
    print()
    print(f"{'code':<10}{'FTB':<6}{'severity':<10}{'status':<7} flags")
    print("-" * 74)

    for code, status in records:
        fault = code >> 8
        ftb = code & 0xFF
        flags = " ".join(n for b, n in STATUS_BITS if status & b)
        print(f"0x{fault:04X}    0x{ftb:02X}  {dtc_severity(status):<10}"
              f"0x{status:02X}    {flags}")

    print()
    print("Codes are BMW-internal; FTB is the failure-type byte. Cross-reference")
    print("against ISTA for text. 'not-run' means the monitor has not completed")
    print("since the last clear - it is not a present fault.")


def cmd_scan(args) -> None:
    """
    Sweep 0x22 over a DID range and record whatever answers.

    Results are written after every block, and an existing output file is
    reloaded and skipped, so an interrupted scan resumes instead of
    starting over. A scan holds the gateway for minutes and the ZGW
    serves one client at a time - nothing else may be connected.
    """
    out: Dict[int, str] = {}

    if os.path.exists(args.out) and not args.restart:
        with open(args.out) as fh:
            prev = json.load(fh)

        if prev.get("ecu") == args.ecu:
            out = {int(k, 16): v for k, v in prev.get("dids", {}).items()}
            print(f"[+] resuming: {len(out)} DIDs already known in {args.out}")

    done = set()

    if os.path.exists(args.state) and not args.restart:
        with open(args.state) as fh:
            st = json.load(fh)

        if st.get("ecu") == args.ecu:
            done = set(st.get("done_blocks", []))
            print(f"[+] {len(done)} blocks already swept")

    def save() -> None:
        with open(args.out, "w") as fh:
            json.dump(
                {"ecu": args.ecu,
                 "dids": {f"0x{d:04X}": v for d, v in sorted(out.items())}},
                fh, indent=2,
            )

        with open(args.state, "w") as fh:
            json.dump({"ecu": args.ecu, "done_blocks": sorted(done)}, fh)

    client = connect(args)

    if args.session:
        ok = open_session(client, args.ecu)
        print(f"[+] extended session: {ok}")

    lo, hi = (int(x, 0) for x in args.range.split("-"))

    if args.blocks:
        blocks = [int(b, 0) for b in args.blocks.split(",")]
    else:
        blocks = sorted({d >> 8 for d in range(lo, hi + 1)})

    print(f"[+] scanning {len(blocks)} block(s) of 256 DIDs on 0x{args.ecu:02X}")

    try:
        for block in blocks:
            if block in done:
                continue

            for low in range(0x100):
                did = (block << 8) | low

                if not (lo <= did <= hi):
                    continue

                try:
                    resp = client.request_safe(
                        bytes([0x22, did >> 8, did & 0xFF]),
                        timeout=args.scan_timeout, dst=args.ecu,
                    )
                except Exception:
                    continue

                if len(resp) >= 3 and resp[0] == 0x62 and len(resp) > 3:
                    out[did] = resp[3:].hex(" ")

            done.add(block)
            save()
            print(f"  block 0x{block:02X}xx done - {len(out)} DIDs total", flush=True)
    except KeyboardInterrupt:
        print("\n[!] interrupted")
    finally:
        save()
        client.close()

    print(f"\n{len(out)} readable DIDs -> {args.out}")

    scalars = {d: v for d, v in out.items() if len(v.split()) <= 4}
    print(f"{len(scalars)} of them are <= 4 bytes (plausible live scalars)")


def cmd_sparse(args) -> None:
    """
    Sample a few DIDs per 256-block to find which regions are populated,
    so a dense scan only has to visit blocks that contain something.
    """
    client = connect(args)

    if args.session:
        print(f"[+] extended session: {open_session(client, args.ecu)}")

    hits: Dict[int, int] = {}

    for block in range(0x100):
        for low in (0x00, 0x40, 0x80, 0xC0):
            did = (block << 8) | low

            try:
                resp = client.request_safe(
                    bytes([0x22, did >> 8, did & 0xFF]),
                    timeout=args.scan_timeout, dst=args.ecu,
                )
            except Exception:
                continue

            if len(resp) > 3 and resp[0] == 0x62:
                hits[block] = hits.get(block, 0) + 1

    client.close()

    print(f"\n{len(hits)} populated blocks on 0x{args.ecu:02X}:")

    for b, n in sorted(hits.items()):
        print(f"  0x{b:02X}xx  ({n}/4 samples answered)")

    print("\nscan them with:")
    print(f"  python3 egs.py --ecu 0x{args.ecu:02X} scan --blocks "
          + ",".join(f"0x{b:02X}" for b in sorted(hits)))


def cmd_watch(args) -> None:
    """
    Poll the discovered DIDs in a loop and log raw bytes to SQLite, so the
    values can later be correlated against engine channels captured by
    live.py over the same period.
    """
    with open(args.dids) as fh:
        dids = [int(k, 16) for k in json.load(fh)["dids"]]

    if not dids:
        sys.exit("no DIDs in that file - run `scan` first")

    client = connect(args)

    if args.session:
        print(f"[+] extended session: {open_session(client, args.ecu)}")

    db = sqlite3.connect(args.db)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS did_samples (
            ts REAL NOT NULL, ecu INTEGER, did INTEGER, raw BLOB
        );
        CREATE INDEX IF NOT EXISTS did_ts ON did_samples(did, ts);
    """)
    db.commit()

    print(f"[+] watching {len(dids)} DIDs, Ctrl-C to stop")

    pending, n = [], 0

    try:
        while True:
            for did in dids:
                try:
                    resp = client.request_safe(
                        bytes([0x22, did >> 8, did & 0xFF]),
                        timeout=args.timeout, dst=args.ecu,
                    )
                except Exception:
                    continue

                if len(resp) >= 3 and resp[0] == 0x62:
                    pending.append((time.time(), args.ecu, did, resp[3:]))

            if len(pending) >= 200:
                db.executemany(
                    "INSERT INTO did_samples(ts, ecu, did, raw) VALUES (?,?,?,?)",
                    pending,
                )
                db.commit()
                n += len(pending)
                pending.clear()
                print(f"\r    {n} samples", end="", flush=True)

            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if pending:
            db.executemany(
                "INSERT INTO did_samples(ts, ecu, did, raw) VALUES (?,?,?,?)", pending
            )
            n += len(pending)

        db.commit()
        db.close()
        client.close()
        print(f"\n[+] {n} DID samples written to {args.db}")


def cmd_correlate(args) -> None:
    """
    For every DID, interpret each byte offset as u8 and u16-BE, and
    correlate that field against each engine channel logged by live.py.
    A field that tracks road speed is the output shaft; one that tracks
    engine rpm is the input shaft; a slow monotonic ramp is oil temp.
    """
    db = sqlite3.connect(args.db)

    engine: Dict[str, List[Tuple[float, float]]] = {}

    for key, ts, value in db.execute(
        "SELECT p.key, s.ts, s.value FROM samples s JOIN params p ON p.id = s.param_id "
        "ORDER BY s.ts"
    ):
        engine.setdefault(key, []).append((ts, value))

    if not engine:
        sys.exit(f"no engine samples in {args.db} - run live.py against the car first")

    have = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='did_samples'"
    ).fetchone()

    if not have:
        db.close()
        sys.exit(f"{args.db} has no did_samples table - run `watch` first")

    dids: Dict[int, List[Tuple[float, bytes]]] = {}

    for did, ts, raw in db.execute("SELECT did, ts, raw FROM did_samples ORDER BY ts"):
        dids.setdefault(did, []).append((ts, bytes(raw)))

    db.close()

    if not dids:
        sys.exit("no DID samples - run `watch` first")

    def at(seriesx, t):
        lo, hi = 0, len(seriesx) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if seriesx[mid][0] < t:
                lo = mid + 1
            else:
                hi = mid
        return seriesx[lo][1]

    def pearson(xs, ys):
        n = len(xs)
        if n < 8:
            return 0.0
        mx, my = sum(xs) / n, sum(ys) / n
        num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
        dx = sum((a - mx) ** 2 for a in xs) ** 0.5
        dy = sum((b - my) ** 2 for b in ys) ** 0.5
        return 0.0 if dx * dy == 0 else num / (dx * dy)

    print(f"[+] {len(dids)} DIDs vs {len(engine)} engine channels "
          f"(|r| >= {args.min_r})\n")

    results = []

    for did, samples in sorted(dids.items()):
        width = min(len(s[1]) for s in samples)

        for off in range(width):
            for size in (1, 2):
                if off + size > width:
                    continue

                field = [
                    (ts, int.from_bytes(raw[off:off + size], "big"))
                    for ts, raw in samples
                ]

                if len({v for _, v in field}) < 3:
                    continue        # constant - carries no information

                for key, eng in engine.items():
                    xs = [v for _, v in field]
                    ys = [at(eng, ts) for ts, _ in field]
                    r = pearson(xs, ys)

                    if abs(r) >= args.min_r:
                        results.append((abs(r), did, off, size, key, r,
                                        min(xs), max(xs)))

    results.sort(reverse=True)

    if not results:
        print("no correlations above threshold - try a longer drive with more"
              " variation, or lower --min-r")
        return

    print(f"{'DID':<8}{'off':<5}{'sz':<4}{'channel':<12}{'r':>7}   value range")
    print("-" * 62)

    for _, did, off, size, key, r, lo, hi in results[:args.top]:
        print(f"0x{did:04X}  {off:<5}{'u8' if size == 1 else 'u16':<4}"
              f"{key:<12}{r:>7.3f}   {lo}..{hi}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip"); ap.add_argument("--local-ip")
    ap.add_argument("--ecu", type=lambda s: int(s, 0), default=0x18)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--scan-timeout", type=float, default=0.25)
    ap.add_argument("--session", action="store_true",
                    help="send 0x10 0x03 extended diagnostic session first")
    ap.add_argument("--db", default="telemetry.db")

    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("find")
    sub.add_parser("dtc")

    sub.add_parser("sparse")

    sc = sub.add_parser("scan")
    sc.add_argument("--range", default="0x0000-0xFFFF")
    sc.add_argument("--blocks", default=None,
                    help="comma-separated 256-blocks, e.g. 0x30,0x41 (from `sparse`)")
    sc.add_argument("--out", default="egs_dids.json")
    sc.add_argument("--state", default=None)
    sc.add_argument("--restart", action="store_true",
                    help="ignore previous results and start over")

    w = sub.add_parser("watch")
    w.add_argument("--dids", default="egs_dids.json")
    w.add_argument("--interval", type=float, default=0.1)

    c = sub.add_parser("correlate")
    c.add_argument("--min-r", type=float, default=0.85)
    c.add_argument("--top", type=int, default=40)

    args = ap.parse_args()

    if getattr(args, "state", None) is None and args.cmd == "scan":
        args.state = args.out + ".state"

    return {
        "find": cmd_find, "dtc": cmd_dtc, "scan": cmd_scan,
        "sparse": cmd_sparse, "watch": cmd_watch, "correlate": cmd_correlate,
    }[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
