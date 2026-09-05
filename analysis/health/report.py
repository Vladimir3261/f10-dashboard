"""
Assemble every model into one report, and render it for a person.

`build_report` is the only entry point the CLI and the tests need: a
source, an optional baseline definition, an optional event history, and
back comes a `HealthReport` whose `as_dict()` is the JSON contract.
`render_text` is the short human version: one sentence per metric, in
the shape the issue asked for, with the unavailable ones saying why.
"""

from typing import Any, Dict, List, Optional, Sequence

from bmwdiag.vehicle import VehicleEvent

from analysis.health.contract import (
    BaselineDefinition, DEFAULT_DEFINITION, HealthReport, ModelResult,
)
from analysis.health.eligibility import load_eligible
from analysis.health.models import egr_model, tracking_model, warmup_model
from analysis.health.source import Source

__all__ = ["build_report", "render_text", "MODELS"]

#: Order of appearance. Names are the model ids in the output.
MODELS = ("warmup", "boost", "rail", "egr")


def build_report(source: Source,
                 definition: Optional[BaselineDefinition] = None,
                 events: Sequence[VehicleEvent] = (),
                 models: Sequence[str] = MODELS) -> HealthReport:
    definition = definition or DEFAULT_DEFINITION
    eligibility = load_eligible(source)
    results: List[ModelResult] = []

    for name in models:
        if name == "warmup":
            results.append(warmup_model(eligibility.trips, definition, events))
        elif name in ("boost", "rail"):
            results.append(tracking_model(name, eligibility.trips, definition, events))
        elif name == "egr":
            results.append(egr_model(eligibility.trips, definition, events))
        else:
            raise ValueError(f"unknown model {name!r}; known: {', '.join(MODELS)}")

    summary: Dict[str, Any] = eligibility.as_dict()
    summary["vehicle_events_declared"] = [e.describe() for e in events]

    return HealthReport(
        source=source.describe(),
        vehicle_label=eligibility.vehicle_label,
        definition=definition.as_dict(),
        eligibility=summary,
        models=results,
    )


def render_text(report: HealthReport) -> str:
    """The short human report: what was concluded, and what could not be."""
    e = report.eligibility
    lines = [
        f"# Health models - {report.vehicle_label or 'unlabelled vehicle'}",
        f"source: {report.source}",
        f"baseline definition: {report.definition['id']} v{report.definition['version']} "
        f"(reference = first {report.definition['reference_trips']} trips, "
        f"current = last {report.definition['current_trips']})",
        f"eligible: {e['trips_eligible']} of {e['trips_total']} trips "
        f"({e['sessions_clock_synced']} of {e['sessions_total']} sessions clock-synced); "
        f"{e['samples_seen']} samples seen, "
        f"{sum(e['samples_excluded_by_quality'].values())} excluded by quality, "
        f"{e['samples_unlabelled']} unlabelled",
    ]

    for excluded in e["excluded_trips"]:
        lines.append(f"  - excluded trip {excluded['trip_uid']}: {excluded['reason']}")

    if e.get("vehicle_events_declared"):
        lines.append("declared vehicle events: " + "; ".join(e["vehicle_events_declared"]))

    for model in report.models:
        lines.append("")
        lines.append(f"## {model.model}")

        if model.status != "computed":
            lines.append(f"unavailable - {model.unavailable_reason}")

            for note in model.notes:
                lines.append(f"  note: {note}")

            continue

        for note in model.notes:
            lines.append(f"  note: {note}")

        available = [m for m in model.metrics if m.available]
        missing = [m for m in model.metrics if not m.available]

        for metric in available:
            lines.append("- " + metric.sentence())

        if missing:
            reasons: Dict[str, List[str]] = {}

            for metric in missing:
                where = ", ".join(f"{k}={v}" for k, v in metric.condition.items()
                                  if k != "window")
                reasons.setdefault(metric.unavailable_reason or "", []).append(
                    f"{metric.metric} [{where}]"
                )

            for reason, names in reasons.items():
                lines.append(f"- unavailable ({len(names)}): {reason}")
                lines.append("    " + "; ".join(names))

    return "\n".join(lines) + "\n"
