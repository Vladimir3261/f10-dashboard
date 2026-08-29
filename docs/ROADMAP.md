# Roadmap & critical assessment

A candid review of where the project stands and where the value is, for
one BMW F10 520d (N47). Written to be argued with, not admired. See
`CLAUDE.md` for purpose/architecture and `research/reports/
n47-next-session.md` for the immediate on-car to-do.

## 1. Value of the overall idea — honest verdict

**Strong foundation, unproven payoff.** The acquisition stack and the
mapping/validation discipline are genuinely good — better than most
personal projects. But every bit of the *value thesis* ("understand this
car's condition over time") rests on an analytics layer that **does not
exist yet**. Today the project is a very well-engineered data recorder.

What is genuinely valuable:

- **The rigor is the asset.** Provenance labels, read-only gating,
  capability-by-probe, no-invented-data, float-exact decoding, on-car
  cross-checks against standard OBD — this is what makes the data
  trustworthy enough to trend over months. Cheap loggers produce data
  you can't trust to compare against itself; this one doesn't.
- **The N47 is a good target for this.** It has well-known wear/failure
  modes that *do* show up in readable signals: DPF restriction, EGR
  fouling, swirl-flap/intake issues, thermostat/cooling drift, glow/
  charging health, injector correction drift. Early "this is creeping"
  warnings here can save real money and catch limp-mode before it
  strands you.

Where the ceiling is (be realistic):

- **Single-car statistics are noisy and slow to accumulate.** Baselines
  need many *comparable* operating points. Irregular driving = thin
  baselines for a long time. Value compounds slowly; there is no
  population to borrow strength from.
- **Some failures don't telegraph.** Timing-chain stretch (the N47's
  signature failure) has weak/late diagnostic signature in the channels
  you can read. The system will be good at "X changed vs its own past"
  and weaker at "X predicts failure Y."
- **The best analysis needs data you don't yet capture well** — trip
  segmentation, ambient/load context per sample, regen events. Until the
  data model carries that, analytics stays shallow.

**Net:** worth continuing *if and only if* the next effort goes into (a)
the data model + quality that make trends valid, and (b) actually
building the first analytics — not into more acquisition polish, and
definitely not into embedded/AI/cloud yet. If it stalls at "nice live
dashboard," it is over-built for what it delivers.

## 2. Critical review of the current state

### Architectural weaknesses

1. **The data model is too thin for the stated goal (top issue).**
   `samples(run_id, ts, param_id, value)` is a flat EAV time-series with
   no operating context. The analytics goal is "coolant at comparable
   ambient/load/speed/trip-phase," but ambient/load/speed are just other
   rows in the same table at *different timestamps* (channels are polled
   at different rates and carry their own ts). Reconstructing an
   operating point means a wide, lossy, expensive time-join. There is no
   trip concept, no materialized operating-point, no per-sample context.
   **Everything in the analytics vision is blocked on this.**
2. **No data-quality flags in storage.** A saturated MAP (255 kPa), a
   sentinel temperature (−40 °C / lambda 2.0), a stale carried-forward
   value, and a real reading are indistinguishable once stored — invalid
   values are simply *dropped* by the decoder, conflating "not polled"
   with "sensor unavailable." Analytics needs to *know* a point was
   saturated, not silently lose it.
3. **No mapping/software version per run.** `runs` has no
   `mapping_version` / `git_sha`. If a scale is corrected later you can't
   tell which historical rows used the old scale — a silent break in any
   long trend that crosses the change.
4. **"Run" ≠ "trip."** A run is one HSFZ connection; ignition-off splits
   it (see the reconnect finding). Trips — the natural unit for
   warm-up/heat-soak/comparison — don't exist.
5. **F303 polling cost and fast/slow coupling.** Each proprietary channel
   costs 3 frames (~90 ms); 13 of them on one slow cycle ≈ 1.2 s that
   *blocks the fast OBD channels* in the single-threaded loop. The
   re-arm-per-switch is correct but there's no medium class, no
   staggering, no separation of the heavy reads from the fast ones. This
   will bite as channels are added.
