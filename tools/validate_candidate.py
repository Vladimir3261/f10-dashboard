#!/usr/bin/env python3
"""
validate_candidate.py - supervised, read-only on-car validation of a
candidate diagnostic mapping.

This is the step that turns a `candidate` mapping into a `locally_verified`
one (or `rejected`). It is deliberately NOT part of live.py and NOT part
of the polling loop: it runs one candidate request at a time, by hand,
records exactly what the car said, and never writes to the vehicle.

    # discover the car and confirm the engine ECU, send nothing else
    python3 tools/validate_candidate.py identify

    # run ONE request from a candidate file (setup sequence + poll)
    python3 tools/validate_candidate.py run \
        mappings/candidates/bmw/dde/n47/d72n47a0_dynamic.yaml n47.d72.dyn.4517

    # a whole file, one request at a time with a confirmation between each
    python3 tools/validate_candidate.py run \
        mappings/candidates/bmw/dde/n47/d72n47a0_dynamic.yaml --all --step

Safety
------
* Every payload is checked against a READ-ONLY service allowlist before
  it leaves the machine. Anything else aborts the run - a candidate that
  tries to send 0x2E/0x2F/0x31/0x14/0x27/0x34.. never reaches the car.
* Exactly one dynamic (0xF303) request runs per invocation unless you
  pass --all, and even then they run strictly in sequence, each fully
  cleared before the next - never two defines armed at once.
* A negative response (7F .. NRC) is recorded as a RESULT, not an error:
  "the ECU rejected this on this variant" is exactly the evidence a
  validation run exists to gather.
* Nothing here starts a diagnostic session or sends TesterPresent unless
  you ask for it; the reads targeted here work in the default session.

Output / artifacts
------------------
EVERY run writes a complete, timestamped artifact set so nothing about a
session is lost:

  validation-runs/<UTC-timestamp>-<cmd>/   (tracked in git)
      run.json      full machine record: environment, every frame
                    (tx/rx/nrc/latency), decoded values, outcomes
      summary.md    human-readable: what was sent, what came back,
                    what decoded, and the plausibility questions to
                    answer before promoting anything
      frames.ndjson one JSON object per wire frame, in order

  local/validation-runs-raw/<...>/         (gitignored)
      the same record UNREDACTED - it holds the VIN read from the
      gateway, which must not enter version control (see README).

The tracked copy is VIN-redacted. Nothing is auto-promoted: the
artifacts are the evidence you review before editing a mapping's
`verification.status` to `locally_verified` or `rejected`.
"""

import argparse
import importlib.util
import json
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_spec = importlib.util.spec_from_file_location(
    "live", os.path.join(_ROOT, "live.py")
)
live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(live)

from bmwdiag.mapping import MappingRegistry, load_file          # noqa: E402
from bmwdiag.mapping.decoder import decode_response, match_prefix  # noqa: E402
from bmwdiag.mapping.registry import AllCapabilities            # noqa: E402
from bmwdiag.protocol.errors import NegativeResponse           # noqa: E402
from bmwdiag.protocol.request import build_payload             # noqa: E402


# ------------------------------------------------------- read-only gate


#
# The allowlist itself lives in bmwdiag/protocol/safety.py and is shared
# with the normal in-car runtime - one policy, one implementation. This
# tool used to carry its own copy, which meant the supervised validation
# path was protected while live.py was not, and the two lists could
# drift. The local names are kept as aliases so this file reads as it
# always has; a test asserts they ARE the shared objects, not copies.
#
from bmwdiag.protocol.safety import (                           # noqa: E402
    DDD_SUBFUNCTIONS as DDD_READ_SUBFUNCTIONS,
    OBSERVATIONAL_SERVICES as READ_ONLY_SERVICES,
    WRITE_SERVICES,
    UnsafePayload,
    assert_observational as assert_read_only,
)


# ------------------------------------------------------------ transport


