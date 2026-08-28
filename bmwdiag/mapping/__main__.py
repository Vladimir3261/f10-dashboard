"""
Standalone mapping CLI, for development. Never needs a vehicle.

    python3 -m bmwdiag.mapping validate mappings/
    python3 -m bmwdiag.mapping list mappings/
    python3 -m bmwdiag.mapping show mappings/obd/engine.yaml
    python3 -m bmwdiag.mapping plan mappings/obd --slow-every 10
    python3 -m bmwdiag.mapping decode mappings/obd/engine.yaml rpm "41 0C 0C 3C"
    python3 -m bmwdiag.mapping request mappings/obd/engine.yaml obd.mode01.0C

This is deliberately separate from live.py's runtime CLI: mapping work is
a research/authoring activity and should never require the vehicle stack.
"""

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from .decoder import decode_response, decode_signal, match_prefix
from .errors import MappingError
from .loader import iter_mapping_files, load_file
from .model import MappingFile
from .polling import PollingClassDef, PollingPlan, resolve_classes
from .registry import AllCapabilities, MappingRegistry


def _load(paths: List[str], production_only: bool = False) -> List[MappingFile]:
    out: List[MappingFile] = []

    for path in paths:
        for found in iter_mapping_files(path):
            mapping = load_file(found)

            if production_only and not mapping.production:
                continue

            out.append(mapping)

    return out


def _parse_bytes(text: str) -> bytes:
    cleaned = text.replace("0x", "").replace(",", " ").replace(":", " ")

    if " " in cleaned.strip():
        return bytes(int(p, 16) for p in cleaned.split())

    return bytes.fromhex(cleaned.strip())


def _flag(mapping: MappingFile) -> str:
    return "" if mapping.production else "  [non-production]"


# ------------------------------------------------------------- commands


def cmd_validate(args) -> int:
    files = iter_mapping_files_all(args.paths)

    if not files:
        print("no mapping files found", file=sys.stderr)
        return 1

    failures = 0

    for path in files:
        try:
            mapping = load_file(path)
        except MappingError as exc:
            failures += 1
            print(f"FAIL {path}\n     {exc}")
            continue

        print(
            f"ok   {path}  ({mapping.id}, {len(mapping.requests)} requests, "
            f"{len(mapping.signals)} signals, {len(mapping.derived)} derived)"
            f"{_flag(mapping)}"
        )

    #
    # Cross-file collisions only show up once everything is in one
    # registry, so validate the whole set as well as each file.
    #
    if not failures:
        try:
            MappingRegistry(_load(args.paths))
        except MappingError as exc:
            failures += 1
            print(f"FAIL registry\n     {exc}")

    print(f"\n{len(files) - failures}/{len(files)} file(s) valid")

    return 1 if failures else 0


def iter_mapping_files_all(paths: List[str]) -> List[str]:
    out: List[str] = []

    for path in paths:
        out.extend(iter_mapping_files(path))

    return out


def cmd_list(args) -> int:
    mappings = _load(args.paths)

    for mapping in mappings:
        print(f"{mapping.id}  ({mapping.source_path}){_flag(mapping)}")
        print(f"    ecu family      {mapping.ecu.family}")
        print(f"    target          {mapping.ecu.target.describe()}")
        print(f"    verification    {mapping.verification.status}")
        print(f"    source          {mapping.provenance.type}")

        for request in mapping.requests:
            print(
                f"    request {request.id:<24} {request.protocol:<4} "
                f"-> {request.target.describe():<20} "
                f"[{request.polling_class}]"
            )

            for signal in request.signals:
                marker = "" if signal.verification.status == "verified" else \
                    f" ({signal.verification.status})"
                print(
                    f"        {signal.key:<16} {signal.decode.type:<12} "
                    f"{signal.unit or '-':<6}{marker}"
                )

        for derived in mapping.derived:
            print(
                f"    derived {derived.key:<24} {derived.operation} "
                f"<- {', '.join(n for _, n in derived.inputs)}"
            )

        print()

    return 0


