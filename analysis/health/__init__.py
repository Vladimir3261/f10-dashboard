"""
Longitudinal health models for one car, with coverage and confidence.

The dashboard shows what the engine is doing now. This package asks the
question the project exists for: has the behaviour of THIS vehicle
changed, at comparable operating conditions, since a baseline - and how
strong is the evidence?

Every number leaves here inside a `HealthMetric` (see `contract.py`),
which carries the value together with everything a reader needs to
decide whether to believe it: the baseline distribution it is compared
against, the sample and trip counts on each side, how much of the
recorded data survived the eligibility filters, the alignment tolerance
and operating-condition window in force, the mapping versions the
channels were decoded with, a confidence grade with the rules that
produced it, and - when the answer is "cannot be computed" - the reason,
in the same field a value would have occupied.

Three models are implemented (`models.py`):

  * warm-up / cooling  - time to 60/80/90 °C, warm-up slope, oil-vs-
                         coolant lag, stabilised coolant; per ambient
                         band, with idle-vs-moving and load context;
  * boost tracking     - actual minus setpoint at STEADY operating
                         points, per RPM x pedal cell;
  * rail tracking      - the same shape for rail pressure.

EGR is declared unavailable: the car exposes an EGR control deviation
but no requested/actual pair, and a model built on a deviation the ECU
already computed would be restating the ECU, not checking it.

Everything is deterministic and stdlib-only. The same code runs on a
recorder database and on lake rows through the seam in `source.py`, and
it never writes anything, never sees a VIN, and never assumes another
vehicle's behaviour. See docs/HEALTH_MODELS.md.
"""

from analysis.health.contract import (  # noqa: F401
    BaselineDefinition,
    DEFAULT_DEFINITION,
    HealthMetric,
    HealthReport,
    ModelResult,
)
from analysis.health.report import build_report, render_text  # noqa: F401
from analysis.health.source import RowSource, SqliteSource  # noqa: F401

__all__ = [
    "BaselineDefinition",
    "DEFAULT_DEFINITION",
    "HealthMetric",
    "HealthReport",
    "ModelResult",
    "RowSource",
    "SqliteSource",
    "build_report",
    "render_text",
]