class GatedTransport:
    """
    Wraps a DiagnosticTransport and refuses to send anything that is not
    on the read-only allowlist. This is the single choke point every
    frame passes through, so the safety property does not depend on any
    individual call site remembering to check.
    """

    def __init__(self, inner, log: List[Dict]):
        self.inner = inner
        self.log = log

    def request(self, payload: bytes, *, dst: int,
                timeout: Optional[float] = None) -> bytes:
        assert_read_only(bytes(payload))

        started = time.monotonic()
        negative: Optional[NegativeResponse] = None
        response = b""

        try:
            response = self.inner.request(payload, dst=dst, timeout=timeout)
        except NegativeResponse as exc:
            #
            # A negative response is data. The client raises it as a
            # structured category with the NRC as a field; capture it
            # rather than letting it abort the run. Everything else
            # (a dead link, a timeout, a refusal to route) propagates.
            #
            negative = exc

        self.log.append({
            "dst": f"0x{dst:02X}",
            "tx": bytes(payload).hex(" "),
            "rx": response.hex(" ") if response else None,
            #: The NRC as a NUMBER, plus its hex form and ISO 14229 name
            #: for reading. Never the exception's sentence: an artifact is
            #: compared across runs, and prose is not stable.
            "nrc": None if negative is None else negative.nrc,
            "nrc_hex": None if negative is None else negative.nrc_hex,
            "nrc_name": None if negative is None else negative.name,
            "service": (
                None if negative is None else f"0x{negative.service:02X}"
            ),
            "ms": round((time.monotonic() - started) * 1000, 1),
        })

        return response


# ----------------------------------------------------------- discovery


def connect_engine(args) -> Tuple[live.HsfzClient, "live.EcuInfo"]:
    """Discover the gateway and the engine ECU, sending only reads."""
    local = args.local_ip or live.find_link_local_ip()
    ip, vin = args.ip, args.vin

    if ip is None:
        if not local:
            sys.exit("no 169.254.x.x interface found - pass --local-ip or --ip")

        ip, vin = live.discover(local)
        print(f"[+] gateway {ip} (VIN {vin or '?'})")

    #
    # Reuse live.py's discovery: it sends only 0x01/0x09 reads, and it
    # confirms the engine by capability (PID 0x0C), never by address.
    #
    client, engine, ecus = live.connect_and_discover(
        ip, local, args, report=lambda m: print(f"[scan] {m}"),
    )

    print(f"[+] engine ECU {engine.label()} - "
          f"{len(engine.supported)} PIDs advertised")

    others = [e.label() for e in ecus if e.addr != engine.addr]

    if others:
        print(f"[+] other OBD-capable ECUs: {', '.join(others)}")

    return client, engine


def read_ident(client: live.HsfzClient, addr: int) -> Dict[str, str]:
    """
    Read-only identification of one ECU: the honest way to resolve the
    DDE variant is to ask the ECU, then compare offline against the
    SGBD IDENT results - never to guess from the address.
    """
    out: Dict[str, str] = {}

    reads = {
        "vin": bytes([0x22, 0xF1, 0x90]),
        "sysname_f197": bytes([0x22, 0xF1, 0x97]),
        "hw_f191": bytes([0x22, 0xF1, 0x91]),
        "sw_f194": bytes([0x22, 0xF1, 0x94]),
        "supplier_f18a": bytes([0x22, 0xF1, 0x8A]),
        "ecu_name_0900": bytes([0x09, 0x0A]),
    }

    for key, payload in reads.items():
        assert_read_only(payload)

        try:
            resp = client.request(payload, timeout=1.0, dst=addr)
        except NegativeResponse as exc:
            out[key] = f"(NRC {exc.nrc_hex} {exc.name})"
            continue
        except Exception:
            out[key] = "(no answer)"
            continue

        text = "".join(chr(b) for b in resp[3:] if 32 <= b < 127).strip()
        out[key] = text or resp.hex(" ")

    return out


# ------------------------------------------------------------ commands


TRACKED_RUNS = os.path.join(_ROOT, "validation-runs")
RAW_RUNS = os.path.join(_ROOT, "local", "validation-runs-raw")

#: Keys whose values may carry a VIN or other per-car identifier. In the
#: tracked artifacts these are replaced with a redaction marker; the raw
#: copy under local/ keeps them.
VIN_BEARING_KEYS = {"vin", "sysname_f197", "supplier_f18a"}