6. **Collector self-observability is minimal.** `on_error` exists on the
   executor but `live.py` doesn't wire it; NRCs and decode failures are
   not logged to the DB; there's no per-channel success rate. You can't
   later ask "when did channel X start failing."
7. **telemetry.db is a single gitignored file with no backup/export
   cadence.** One corruption loses the entire history the whole project
   is about.

### Over-engineered (relative to one car)

- **The research/conflict/independence apparatus.** Six verification
  states, tiered evidence, ancestry-aware cross-source confirmation,
  conflict graphs — elegant, but heavy for "import a few tables, validate
  on my car." It's the most code serving the least *ongoing daily*
  value. Don't expand it further; it's done.
- **The importer breadth.** Five importers, ~1600 D73 records almost all
  partial and non-executable. Good as a candidate mine, but the
  machinery:shipped-channels ratio is high. Mine the existing records;
  don't build more importers speculatively.

### Under-engineered (all high-value, all missing)

- Analytics (nonexistent).
- Data-quality tagging (nonexistent).
- Trip segmentation + operating context (nonexistent).
- Collector observability into the DB (nonexistent).
- Backup/export (nonexistent).

### Missing telemetry domains (high value, not yet built)

Even within the DDE, the d72 table has channels not yet built that map
directly onto N47 failure modes: **DPF differential pressure** (IPDIP),
**exhaust temps before/after DPF**, **regeneration state / request /
distance / count**, **EGR actual+requested**, **injector correction/
quantities**, **swirl/intake actuator positions**. Beyond the DDE: **EGS**
(gearbox — egs.py exists, no validated mappings), **fuel/body tank
sensors** (`local/captures/kombi_dids.json` captured, unmined),
**electrical/IBS** beyond OBD voltage.

### Missing tests / observability

- No end-to-end test that the recorder persists proprietary channels
  (the `pid=NULL` path) under `--extra-mappings`.
- No poll-loop-level test of the variant probe (only unit-level).
- No data-quality tests (no layer to test).
- No test/measurement of F303 polling cost vs the fast-channel Hz.

## 2.5 Where we actually are (2026-08-29)

The infrastructure detour is finished and is no longer a roadmap item: the
server is Terraform + Ansible, the lake is deployed and accumulating, the Pi
provisions from one script, and telemetry has been flowing unattended.
**~1.56M samples across 5 days and 7 drives** is now enough to do real
analysis on.

Stage status against the plan below:

| stage | state |
|---|---|
| 0 — data model | **mostly done.** Mapping versions are stamped end to end (`params.mapping_ver`, `run_mappings`, `samples.mapping_ver`). Trip segmentation still weak: many `sessions` rows never get `ended` |
| 1 — data quality | **not started.** The `quality` column exists and defaults to `ok`; nothing ever writes it. The OBD MAP saturation was found by hand, which is exactly the case it should have flagged |
| 2 — DDE telemetry | **done enough.** ~23 verified proprietary channels incl. DPF/EGR |
| 3 — analytics | **unblocked, not started.** This is the next real work |
| 5 — drift/anomaly | the goal; needs 1 and 3 |

### Two findings that reorder the priorities

**1. The soot decode is falsified — fix before building DPF analytics.**
Drive 7 straddled a completed regeneration (`n47d_regen_count` 92 → 93,
`n47d_dist_since_regen` reset to ~9 km — two independent counters agree, so
the event is solid). A regeneration burns soot out, so `n47d_soot_meas`
should have *dropped*. It rose ~0.5 g and kept rising.

This is the first real test of that candidate scale and it did not pass.
`n47d_soot_meas` / `n47d_soot_model` may be cumulative-since-new, an ash
estimate, or carry an offset the scale does not model. **Every DPF health
conclusion depends on this channel**, so it is the highest-value open
question in the project — and there is now a captured natural experiment to
test hypotheses against.

