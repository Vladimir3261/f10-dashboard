# Health models — the first longitudinal baselines

`analysis/health/` turns recorded drives into a small number of
**interpretable** health metrics, each carrying the evidence it rests on
(sample counts, coverage, the filters applied, the alignment tolerance,
the operating-condition window, the mapping versions, a confidence
grade) and an explicit `unavailable_reason` whenever a number cannot
honestly be produced. It runs on the recorder's SQLite file and on lake
rows through one row-iterator seam, is pure stdlib, and is deterministic.

```
python3 -m analysis.health --db local/telemetry.db
python3 -m analysis.health --db local/telemetry.db --json report.json
python3 -m analysis.health --lake-sessions sessions.jsonl --lake-samples samples.jsonl
```

**Nothing in this document is a statement about the car.** Every number
quoted as an example comes from the seeded synthetic drives in
`tests/test_health_models.py` unless it is explicitly marked *lake
measurement* — a read-only query over the ClickHouse lake's clock-synced
sessions, run 2026-09-05, used only to choose constants and to state
what the current data can and cannot support.

## What is claimed, and what is not

Claimed:

- A metric's `value` is the current population's median under the
  stated conditions, after the stated filters, compared with a baseline
  drawn from the same car under the same definition.
- `drift.detected` is true only when the shift is material against the
  car's own scatter, supported by a rank-sum statistic, and the data
  grade is at least `moderate` (rules below). A missed change is
  preferred to an invented one.
- `unavailable_reason` is the answer whenever the definition's minimums
  are not met. **A missing number is never silently replaced by a
  weaker one.**

Not claimed:

- **No physical interpretation.** A boost residual drifting down says
  the actuator/sensor/model loop tracks differently at like operating
  points; it does not say "wastegate" or "leak". The AI/interpretation
  layer is explicitly not built (`docs/ROADMAP.md`).
- **No cross-vehicle reference.** There is no "normal N47" anywhere in
  this package. The baseline is this car's own earlier drives.
- **Cooling after shutdown is not modelled** — recording stops with the
  ignition, so it is never observed.
- **EGR is unavailable by design.** Only the ECU's own control deviation
  (`n47d_egr_deviation`) and the OBD commanded/error percentages are
  mapped; there is no requested/actual pair, and a model over the ECU's
  own deviation would restate the ECU rather than check it.
- **DPF-related channels are not used** (the filter was removed; see
  `docs/DPF_SOOT.md`).
- The rail model is **built but produces nothing on the data recorded so
  far** — see "What the lake supports today".

## Eligibility (the contract, reused not restated)

| Filter | Rule | Where it comes from |
|---|---|---|
| clock trust | every run of a trip has `clock_synced = 1`; otherwise the trip is excluded and listed | `runs.clock_synced` / `sessions.clock_synced`; `analysis/trips.py` already refuses to group across an undisciplined clock |
| quality | only `quality = 'ok'` samples are measurements; other labels are excluded and counted per label; `NULL` (pre-labelling) is used and counted as `unlabelled` | `docs/DATA_QUALITY.md`, `analysis/session_report.py` |
| trips | the unit of comparison is the physical drive from `analysis.trips.group_trips`, never the acquisition run | `analysis/trips.py` |
| alignment | actual/setpoint pairs are matched with `analysis.alignment.pairing_for` and its declared `max_age_s`; **no tolerance was changed** | `analysis/alignment.py` |
| mapping compatibility | see below | `run_channels.mapping_version` / `samples.mapping_ver` |

Every metric echoes the filter list under `quality_filters` and the
counts under `coverage`.

### Mapping-version compatibility

A channel is either a **value channel** (its number enters the metric:
boost actual and setpoint, rail actual and setpoint, coolant, oil) or a
**context channel** (it only selects or labels samples: RPM, pedal/load,
speed, ambient, charge-air temperature).

- A version change on a value channel across trips is **refused**: the
  trips are split into populations at the change and only the newest
  population is compared. The metric's `compatibility` block lists the
  earlier populations that were cut off and why. A version bump means
  the file's content changed and nothing in the version says whether the
  decode did; pooling would be a guess.
- A trip whose own runs decoded a value channel with two versions is
  **dropped** and listed under `compatibility.trips_dropped`.
- A switch in *which* value channel a trip used (DDE coolant vs OBD
  coolant) is a population break for the same reason: different sensor
  path, different resolution.