def _looks_like_vin(text: str) -> bool:
    #
    # A 17-char alnum run (BMW VINs are 17 chars). Conservative: redact
    # anything VIN-shaped rather than risk leaking one.
    #
    import re
    return bool(re.search(r"[A-HJ-NPR-Z0-9]{17}", str(text)))


def _redact(obj):
    """Deep-copy a record with VIN-bearing values masked."""
    if isinstance(obj, dict):
        out = {}

        for key, value in obj.items():
            if key in VIN_BEARING_KEYS and isinstance(value, str) and value:
                out[key] = "<redacted: recorded in local/ only>"
            elif isinstance(value, str) and _looks_like_vin(value):
                out[key] = "<redacted: VIN-shaped>"
            else:
                out[key] = _redact(value)

        return out

    if isinstance(obj, list):
        return [_redact(v) for v in obj]

    return obj


class RunArtifacts:
    """
    One run -> one tracked artifact directory + one raw local directory.

    Everything about the session lands here: environment, every wire
    frame, decoded values and outcomes. The tracked copy is redacted;
    the raw copy (VIN included) stays under the gitignored local/ tree.
    """

    def __init__(self, cmd: str):
        self.stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self.slug = f"{self.stamp}-{cmd}"
        self.cmd = cmd
        self.records: List[Dict] = []
        self.meta: Dict = {
            "kind": cmd,
            "started_utc": self.stamp,
            "tool": "tools/validate_candidate.py",
            "read_only": True,
            "allowlist": sorted(hex(s) for s in READ_ONLY_SERVICES | {0x2C}),
            "argv": sys.argv[1:],
            "host_time_local": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        }

    def set_environment(self, **kw) -> None:
        self.meta.setdefault("environment", {}).update(kw)

    def add(self, record: Dict) -> None:
        self.records.append(record)

    # -- rendering --------------------------------------------------

    def _run_json(self) -> Dict:
        return {"meta": self.meta, "records": self.records}

    def _frames(self) -> List[Dict]:
        frames: List[Dict] = []

        for record in self.records:
            for frame in record.get("frames", []):
                frames.append({"request": record.get("request"), **frame})

        return frames

    def _summary_md(self, redacted: Dict) -> str:
        m = redacted["meta"]
        lines = [
            f"# Validation run {self.slug}",
            "",
            f"- **Command:** `{self.cmd}`  (`{' '.join(m['argv'])}`)",
            f"- **When (UTC):** {m['started_utc']}",
            "- **Read-only:** yes — every frame passed the service "
            f"allowlist `{', '.join(m['allowlist'])}`",
        ]

        env = m.get("environment", {})

        if env:
            lines.append(f"- **Gateway:** {env.get('gateway', '?')}  "
                         f"**Engine ECU:** {env.get('ecu', '?')} "
                         f"({env.get('supported_pid_count', '?')} PIDs)")

        lines.append("")

        for record in redacted["records"]:
            lines.append(f"## {record.get('request', record.get('kind'))}")
            lines.append("")

            if record.get("kind") == "identify":
                lines.append("Read-only identification:")
                lines.append("")

                for key, value in record.get("ident", {}).items():
                    lines.append(f"- `{key}` = {value}")

                lines.append("")
                lines.append(f"> {record.get('note', '')}")
                lines.append("")
                continue

            if record.get("kind") == "sweep_channel":
                lo, hi = record.get("min"), record.get("max")
                unit = record.get("unit", "")
                lines.append(
                    f"- range **{lo} → {hi}** {unit} "
                    f"(span {record.get('span')}, last {record.get('last')}, "
                    f"{record.get('samples')} samples)"
                )
                lines.append("- [ ] moved as expected under throttle? "
                             "(boost/rail/MAF rise, pedal tracks foot)")
                lines.append("")
                continue

            outcome = record.get("outcome", record.get("aborted", "?"))
            lines.append(f"- **ECU:** {record.get('ecu_addr', '?')}")
            lines.append(f"- **Outcome:** `{outcome}`")

            if record.get("nrc") is not None:
                lines.append(f"- **Negative response:** {_nrc_text(record)} "
                             "(recorded as evidence — the ECU rejects this "
                             "identifier on this variant)")

            lines.append("")
            lines.append("| # | dir | bytes | nrc | ms |")
            lines.append("|---|---|---|---|---|")

            for i, frame in enumerate(record.get("frames", [])):
                lines.append(
                    f"| {i} | tx→{frame['dst']} | `{frame['tx']}` | "
                    f"{_nrc_text(frame) if frame.get('nrc') is not None else '-'}"
                    f" | {frame.get('ms', '-')} |"
                )
                if frame.get("rx"):
                    lines.append(f"| {i} | rx | `{frame['rx']}` | - | - |")

            lines.append("")

            if record.get("signals"):
                lines.append("**Decoded:**")
                lines.append("")

                for key, value in record["signals"].items():
                    lines.append(f"- `{key}` = **{value}**")

                lines.append("")
                lines.append(f"- [ ] {record.get('plausibility_note', '')}")
                lines.append("")

        lines += [
            "## Promotion decision",
            "",
            "For each decoded request, once the plausibility box above is",
            "checked and true, edit the candidate mapping's",
            "`verification.status` to `locally_verified` and record the",
            "vehicle label `F10-520d-dev`. If a request returned a",
            "negative response or an implausible value, set it `rejected`",
            "with the NRC/reason. Nothing here is promoted automatically.",
            "",
        ]

        return "\n".join(lines) + "\n"

    def write(self) -> Tuple[str, str]:
        run_json = self._run_json()
        redacted = _redact(run_json)

        tracked = os.path.join(TRACKED_RUNS, self.slug)
        raw = os.path.join(RAW_RUNS, self.slug)
        os.makedirs(tracked, exist_ok=True)
        os.makedirs(raw, exist_ok=True)

        # tracked (redacted)
        with open(os.path.join(tracked, "run.json"), "w", encoding="utf-8") as fh:
            json.dump(redacted, fh, indent=2, ensure_ascii=False)

        with open(os.path.join(tracked, "frames.ndjson"), "w",
                  encoding="utf-8") as fh:
            for frame in _redact(self._frames()):
                fh.write(json.dumps(frame, ensure_ascii=False) + "\n")

        with open(os.path.join(tracked, "summary.md"), "w",
                  encoding="utf-8") as fh:
            fh.write(self._summary_md(redacted))

        # raw (unredacted, gitignored)
        with open(os.path.join(raw, "run.json"), "w", encoding="utf-8") as fh:
            json.dump(run_json, fh, indent=2, ensure_ascii=False)

        return (os.path.relpath(tracked, _ROOT), os.path.relpath(raw, _ROOT))