**2. Drives fragment into runs, which corrupts longitudinal analysis.**
Drive 7 recorded as 4 runs, not 1. Cause is error handling, not the cable: in
`bmwdiag/mapping/execute.py` the transport call sits outside the try/except
that guards decoding, so one `TimeoutError` (or an `HsfzNack` from the EGS)
tears down the whole HSFZ link instead of failing that one request. Cost is
1.35% of wall time but 100% of analytical continuity — no drive can be
summarised in one report without stitching. Cheap to fix, and it should be
fixed *before* collecting much more data.

### Recommended order

1. **Resolve the soot channel** (research + the captured regen event).
   Nothing about DPF health is trustworthy until this is settled.
2. **Fix the per-request teardown** in `execute.py` — small, and every later
   analysis is cleaner for it. Close `sessions.ended` while there.
3. **Stage 1 data quality** — populate `quality` (saturated / sentinel /
   stale). The MAP-saturation case proves the need and gives a test.
4. **Stage 3 analytics** — condition-normalized baselines, starting with
   DPF ΔP vs exhaust flow, now that 1–3 make the inputs trustworthy.

## 3. The roadmap (staged, testable against the real car)

Ordering principle: unblock valid trends first, then add the highest-
diagnostic-value signals, then build analytics, then widen to other ECUs.
Each stage is small enough to validate on the car before the next.

### Stage 0 — Data-model foundation *(do first; everything depends on it)*

- **Objective:** make longitudinal, condition-normalized analysis
  *possible* to query, and keep historical data interpretable forever.
- **Work:** add to `runs`: `mapping_version`/`git_sha`, tool version,
  and a per-run `variant`. Introduce a **trip** concept (segment by
  ignition/RPM>0 with a gap threshold; a run can hold several trips).
  Add a lightweight **operating-context** per sample or per short time
  bucket (rpm, speed, load, coolant, ambient carried onto each stored
  point, or a periodic "context row" other channels reference). Keep
  SQLite; add columns/tables, don't switch stores.
- **Dependencies:** none.
- **Output:** every historical point is attributable to a trip, an
  operating context, and the mapping version that produced it.
- **Validation:** replay an existing run; confirm trips segment sensibly
  and context reconstructs without a wide time-join.
- **Risks:** schema migration of existing telemetry.db; context sampling
  cadence vs storage size.
- **NOT yet:** external DB, columnar store, remote sync.

### Stage 1 — Data-quality layer

- **Objective:** never confuse unavailable/saturated/stale with real.
- **Work:** carry a per-sample quality flag (ok / saturated / sentinel /
  stale / clipped / decode_fail). Stop silently dropping invalid values —
  record them flagged. Log NRCs and decode failures to `events`. Wire the
  executor `on_error` hook into the recorder.
- **Dependencies:** Stage 0 (storage shape).
- **Output:** analytics can exclude or specially-handle bad points;
  channel health becomes queryable.
- **Validation:** force a saturated MAP and a sentinel temp on the car
  (or in demo), confirm they're stored flagged, not dropped.
