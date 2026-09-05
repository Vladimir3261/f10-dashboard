#!/usr/bin/env python3
"""
Health models over a recorder database or a lake export.

    python3 -m analysis.health --db local/telemetry.db
    python3 -m analysis.health --db local/telemetry.db --json report.json
    python3 -m analysis.health --lake-sessions s.jsonl --lake-samples r.jsonl
    python3 -m analysis.health --db ... --reference-trips 3 --current-trips 2
    python3 -m analysis.health --db ... --events local/vehicle-events.yaml

Prints the short human report; `--json` writes the full contract. Read
only: opens the database with `mode=ro`, writes nothing but the file
named by `--json`, and never emits a VIN (the row shape has no field
for one).

Changing `--reference-trips` / `--current-trips` changes the baseline
definition, and the definition actually used is echoed in every metric,
so a result can always be told apart from one built under the default.
"""

import argparse
import json
import sys

from bmwdiag.vehicle import load_events

from analysis.health.contract import DEFAULT_DEFINITION
from analysis.health.report import MODELS, build_report, render_text
from analysis.health.source import RowSource, SqliteSource


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m analysis.health",
        description="longitudinal health models with coverage and confidence",
    )
    src = ap.add_argument_group("source (one of)")
    src.add_argument("--db", help="recorder telemetry.db (opened read-only)")
    src.add_argument("--lake-sessions",
                     help="JSONEachRow export of telemetry.sessions (see source.py)")
    src.add_argument("--lake-samples",
                     help="JSONEachRow export of telemetry.samples (see source.py)")
    ap.add_argument("--events", default=None,
                    help="vehicle events YAML (default: the profile's local file)")
    ap.add_argument("--no-events", action="store_true",
                    help="ignore the vehicle event history")
    ap.add_argument("--reference-trips", type=int,
                    default=DEFAULT_DEFINITION.reference_trips)
    ap.add_argument("--current-trips", type=int,
                    default=DEFAULT_DEFINITION.current_trips)
    ap.add_argument("--models", default=",".join(MODELS),
                    help=f"comma-separated subset of {','.join(MODELS)}")
    ap.add_argument("--json", help="write the full contract here")
    ap.add_argument("--quiet", action="store_true", help="no text report")
    args = ap.parse_args(argv)

    if args.db:
        source = SqliteSource(args.db)
    elif args.lake_sessions and args.lake_samples:
        source = RowSource.from_json_lines(args.lake_sessions, args.lake_samples)
    else:
        ap.error("give --db, or both --lake-sessions and --lake-samples")

    definition = DEFAULT_DEFINITION.replace(
        reference_trips=args.reference_trips, current_trips=args.current_trips,
    )
    events = () if args.no_events else load_events(args.events)
    report = build_report(
        source, definition, events,
        models=[m.strip() for m in args.models.split(",") if m.strip()],
    )

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(report.as_dict(), fh, indent=2, sort_keys=True)
            fh.write("\n")

    if not args.quiet:
        sys.stdout.write(render_text(report))

    return 0


if __name__ == "__main__":
    sys.exit(main())