def cmd_identify(args) -> int:
    artifacts = RunArtifacts("identify")
    client, engine = connect_engine(args)

    try:
        ident = read_ident(client, engine.addr)

        print("\n[+] engine ECU identification (read-only):")

        for key, value in ident.items():
            print(f"    {key:16s} {value}")

        artifacts.set_environment(
            gateway=getattr(client, "ip", "?"),
            ecu=engine.label(),
            ecu_addr=f"0x{engine.addr:02X}",
            supported_pid_count=len(engine.supported),
        )
        artifacts.add({
            "kind": "identify",
            "request": "identify",
            "ecu": engine.label(),
            "ecu_addr": f"0x{engine.addr:02X}",
            "supported_pids": sorted(f"0x{p:02X}" for p in engine.supported),
            "ident": ident,
            "note": "compare hw/sw/supplier against the d_motor IDENT "
                    "results in an offline EDIABAS/ediabasx oracle to "
                    "resolve the exact DDE SGBD variant",
        })
        tracked, raw = artifacts.write()

        print(f"\n[+] artifacts: {tracked}/  (redacted, tracked)")
        print(f"[+] raw copy:  {raw}/  (VIN included, gitignored)")
        print("[i] next: resolve the variant offline, then `run` one "
              "candidate request.")

        return 0
    finally:
        client.close()


