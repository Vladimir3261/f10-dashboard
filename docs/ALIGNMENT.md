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

## Symmetric nearest, on both sides

"Nearest" means nearest in *either direction*. ClickHouse's `ASOF JOIN`
is one-directional — `a.ts >= b.ts` finds only the most recent **earlier**
row — so a sample 100 ms after `a` is ignored in favour of one 10 s
before it. Every comparison therefore joins both ways and picks the
smaller gap:

```sql
ASOF LEFT JOIN (...) prev ON a.session_id=prev.session_id AND a.ts>=prev.ts
ASOF LEFT JOIN (...) next ON a.session_id=next.session_id AND a.ts<=next.ts
...
if(prev_gap <= next_gap, prev.value, next.value)   -- ties go to prev
```

An unmatched `LEFT` side yields the type default (1970), which makes
`prev_gap` enormous (harmless — it loses and fails the window) but makes
`next_gap` **negative**, and a negative gap would win `least()`. Hence
the `>= 0` guard. This is load-bearing, not decoration.

This matters because it is the difference between the two
implementations agreeing and disagreeing about the same data. Measured
against the live lake: `a@10.0` with `b@9.6` and `b@10.2` selects `10.2`
two-sided, and `9.6` backward-only.

## The windows

Declared once, in `analysis/alignment.py`, and mirrored in the SQL:

| pair | window | reasoning |
|---|---|---|
| boost actual / setpoint | 1.0 s | measured 0.56 s apart; see below |
| rail actual / setpoint | 1.0 s | same shape |
| DDE / OBD coolant | 15 s | two reads of one slow sensor |
| DDE ambient / OBD baro | 60 s | effectively constant over a minute |
| oil / coolant (warm-up) | 15 s | the quantity of interest is minutes wide |
| soot measured / modelled | 15 s | two slow ECU model outputs |
| *anything undeclared* | 1.0 s | strict on purpose |

## Coverage is part of the result

Every comparison reports how much of its input satisfied the window.
Below **50%** the report states that the comparison cannot be concluded
rather than printing an average — a metric covering a fraction of its own
inputs is describing the poll schedule, not the car.

## What the data actually looks like

Measured on the lake, 2026-08-31, with symmetric nearest:

| pair | median gap | p10–p90 | inside window |
|---|---|---|---|
| boost actual / setpoint | **0.56 s** | 0.52–0.59 | 98.9 % (1.0 s) |
| rail actual / setpoint | 4.32 s | — | (1.0 s) |
| DDE / OBD coolant | 2.75 s | — | 100 % (15 s) |

**A correction worth recording.** An earlier draft of this work reported
the boost pair as 12.33 s apart with 4.7% coverage, and concluded the
Stage-3 flagship metric "did not have the data to exist". That was an
artifact of measuring with a backward-only ASOF, which can only reach
back to the previous round-robin visit. Looking both ways, the setpoint
is 0.56 s from the actual, consistently — p10 to p90 spans 70 ms. The
pair is well aligned; the earlier number described the query, not the
car.

The window is 1.0 s for the same reason. 0.5 s — the number a control
loop suggests in the abstract — sits just *below* the real 0.56 s
separation and keeps 4.6% of pairs. That measures the threshold.

### What the residual 0.56 s costs

Not nothing. Using 10 Hz OBD MAP as a proxy for how fast manifold
pressure really moves, the change over a 0.56 s interval is:

| median | p90 | p99 |
|---|---|---|
| 0 hPa | 140 hPa | 672 hPa |

So the metric is sound at steady state and in aggregate, and increasingly
noisy under hard transients, where misalignment alone can inject more
error than the deviation being measured. **A single large excursion may
be sampling rather than the actuator**; a rising trend across many
sessions at comparable load is still meaningful.

## Clock trust: the analysis fails closed

`clock_synced = 1` is required, and a run without it does not get a
time-derived number *with a warning* — it does not get one at all.
`warmup()`, `crosschecks()`, setpoint tracking, the soot alignment and
per-channel sample gaps are all skipped and reported as not evaluated.

Warning the reader is not enough: a plausible number with a caveat
attached is exactly what gets quoted later without the caveat.

Value ranges, sample counts and quality breakdowns are unaffected — they
do not depend on the timestamps being right — so an untrusted run still
produces a useful descriptive report.

Only **9 of 119 sessions** currently carry `clock_synced = 1`, so most
historical data is legitimately outside time-derived work.

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
