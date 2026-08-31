# The time-alignment contract

When two observations may be compared, and what it costs to insist.

## The problem

Every cross-channel metric here subtracts two values that were never
sampled at the same instant. Channels are polled on different schedules,
and the proprietary DDE reads share one **staggered** round-robin — a
member refreshes only about every 11 s. Nothing in the stored data says
how far apart two values were when something subtracted one from the
other.

Unbounded nearest-value matching therefore produces a number for every
pair, however stale, and that number is indistinguishable from a
measurement. Boost actual minus a setpoint read twelve seconds earlier is
not control error; it is mostly the engine having changed in between.

**A plausible graph built from mismatched observations is worse than no
graph**, because nobody re-checks a plausible graph.

## The rules

Every comparison, local or in the lake, must satisfy all four:

| rule | why |
|---|---|
| **same session** | An ASOF join keyed only on `vehicle_id` silently reaches into the *previous drive* for its nearest value — across an ignition cycle, hours of parking, and a different mapping/mode configuration. |
| **bounded age** | Per pair, not one global number. A control loop needs sub-second; a coolant cross-check tolerates 15 s. |
| **`clock_synced = 1`** | The Pi has no RTC and once corrected itself 76.5 min mid-recording. A timestamp difference from an undisciplined run means nothing. `NULL` is "recorded before the flag" — unknown, so excluded, never assumed good. |
| **`quality = 'ok'`** | A sentinel or a railed sensor is not a measurement. See [`DATA_QUALITY.md`](DATA_QUALITY.md). |

Session-scoping also delivers **mapping and mode compatibility for
free**: one run has exactly one sampling and mapping configuration by
construction (a mode change ends the run, and `run_channels` pins the
mapping version per run). A comparison that cannot cross a session
cannot silently mix two configurations either.

## The windows

Declared once, in `analysis/alignment.py`, and mirrored in the SQL:

| pair | window | reasoning |
|---|---|---|
| boost actual / setpoint | 0.5 s | the turbo moves in hundreds of ms under load |
| rail actual / setpoint | 0.5 s | the fastest loop on the engine |
| DDE / OBD coolant | 15 s | two reads of one slow sensor; a wide window costs nothing |
| DDE ambient / OBD baro | 60 s | effectively constant over a minute |
| oil / coolant (warm-up) | 15 s | the quantity of interest is minutes wide |
| soot measured / modelled | 15 s | two slow ECU model outputs |
| *anything undeclared* | 1.0 s | strict on purpose — an undeclared comparison should look visibly poor rather than inherit a convenient window |

## Coverage is part of the result

Every comparison reports how much of its input actually satisfied the
window. Below **50%** the report states that the comparison cannot be
concluded rather than printing an average — a metric covering a fraction
of its own inputs is describing the poll schedule, not the car.

## What this cost, measured

On the lake as of 2026-08-31:

| pair | median gap | inside its window |
|---|---|---|
| boost actual / setpoint | **12.33 s** | **4.7 %** |
| rail actual / setpoint | 6.63 s | 4.3 % |
| DDE / OBD coolant | 4.48 s | 100 % |

And only **9 of 119 sessions** carry `clock_synced = 1`, so most
historical data is legitimately excluded from time-derived work.

Two consequences worth stating plainly:

1. **Boost actual-vs-setpoint — the proposed Stage-3 flagship — does not
   currently have the data to exist.** It was previously reported from
   pairs a median of twelve seconds apart. Enforcing the window does not
   break the metric; it reveals that the metric was never supported. The
   query and the Grafana panel will be sparse or empty, and that is the
   correct output.
2. The DDE/OBD coolant cross-check is unaffected — 100% coverage — which
   is what a genuinely comparable pair looks like.

## Acquisition follow-up: co-schedule the control pairs

The alignment layer cannot fix this from the analysis side. Post-hoc
matching can only reject what was never comparable; the pairs have to be
*acquired* together.

The staggered `dde_dyn` class fires one member per cycle, so an actual
and its setpoint land ~12 s apart by design. The fix is a **paired
request policy**: schedule actual and setpoint adjacently within the
stagger so they are read in consecutive exchanges (~100 ms apart) rather
than a full round-robin apart.

Candidates, in value order:

- `n47d_boost_act` / `n47d_boost_set` — the flagship; without this the
  metric stays unusable
- `n47d_rail_act` / `n47d_rail_set` — same shape, currently 4.3%
- EGR actual / request, if a request channel is ever mapped

**Not implemented here.** It changes what the car is asked and when,
which belongs with the polling work and wants its own on-car validation
run — not a change to make inside an analysis PR. Recorded so the
Stage-3 flagship is not chosen again without it.

## Where it lives

- `analysis/alignment.py` — the windows, the matcher, coverage
- `analysis/session_report.py` — local reports; refuses unusable
  conclusions and declares an untrusted clock at the top
- `analysis/clickhouse/insights.sql` — section 7 reports what the
  contract rejected
- `infra/grafana/dashboards/f10-health.json` — same rules on the panels
- `tests/test_alignment.py` — including guards that keep the committed
  SQL from dropping the session key or the window again