def _run_one(client, engine, request, decode_ok: bool) -> Dict:
    """Send one request's setup sequence + poll; decode if it answered."""
    log: List[Dict] = []
    transport = GatedTransport(live.HsfzTransport(client), log)

    targets = {"discovered_engine": engine.addr}
    poll = build_payload(request)
    dst = request.target.resolve(targets) or engine.addr

    print(f"\n[>] {request.id}  ->  0x{dst:02X}")

    for i, frame in enumerate(request.setup):
        print(f"    setup[{i}] {bytes(frame).hex(' ')}")

    print(f"    poll     {poll.hex(' ')}")

    result: Dict = {
        "kind": "run",
        "request": request.id,
        "ecu": engine.label(),
        "ecu_addr": f"0x{dst:02X}",
        "expected_prefix": bytes(request.response.prefix).hex(" ") or None,
        "signals": {},
    }

    try:
        for frame in request.setup:
            transport.request(bytes(frame), dst=dst, timeout=2.0)

        response = transport.request(poll, dst=dst, timeout=2.0)
    except UnsafePayload as exc:
        result["aborted"] = f"read-only gate: {exc}"
        result["frames"] = log
        print(f"    [!] ABORTED by read-only gate: {exc}")
        return result

    result["frames"] = log
    last = log[-1] if log else {}

    if last.get("nrc") is not None:
        result["outcome"] = "negative_response"
        result["nrc"] = last["nrc"]
        result["nrc_hex"] = last.get("nrc_hex")
        result["nrc_name"] = last.get("nrc_name")
        result["service"] = last.get("service")
        print(f"    [=] NEGATIVE: NRC {_nrc_text(last)}")
        print("        (recorded - this is evidence the ECU rejects this "
              "on this variant)")
        return result

    if not response:
        result["outcome"] = "no_answer"
        print("    [=] no answer (timeout)")
        return result

    print(f"    rx       {response.hex(' ')}")

    #
    # Decode with the SAME engine the runtime uses, so a value that
    # decodes here is a value the dashboard would show.
    #
    try:
        match_prefix(request, response)
    except Exception as exc:
        result["outcome"] = "prefix_mismatch"
        result["detail"] = str(exc)
        print(f"    [=] answered, but not the expected shape: {exc}")
        return result

    try:
        values = decode_response(request, response)
    except Exception as exc:
        result["outcome"] = "decode_error"
        result["detail"] = str(exc)
        print(f"    [=] prefix ok, decode failed: {exc}")
        return result

    result["outcome"] = "decoded"
    result["signals"] = {k: v for k, v in values.items()}

    for key, value in values.items():
        signal = next((s for s in request.signals if s.key == key), None)
        unit = signal.unit if signal else ""
        print(f"    [OK] {key} = {value} {unit}")

    result["plausibility_note"] = args_note()

    return result


def _nrc_text(record: Dict) -> str:
    """`0x31 requestOutOfRange` from a frame or result record."""
    code = record.get("nrc")
    hexed = record.get("nrc_hex") or (
        f"0x{int(code):02X}" if isinstance(code, int) else str(code)
    )
    name = record.get("nrc_name")

    return f"{hexed} {name}" if name else hexed


def args_note() -> str:
    return ("FILL IN: does this value match the physical state? "
            "(e.g. oil ~ coolant when cold, soot measured ~ modelled, "
            "pressure rises with rpm)")