- A version change on a context channel is **flagged**: samples are
  kept, the flag is in `notes`, and the confidence grade is capped at
  `moderate` (`no_context_version_flags` is a `high` rule).

## The baseline definition (data, versioned, echoed)

`analysis.health.contract.BaselineDefinition`, `id = f10-health-baseline`,
`version = 1`. Every metric carries the definition it was computed under
in `baseline_definition`, so two reports can be diffed on their rules and
no result can be computed against a baseline nobody can name. The
`version` is bumped when any rule changes meaning.

| Field | Default | Where it comes from |
|---|---|---|
| `reference_trips` / `current_trips` | 5 / 3 | reference = the **first** 5 eligible trips of the population, current = the **last** 3, non-overlapping. Earliest-first because the question is "has it changed since we started looking"; a sliding baseline would follow the drift it is meant to catch |
| `min_samples_per_side` | 30 | per-sample metrics: the rank-sum normal approximation's rule of thumb (both sides above ~20) with margin, and p10/p90 as at least the third order statistic |
| `min_trips_per_side` | 2 | so one unusual drive cannot be a whole population |
| `min_trip_observations_per_side` | 3 | per-trip metrics (warm-up): the smallest count whose median is not an extreme |
| `material_fraction_of_spread` | 0.5 | a shift is material when the current median moves by at least half the baseline's own p10–p90 spread |
| `drift_z` | 2.0 | rank-sum \|z\| at the conventional two-sided ~5 % level |
| `steady_window_s` / `steady_rpm_range` / `steady_pedal_range_pct` | 2 s / 50 rpm / 2 % | *lake measurement*, 623 boost pairs: residual p10–p90 spread is 34 hPa when RPM stays within 50 over ±2 s, 283 hPa in the 50–100 band, 460–760 above; pedal within 2 % gives 41 hPa against 181+. A ±1 s window gave 50 hPa, ±3 s gave 28 hPa with fewer samples; ±2 s chosen. **Provisional and circular**: the constants were chosen to minimise the residual spread on the same 623 pairs the model is then run over, so on today's lake they are tuned to the data rather than validated against it. They should be re-derived on a held-out set of drives once there are enough, and the definition `version` bumped if they move |
| `steady_min_context_samples` | 4 | fewer motion-tier samples in the window and the pair is *unassessed* (neither steady nor transient). Chosen against the 10 Hz motion tier (≈ 40 samples in ±2 s). When the demand channel falls back to OBD `load` (the `control_ctx` tier, 1 s) the window holds only ~5 samples, so the gate is barely met and the range test rests on 4–5 points; a `load`-gated cell is a coarser judgement of "steady" than a `pedal`-gated one, and the observation's `demand` channel name says which applies |
| `rpm_bins` | 600–1000, 1000–1500, 1500–2000, 2000–2500, 2500–3000, 3000–5000 | operating-condition cells; conventional diesel bands |
| `demand_bins_pct` | 0–3, 3–15, 15–40, 40–100 | pedal (preferred, 10 Hz beside RPM), `n47d_pedal`, then `load` |
| `cold_start_max_c` | 40 °C | a trip is a cold start when its first coolant reading is below this: under the thermostat, above any ambient the car sees |
| `warmup_targets_c` | 60 / 80 / 90 | |
| `ambient_bin_c` | 5 °C | warm-up trips are compared within the same ambient band |
| `stabilised_after_s` | 120 s | stabilised coolant = median from this long after the 80 °C crossing, while moving |
| `moving_speed_kmh` | 3 | the session report's `DRIVING_SPEED` |

Overrides (`--reference-trips`, `--current-trips`, or
`BaselineDefinition.replace`) change the definition and the changed
definition is what gets echoed.

## Confidence — what the grade means numerically

`grade_confidence` grades the **strength of the data behind the
comparison**, not the size of the effect (the effect is `drift`). Every
rule it evaluated is in `confidence.rules`, the failing ones in
`confidence.reasons`.