- **Risks:** modest storage overhead; defining sentinels per channel.
- **NOT yet:** automatic anomaly flags (that's Stage 6).

### Stage 2 — Expand high-value DDE telemetry

- **Objective:** capture the signals that actually track N47 health.
- **Work:** build d72 candidates (same F303 pattern, sourced scales) for
  **DPF ΔP, exhaust temps pre/post DPF, regen state/request/distance/
  count, EGR actual+requested, injector corrections**. Add a **medium**
  polling class and **stagger** the F303 reads so they don't starve fast
  OBD. Validate each on the car via `validate_candidate.py` /
  `sweep`, promote to verified.
- **Dependencies:** Stages 0–1 (so new channels record with context +
  quality); the polling-cost fix.
- **Output:** the DPF/EGR/emissions picture the health goal needs.
- **Validation:** cross-check against OBD where possible (cat temp PID),
  physical plausibility, measured-vs-modelled agreement, regen distance
  monotonicity.
- **Risks:** F303 contention if staggering isn't done; some IDs may NRC.
- **NOT yet:** EGS/fuel (Stage 4); actuator *control* (never).

### Stage 3 — First analytics (descriptive + first baselines)

- **Objective:** turn stored data into the first real insight.
- **Work:** a `analysis/` package (pandas/polars, offline, reads the DB
  read-only). Start with what current data already supports: **warm-up
  curves** (coolant/oil vs time-since-start), **actual-vs-requested
  deviation** (boost, rail), **DPF measured-vs-modelled soot agreement**,
  per-trip summaries, trip-over-trip comparison. Then the first
  **condition-normalized baseline** (e.g. coolant vs ambient×load bucket)
  once Stage 0 context exists.
- **Dependencies:** Stages 0–2.
- **Output:** "this trip vs your last N comparable trips" reports; the
  first baseline a later drift check can watch.
- **Validation:** sanity against known-good trips; the numbers must match
  hand-computed spot checks.
- **Risks:** thin data early → wide confidence intervals; resist
  over-reading small samples.
- **NOT yet:** ML, change-point detection, AI narration.

### Stage 4 — EGS + fuel/body telemetry

- **Objective:** widen beyond the engine where it's useful and read-only.
- **Work:** validate EGS (`0x18`) channels — gear, turbine/output speed,
  gearbox oil temp, converter slip — using egs.py findings + OBDb
  DA-block claims + the D73 DDE-received values (cross-check the two
  paths). Mine `kombi_dids.json` for tank sensors; validate against a
  fill-up.
- **Dependencies:** Stage 0–2 patterns; egs.py.
- **Output:** drivetrain + fuel domains added to the same pipeline.
- **Validation:** gear vs actual selected gear; oil temp plausibility;
  tank sensors vs a known fill.
- **Risks:** EGS may need a different request pattern; variant matching
  for a second ECU.
- **NOT yet:** anything write/adaptation on the EGS.

### Stage 5 — Drift / anomaly / change-point detection

- **Objective:** the actual payoff — "this is changing."
- **Work:** on top of baselines, add rolling drift detection, simple
  change-point detection, residual-vs-baseline anomaly scoring, and
  multi-signal relationship monitoring (e.g. ΔP vs exhaust-flow slope
  over months). Deterministic and explainable.
- **Dependencies:** Stages 3–4 and *months of accumulated comparable
  trips* — this is gated on data quantity, not just code.
- **Output:** the DPF-restriction / EGR-degradation / cooling-drift
  early warnings that justify the whole project.
- **Validation:** inject synthetic drift into historical data; confirm
  detection; confirm low false-positive rate on stable periods.
- **Risks:** false positives erode trust; single-car noise.
- **NOT yet:** AI interpretation as a *replacement* for this.

### Later / explicitly deferred

- **AI interpretation layer** — consumes Stage 3–5 *structured results*
  (trends, anomalies, deviations) and narrates them, always distinguishing
  fact/inference/hypothesis/unknown. Never feed it raw rows; never let it
  invent a diagnosis. Deferred until there are real analytics outputs to
  narrate.
- **Embedded device / mapping→C compiler** — the mapping format is kept
  portable for this, but do not start it until the telemetry model has
  stabilized on the dev platform.
- **Remote/cloud storage** — only when data volume or multi-location
  access actually justifies it. Not now.

## 4. Quick-win analytics from data you already have

Before any model work, from the idle + sweep captures already recorded:
warm-up curve shape, boost actual−requested and rail actual−setpoint
deviation, and DPF measured−modelled soot agreement. These need only a
read-only pandas script and give immediate, honest signal — a good Stage
3 warm-up and a way to prove the analytics approach before investing in
the data-model migration.

## 5. Highest-diagnostic-value signals to add next

In rough priority for an N47 diesel: **DPF differential pressure**,
**exhaust temp pre/post DPF**, **regeneration distance + count + state**,
**EGR actual vs requested**, **injector correction quantities**, then
**gearbox oil temp / converter slip** and **tank sensors**. Each ties to
a known wear/failure mode and to a baseline worth trending.