def cmd_run(args) -> int:
    mapping = load_file(args.path)

    if mapping.production:
        print("[!] refusing: this is a production mapping, validate "
              "candidates only", file=sys.stderr)
        return 2

    if args.request and args.all:
        sys.exit("pass a request id OR --all, not both")

    if args.all:
        requests = list(mapping.requests)
    else:
        if not args.request:
            print("[i] requests in this file:")

            for r in mapping.requests:
                dyn = " (dynamic 0xF303 - runs alone)" if r.setup else ""
                print(f"    {r.id}{dyn}")

            return 0

        requests = [r for r in mapping.requests if r.id == args.request]

        if not requests:
            sys.exit(f"no request {args.request!r} in {args.path}")

    # Pre-flight: every frame of every selected request is read-only,
    # checked BEFORE we touch the car.
    for request in requests:
        for frame in list(request.setup) + [build_payload(request)]:
            try:
                assert_read_only(bytes(frame))
            except UnsafePayload as exc:
                sys.exit(f"[!] {request.id}: {exc}")

    artifacts = RunArtifacts("run")
    client, engine = connect_engine(args)
    artifacts.set_environment(
        gateway=getattr(client, "ip", "?"),
        ecu=engine.label(),
        ecu_addr=f"0x{engine.addr:02X}",
        supported_pid_count=len(engine.supported),
        mapping_file=os.path.relpath(args.path, _ROOT),
    )
    results: List[Dict] = []

    try:
        for i, request in enumerate(requests):
            if args.step and i > 0:
                reply = input("\n[?] continue to the next request? [y/N] ")

                if reply.strip().lower() not in ("y", "yes"):
                    print("[i] stopping at your request.")
                    break

            result = _run_one(client, engine, request, decode_ok=True)
            artifacts.add(result)
            results.append(result)

            #
            # A dynamic define leaves F303 pointing at this request's
            # source. Clear it before the next one so two defines are
            # never live at once (belt and braces - each request also
            # re-clears in its own setup).
            #
            if request.setup and i + 1 < len(requests):
                try:
                    GatedTransport(live.HsfzTransport(client), []).request(
                        bytes.fromhex("2C 03 F3 03"),
                        dst=request.target.resolve(
                            {"discovered_engine": engine.addr}
                        ) or engine.addr,
                        timeout=2.0,
                    )
                except Exception:
                    pass
    finally:
        client.close()

    tracked, raw = artifacts.write()
    ok = [r for r in results if r.get("outcome") == "decoded"]

    print(f"\n[+] {len(ok)}/{len(results)} request(s) decoded a value.")
    print(f"[+] artifacts: {tracked}/  (redacted, tracked — summary.md, "
          "run.json, frames.ndjson)")
    print(f"[+] raw copy:  {raw}/  (gitignored)")
    print("[i] review summary.md, fill in each plausibility box, and only "
          "then promote a mapping to verification.status: locally_verified "
          "(or rejected).")

    return 0


def _poll_value(transport, request, dst) -> Dict:
    """One setup+poll+decode round, lean - for the sweep loop."""
    #
    # Re-arm every round: in a sweep across several F303 channels the
    # define must precede its own poll, and re-sending it for a single
    # channel is cheap and keeps the loop correct without shared state.
    #
    for frame in request.setup:
        transport.request(bytes(frame), dst=dst, timeout=2.0)

    response = transport.request(build_payload(request), dst=dst, timeout=2.0)

    try:
        match_prefix(request, response)
        values = decode_response(request, response)
    except Exception:
        return {}

    return values