| Grade | Meaning |
|---|---|
| `none` | below the definition's minimum samples or trips on either side, or alignment coverage under the usable threshold. The metric is **unavailable** and says why; a `none` grade never accompanies a value |
| `low` | minimums met, but the two sides were recorded in different drive modes (different sampling configuration) |
| `moderate` | minimums met, alignment coverage ≥ 50 % (`MIN_USEFUL_COVERAGE`, the alignment contract's own threshold), same drive modes |
| `high` | `moderate` plus: ≥ 100 samples from ≥ 3 trips on each side (per-trip metrics: ≥ 5 trips per side), alignment coverage ≥ 80 %, no context-channel version flag |

Coverage rules are vacuous for per-trip warm-up figures (no pairwise
alignment step); the alignment block says so. Note that under the
default definition (current window of 3 trips) a per-trip metric can
never reach `high`: see "What per-trip drift can and cannot say" under
the warm-up model.

## Drift — the rule, stated

```
detected  iff  |delta| >= material_fraction_of_spread × (baseline p90 − p10)
          and  |z| >= drift_z
          and  confidence >= moderate
```

`delta` = current median − baseline median. `z` is the tie-corrected
Mann–Whitney normal approximation, signed so a positive z means the
current sample tends to be larger. `p_exceed` is the common-language
effect size P(current sample > baseline sample); 0.5 means
indistinguishable. All of `material`, `supported`, `detected`,
`direction`, `span_s` and the rule text are in `drift`, so a reader can
see which leg failed.

## The models

### warm-up / cooling

Per cold-start trip: `time_to_60c` / `time_to_80c` / `time_to_90c` (s
from the first coolant sample, resolved to the coolant sampling interval,
which is echoed per observation), `warmup_slope` (least-squares °C/min
to the 80 °C crossing), `oil_lag_to_60c` (oil crossing 60 °C minus
coolant crossing 60 °C), `stabilised_coolant` (median from 120 s after
the 80 °C crossing while speed > 3 km/h, ≥ 5 samples). Context per
observation: ambient median over the ramp, share of the ramp spent
moving, mean OBD load over the ramp.

Conditioning: one metric per ambient band (5 °C). Value channels: the
coolant and oil channels the trip actually used (`n47d_coolant` before
`coolant`, `n47d_oil_temp` before `oil`). Per-trip metric: one
observation per trip; minimum 3 per side; `high` needs 5 per side.
Cooling after shutdown: not observed, not modelled.

**Truncated ramps.** `warmup_slope` exists only for a trip whose coolant
crossed 80 °C; a trip that ended first has `warmup_slope_c_per_min:
null` and `ramp_complete: false`. The slope of a truncated exponential
is steeper than the slope of the whole ramp, so pooling the two would
make "drift" a function of trip length — and short winter trips are
routine on this car. The ramp context (`moving_fraction`,
`load_mean_pct`, `ambient_c`) is still computed for an incomplete trip,
over what there was of the ramp.

**Survivorship.** A crossing metric (`time_to_60c` / `_80c` / `_90c`,
`warmup_slope`, `stabilised_coolant`, `oil_lag_to_60c`) only sees the
trips that reached its target — and a *slower* warm-up is exactly what
makes a trip fail to reach it. The metric therefore **under-detects
slowing**: the trips that would have carried the evidence are the ones
missing from it. So every warm-up metric reports, under `coverage`,
`cold_starts_in_band`, `cold_starts_reached` and `reached_fraction`,
and carries a note whenever the fraction is below 1. A falling
`reached_fraction` over time is itself a warm-up signal, and the honest
one when the metric says "no change".

**What per-trip drift can and cannot say.** With the default 5
reference + 3 current trips the rank-sum |z| has a ceiling of 2.236
(U ∈ [0, 15]), so `supported` (|z| ≥ 2.0) needs complete separation of
the two sides with at most one cross-group tie: every current trip
beyond every baseline trip. That is a deliberately hard bar for eight
observations. And a per-trip metric can **never grade `high`** under the
default definition — `high` needs 5 trips on each side and the current
window holds 3 — so `detected` per trip tops out at `moderate`. To reach
`high`, run with `--current-trips 5` (10 cold starts in the same ambient
band, 5 + 5); the |z| ceiling then rises to 2.61 and `supported` allows
at most two of the 25 cross-group comparisons to go the other way.

### boost tracking

Residual = `n47d_boost_act` − `n47d_boost_set` (hPa) on pairs aligned
within the declared 1.0 s. Each pair is judged **steady** (RPM range ≤ 50
and pedal range ≤ 2 % over ±2 s of motion-tier samples), **transient**,
or **unassessed** (fewer than 4 motion-tier samples in the window).
Steady residuals are compared per `(rpm_bin, demand_bin)` cell as
`residual_steady`, labelled with the charge-air temperature median of
each side (context, 15 s pairing — see below). Transient residuals are
pooled across operating points as `residual_transient`, marked
descriptive: the 0.56 s between the two reads injects up to ~140 hPa
(p90, *lake measurement* in `analysis/alignment.py`) of its own under
transients, so no drift claim rests on it.

### rail-pressure tracking

The same shape on `n47d_rail_act` − `n47d_rail_set` (bar), the same 1.0 s
tolerance, the same gate and cells. The transient note is per model
(`TRACKING[kind]["transient_error"]`): for rail the gap's own error is
**not measured** — the two reads never aligned on the lake's clock-synced
sessions — and the note says so rather than borrowing the boost figure.

### EGR

`status = unavailable`, reason `requested/actual not yet mapped: …`.
Not faked.

## What the lake supports today (*lake measurement*, 2026-09-05)

Read-only over `telemetry.samples` / `telemetry.sessions`, no vehicle
identifier read or printed:

- 119 sessions, 9 with `clock_synced = 1` (all mode `normal`), grouping
  into 4 eligible trips, 2 of them cold starts in two different ambient
  bands. Under the default definition (5 + 3 trips) **every metric is
  unavailable with `insufficient trips`**, and that is the correct
  output: the pipeline is ready before the data is.
- Boost: 623 of 623 actual samples aligned within 1.0 s; 293 steady,
  273 transient, 57 unassessed. Cells exist for 600–1000 / 1000–1500 /
  1500–2000 rpm at 3–15 % and 15–40 % pedal.
- Rail: the two reads sit **1.53–1.98 s apart on every clock-synced
  session** (flow mapping v2 schedule), so 0 of 623 pairs align within
  the 1.0 s tolerance and the model reports "the pair never aligned … a
  schedule fact, not a car fact". The tolerance was not loosened; the
  fix is on the polling side: the flow mapping has declared
  `polling: {pair: rail}` since v3 (commit `21bc171`, authored
  2026-09-01), so the two reads share
  one rotation slot on drives recorded after that; the clock-synced
  sessions in the lake all predate it (flow v2).
- Charge-air temperature moves 0.1 °C median / 0.7 °C p90 across one
  ~12 s round-robin at steady state (610 consecutive reads), which is
  the measurement behind the two new **context-only** pairings
  `(n47d_boost_act, n47d_charge_air_temp)` and
  `(n47d_rail_act, n47d_charge_air_temp)` at 15 s in
  `analysis/alignment.py`. They are new declarations for a value that is
  never subtracted; the control-loop pairings are unchanged.

## The data-source seam

`analysis.health.source.Source` is three methods: `describe()`,
`sessions() -> [SessionMeta]`, `rows(run_id) -> iter[Row(ts, channel,
value, quality, mapping_ver)]`. `SqliteSource` reads a recorder database
read-only (`mode=ro`), taking per-channel provenance from `run_channels`
and falling back to `params.mapping_ver` only when the row is absent.
`RowSource` takes lake-shaped dicts (`session_id`, `ts`, `channel_raw`,
`value`, `quality`, `mapping_ver`) in memory or from two `JSONEachRow`
exports; it prefers `channel_raw` because the ingest merges `coolant`
and `n47d_coolant` into one normalised name and the models must know
which one they are looking at. Neither reader has a field for a VIN.

## The synthetic test drives

`tests/test_health_models.py` generates every drive from a seeded
`random.Random`: an idle minute, then 240 s blocks of steady/ramp/
steady/ramp at invented operating points; a ~12 s round-robin with the
actual read 0.56 s before the setpoint; an exponential warm-up from
ambient + 2 °C. Boost offset −30 hPa with σ = 10 noise, transient error
−400 hPa inside ramps. The eight cases from the issue are exercised on
it: stable (no drift declared), gradual drift (declared, `moderate`/
`high`), sentinels (excluded, counted, cannot move the median), too few
trips / too little alignment coverage / a pair that never aligns
(unavailable with the reason), value-channel version change (refused)
and context-channel change (flagged), a declared `sensor_replacement`
(baseline restarts after it), and mixed transient/steady (the steady
cell does not see the transients). Also: the SQLite and row readers
produce identical reports from the same drives, and the CLI writes the
contract. **All of it is invented data.**

## Left out, on purpose

- MAF as a demand fallback (the definition names pedal/load only).
- Kilometres spanned by a comparison (no odometer channel is mapped;
  `span_s` is wall-clock).
- Any model over EGR, DPF or soot.
- Grafana panels or lake schema for these results.