def cmd_show(args) -> int:
    mapping = load_file(args.path)
    payload: Dict[str, Any] = {
        "schema_version": mapping.schema_version,
        "id": mapping.id,
        "version": mapping.version,
        "description": mapping.description,
        "production": mapping.production,
        "source": mapping.provenance.as_dict(),
        "verification": mapping.verification.as_dict(),
        "ecu": {
            "family": mapping.ecu.family,
            "target": mapping.ecu.target.describe(),
            "sgbd": mapping.ecu.sgbd,
            "variant": mapping.ecu.variant,
            "hardware": mapping.ecu.hardware,
            "software": mapping.ecu.software,
            "match": [c.as_dict() for c in mapping.ecu.match],
        },
        "requests": [],
        "derived": [],
    }

    from ..protocol.request import build_payload

    for request in mapping.requests:
        try:
            wire = build_payload(request).hex(" ")
        except MappingError:
            wire = None

        payload["requests"].append({
            "id": request.id,
            "protocol": request.protocol,
            "service": request.service,
            "pid": request.pid,
            "did": request.did,
            "payload": wire,
            "target": request.target.describe(),
            "polling_class": request.polling_class,
            "expect_prefix": bytes(request.response.prefix).hex(" "),
            "data_length": request.response.data_length,
            "requires": [c.as_dict() for c in request.requires],
            "source": request.provenance.as_dict(),
            "verification": request.verification.as_dict(),
            "signals": [
                {
                    "key": s.key,
                    "source_name": s.source_name,
                    "label": s.label,
                    "unit": s.unit,
                    "decode": s.decode.type,
                    "offset": s.decode.offset,
                    "display": {
                        "digits": s.display.digits,
                        "lo": s.display.lo,
                        "hi": s.display.hi,
                    },
                    "source": s.provenance.as_dict(),
                    "verification": s.verification.as_dict(),
                }
                for s in request.signals
            ],
        })

    for derived in mapping.derived:
        payload["derived"].append({
            "key": derived.key,
            "label": derived.label,
            "unit": derived.unit,
            "operation": derived.operation,
            "inputs": dict(derived.inputs),
            "fallback": dict(derived.fallback),
            "trigger": list(derived.trigger),
            "source": derived.provenance.as_dict(),
            "verification": derived.verification.as_dict(),
        })

    print(json.dumps(payload, indent=2, ensure_ascii=False))

    return 0


def cmd_decode(args) -> int:
    mapping = load_file(args.path)
    response = _parse_bytes(args.response)
    registry = MappingRegistry([mapping])
    signal = registry.find_signal(args.signal)

    if signal is None:
        derived = next(
            (d for d in mapping.derived if d.key == args.signal), None
        )

        if derived is None:
            print(f"no signal named {args.signal!r}", file=sys.stderr)
            return 1

        print(
            f"{args.signal!r} is a derived signal; decode a request "
            "response instead, or use `python3 -m bmwdiag.mapping request`",
            file=sys.stderr,
        )
        return 1

    request = registry.find_request(signal.request_id)
    value = decode_signal(signal, request, response)
    payload = match_prefix(request, response)

    print(f"request  {request.id}")
    print(f"response {response.hex(' ')}")
    print(f"payload  {payload.hex(' ')}")
    print(f"{signal.key} = {value}{(' ' + signal.unit) if signal.unit else ''}")

    if args.all:
        print()

        for key, other in decode_response(request, response).items():
            print(f"  {key} = {other}")

    return 0


def cmd_request(args) -> int:
    from ..protocol.request import build_request

    mapping = load_file(args.path)
    registry = MappingRegistry([mapping])
    request = registry.find_request(args.request)

    if request is None:
        print(f"no request named {args.request!r}", file=sys.stderr)
        return 1

    targets = {}

    if args.target is not None:
        targets = {
            name: args.target for name in (request.target.name or "",) if name
        }

    bound = build_request(request, targets)

    for i, frame in enumerate(request.setup):
        print(f"setup[{i}]      {bytes(frame).hex(' ')}")

    print(bound.describe())
    print(f"expect prefix {bound.expect_prefix.hex(' ') or '(none)'}")
    print(f"min length    {bound.min_length}")

    return 0