def cmd_sweep(args) -> int:
    """
    Poll a set of requests in a loop for a fixed duration.

    This is the capture to run while you work the throttle: it samples
    the chosen channels round-robin at a steady cadence and records the
    whole time series, so a transient (boost building, rail climbing,
    pedal moving) shows up as a min->max range and a trace, not a single
    frozen number.
    """
    mapping = load_file(args.path)

    if mapping.production:
        print("[!] refusing: validate candidates only", file=sys.stderr)
        return 2

    if args.all:
        requests = list(mapping.requests)
    elif args.request:
        requests = [r for r in mapping.requests if r.id in args.request]
        missing = set(args.request) - {r.id for r in requests}

        if missing:
            sys.exit(f"no such request(s): {', '.join(sorted(missing))}")
    else:
        print("[i] pass request id(s) or --all. requests in this file:")

        for r in mapping.requests:
            print(f"    {r.id}")

        return 0

    for request in requests:
        for frame in list(request.setup) + [build_payload(request)]:
            try:
                assert_read_only(bytes(frame))
            except UnsafePayload as exc:
                sys.exit(f"[!] {request.id}: {exc}")

    artifacts = RunArtifacts("sweep")
    client, engine = connect_engine(args)
    dst = engine.addr
    transport = GatedTransport(live.HsfzTransport(client), [])

    keys = [s.key for r in requests for s in r.signals]
    series: Dict[str, list] = {k: [] for k in keys}
    stats: Dict[str, Dict] = {}
    units = {s.key: s.unit for r in requests for s in r.signals}

    print(f"\n[i] sweeping {len(requests)} request(s) for {args.seconds:.0f}s "
          f"- WORK THE THROTTLE NOW. Ctrl-C to stop early.\n")

    started = time.monotonic()
    rounds = 0

    try:
        while time.monotonic() - started < args.seconds:
            t = round(time.monotonic() - started, 2)

            for request in requests:
                values = _poll_value(transport, request, dst)

                for key, value in values.items():
                    series[key].append([t, value])
                    st = stats.setdefault(key, {"min": value, "max": value,
                                                "last": value})
                    st["min"] = min(st["min"], value)
                    st["max"] = max(st["max"], value)
                    st["last"] = value

            rounds += 1
            line = "  ".join(
                f"{k}={stats[k]['last']:.1f}" for k in keys if k in stats
            )
            print(f"  t={t:5.1f}s  {line}", flush=True)

            if args.interval > 0:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[i] stopped early.")
    finally:
        client.close()

    for key in keys:
        st = stats.get(key, {})
        artifacts.add({
            "kind": "sweep_channel",
            "request": key,
            "signal": key,
            "unit": units.get(key, ""),
            "outcome": "swept" if st else "no_answer",
            "min": st.get("min"),
            "max": st.get("max"),
            "last": st.get("last"),
            "span": (round(st["max"] - st["min"], 3)
                     if st else None),
            "samples": len(series[key]),
            "series": series[key],
        })

    artifacts.set_environment(
        gateway=getattr(client, "ip", "?"), ecu=engine.label(),
        ecu_addr=f"0x{dst:02X}", supported_pid_count=len(engine.supported),
        mapping_file=os.path.relpath(args.path, _ROOT),
        seconds=args.seconds, rounds=rounds,
    )
    tracked, raw = artifacts.write()

    print(f"\n[+] {rounds} rounds. Ranges (min -> max):")

    for key in keys:
        st = stats.get(key)

        if st:
            moved = st["max"] - st["min"]
            flag = "  <== MOVED" if moved > abs(st["min"]) * 0.05 + 0.5 else ""
            print(f"    {key:22s} {st['min']:8.1f} -> {st['max']:8.1f} "
                  f"{units.get(key,''):7s}{flag}")

    print(f"\n[+] artifacts: {tracked}/  (redacted, tracked)")
    print(f"[+] raw copy:  {raw}/  (gitignored)")

    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="supervised read-only on-car candidate validation",
    )
    ap.add_argument("--ip", default=None, help="gateway IP, skips discovery")
    ap.add_argument("--local-ip", default=None, help="local 169.254.x.x address")
    ap.add_argument("--vin", default=None)
    ap.add_argument("--ecu", type=lambda s: int(s, 0), default=None,
                    help="force the engine ECU address (else discovered)")
    ap.add_argument("--scan-timeout", type=float, default=0.3)
    ap.add_argument("--scan-full", action="store_true")

    sub = ap.add_subparsers(dest="cmd", required=True)

    p_id = sub.add_parser("identify", help="discover + read-only ECU identity")
    p_id.set_defaults(func=cmd_identify)

    p_run = sub.add_parser("run", help="run one candidate request (or --all)")
    p_run.add_argument("path", help="a candidate mapping file")
    p_run.add_argument("request", nargs="?", help="the request id to run")
    p_run.add_argument("--all", action="store_true",
                       help="run every request, one at a time, in sequence")
    p_run.add_argument("--step", action="store_true",
                       help="pause for confirmation between requests")
    p_run.set_defaults(func=cmd_run)

    p_sw = sub.add_parser(
        "sweep",
        help="poll request(s) in a loop while you work the throttle",
    )
    p_sw.add_argument("path", help="a candidate mapping file")
    p_sw.add_argument("request", nargs="*",
                      help="request id(s) to sweep (default: use --all)")
    p_sw.add_argument("--all", action="store_true",
                      help="sweep every request in the file")
    p_sw.add_argument("--seconds", type=float, default=20.0,
                      help="how long to sweep (default 20)")
    p_sw.add_argument("--interval", type=float, default=0.0,
                      help="extra pause between rounds in s (default 0 - "
                           "poll as fast as the link allows)")
    p_sw.set_defaults(func=cmd_sweep)

    return ap


def main() -> int:
    args = build_parser().parse_args()

    print("=" * 60)
    print("candidate validation - READ-ONLY, one request at a time")
    print("=" * 60)
    print("[i] live.py must NOT be running: the ZGW serves one HSFZ "
          "client at a time.\n")

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n[i] interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