def cmd_plan(args) -> int:
    registry = MappingRegistry(_load(args.paths))
    profile = registry.resolve(AllCapabilities(), config={"tank": args.tank})
    classes = resolve_classes(
        registry.polling_classes(),
        {"slow": PollingClassDef("slow", "cycles", float(args.slow_every), 1)},
    )
    plan = PollingPlan(profile.requests, classes)

    print("polling classes:")

    for name, cls in sorted(classes.items(), key=lambda kv: kv[1].priority):
        print(f"  {name:<12} {cls.kind}={cls.value:g}  priority {cls.priority}")

    print("\nrequests per class:", plan.counts())
    print(f"\n{len(profile.requests)} requests carry "
          f"{len(profile.signals)} signals "
          f"({len(profile.signals) - len(profile.requests)} extra signals "
          "cost no extra traffic)")

    for cycle in range(0, args.cycles):
        due = plan.due(cycle)
        print(f"  cycle {cycle:>3}: {len(due):>3} requests")

    return 0


def cmd_lock(args) -> int:
    from .versioning import build_lock, render_lock, load_lock, diff_lock, LOCK_NAME
    import os

    document = build_lock(args.paths)
    lock_path = args.out or os.path.join(args.paths[0], LOCK_NAME)

    if args.check:
        if not os.path.exists(lock_path):
            print(f"error: lockfile {lock_path} does not exist; "
                  "run without --check to create it", file=sys.stderr)
            return 1

        problems = diff_lock(document, load_lock(lock_path))

        if problems:
            print(f"mapping versions are OUT OF SYNC with {lock_path}:",
                  file=sys.stderr)
            for line in problems:
                print(f"  - {line}", file=sys.stderr)
            print("\nregenerate with: python3 -m bmwdiag.mapping lock "
                  f"{args.paths[0]}", file=sys.stderr)
            return 1

        print(f"ok  {len(document['mappings'])} mapping(s) match {lock_path}")
        return 0

    with open(lock_path, "w", encoding="utf-8") as handle:
        handle.write(render_lock(document))

    print(f"wrote {lock_path} ({len(document['mappings'])} mappings)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m bmwdiag.mapping",
        description="Inspect, validate and exercise diagnostic mappings "
                    "without a vehicle.",
    )
    sub = parser.add_subparsers(dest="command")

    validate = sub.add_parser("validate", help="load and validate mappings")
    validate.add_argument("paths", nargs="+")
    validate.set_defaults(func=cmd_validate)

    listing = sub.add_parser("list", help="summarise mappings")
    listing.add_argument("paths", nargs="+")
    listing.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="dump one mapping as JSON")
    show.add_argument("path")
    show.set_defaults(func=cmd_show)

    decode = sub.add_parser("decode", help="decode a signal from a raw response")
    decode.add_argument("path")
    decode.add_argument("signal")
    decode.add_argument("response", help='hex bytes, e.g. "41 0C 0C 3C"')
    decode.add_argument("--all", action="store_true",
                        help="also show every other signal in that response")
    decode.set_defaults(func=cmd_decode)

    request = sub.add_parser("request", help="show the bytes a request sends")
    request.add_argument("path")
    request.add_argument("request")
    request.add_argument("--target", type=lambda s: int(s, 0), default=None,
                         help="resolve a dynamic target to this address")
    request.set_defaults(func=cmd_request)

    plan = sub.add_parser("plan", help="show the polling plan")
    plan.add_argument("paths", nargs="+")
    plan.add_argument("--slow-every", type=int, default=10)
    plan.add_argument("--tank", type=float, default=70.0)
    plan.add_argument("--cycles", type=int, default=4)
    plan.set_defaults(func=cmd_plan)

    lock = sub.add_parser("lock",
                          help="write/check the mapping version lockfile")
    lock.add_argument("paths", nargs="+")
    lock.add_argument("--check", action="store_true",
                      help="verify the lock matches disk instead of writing it")
    lock.add_argument("--out", default=None,
                      help="lockfile path (default: <first path>/VERSIONS.lock)")
    lock.set_defaults(func=cmd_lock)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "func", None):
        parser.print_help()
        return 2

    try:
        return args.func(args)
    except MappingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
