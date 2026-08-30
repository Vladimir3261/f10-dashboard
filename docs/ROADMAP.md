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

### Findings that reordered the priorities

**1. The DPF is gone — DPF analytics are out of scope for this car.**
*(Resolved 2026-08-30.)* The soot decode was falsified by a captured
regeneration: `n47d_soot_meas` did not drop across it, never decreased in
1404 points, and was a near-perfect straight line in distance. The reason is
that **this vehicle's particulate filter was removed**, so the channel can
only report the ECU's internal model. Full account in
[`DPF_SOOT.md`](DPF_SOOT.md).

Consequences: `n47d_soot_meas`, `n47d_soot_model` and `n47d_dpf_dp` describe
hardware that is not there and stay `candidate` permanently. **DPF ΔP vs
exhaust flow is no longer the Stage-3 flagship** — see item 4 below. What
survives is a real finding: the ECU still *commands* regenerations against
its model (count 92 → 93), which costs fuel and is the classic oil-dilution
path, on a car where they clean nothing.

**2. Drives fragment into runs, which corrupts longitudinal analysis.**
*(Fixed 2026-08-30, commit `69d879f`.)* Drive 7 recorded as 4 runs, not 1.
Cause was error handling, not the cable: in `bmwdiag/mapping/execute.py` the
transport call sat outside the try/except that guards decoding, so one
`TimeoutError` (or an `HsfzNack` from the EGS) tore down the whole HSFZ link
instead of failing that one request. Per-request faults are now distinguished
from link faults, with a consecutive-fault budget, and every fault is
recorded to `telemetry.channel_errors`.

**3. Polling was ~3× heavier than it needed to be.**
*(Fixed 2026-08-30.)* Eleven OBD channels at 10 Hz were 83% of stored rows at
0.1–3.8% distinct values. Rates now follow the physics of each channel, and a
drive mode scales them at runtime. 7,740 → 2,735 requests/min in `normal`.
See [`POLLING_AND_SAFETY.md`](POLLING_AND_SAFETY.md).

### Recommended order

1. **The overnight battery test** — the one remaining question about whether
   sustained polling can harm the car. `off` mode makes it a clean
   experiment: connected but silent isolates the cable from the polling.
2. **Stage 1 data quality** — populate `quality` (saturated / sentinel /
   stale). The MAP-saturation case proves the need and gives a test.
3. **Stage 3 analytics** — condition-normalized baselines. The inputs are
   now trustworthy (fault rates recorded, mode recorded per session, mapping
   version per sample).
4. **Pick the new Stage-3 flagship.** DPF ΔP vs flow is void here. The
   strongest replacements, all verified channels with real physics behind
   them: **boost actual-vs-setpoint tracking** (turbo/VNT health),
   **rail pressure actual-vs-setpoint** (injection system), **EGR control
   deviation**, and **warm-up thermal behaviour** (coolant/oil vs ambient
   and load). Boost tracking is the natural first: two channels, both
   verified, and a deviation that is directly interpretable.

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

#### TECH DEBT — the host clock can rewrite the timeline (FIXED 2026-08-30)

**Raised 2026-08-29 (drive 8). Fixed 2026-08-30.**

The Pi has no RTC. On 2026-08-29 it booted with a stale clock, began
recording immediately, and `systemd-timesyncd` corrected the clock
**forward by ~76.5 minutes 47 seconds later** — mid-run. The result is a
session whose run 1 contains a phantom 4578.1s gap, a fictitious 5064s
duration for what was really ~8 minutes, and ~18 seconds of samples
stamped 76 minutes in the past. All of it shipped to ClickHouse with
those timestamps.

This is a data-quality defect of exactly the kind Stage 1 exists to
prevent, and it is more dangerous than a bad *value*: a bad *clock*
silently corrupts every rate, gradient and trend derived from the data,
which is the entire premise of the long-term behavioural model. A drive
that looks like a 76-minute idle never happened.

**What was done** — three defences, because no single one covers it:

1. **Ordered after time sync.** `f10-dashboard.service` and
   `f10-sync.service` gained `After=/Wants=time-sync.target`. `Wants`,
   not `Requires`: a car parked out of range must still record.
2. **A bounded wait, then honesty.** `live.py --wait-for-clock` (20 s by
   default) gives NTP a chance before the first run opens; on expiry it
   records anyway and says so. An unknown clock reads as NOT synced —
   "probably fine" is the assumption that shipped the broken timeline.
3. **A step ends the run.** `time.monotonic()` cannot jump, so the
   difference between it and `time.time()` is constant unless the clock
   is stepped. Past 2 s, the runtime closes the run and opens a new one,
   so **no run ever spans a discontinuity** — the same invariant a mode
   change preserves.

**How to use it:** `sessions.clock_synced` (`1` / `0` / NULL for runs
predating the flag). Anything time-derived must filter on it:

```sql
WHERE clock_synced = 1
```

Existing rows are deliberately NOT back-filled: a session recorded
before the flag existed cannot be shown to have had a sane clock, and
the 2026-08-29 session is proof at least one did not. That session stays
suspect — see `drive-sessions/20260829T183627Z-session/NOTES.md`.

**Not done: retro-correcting timestamps.** The offset is known, so the
earlier rows *could* be shifted — but the sync agent ships continuously,
the lake keys on `(vehicle, channel, ts, session)`, and a corrected `ts`
would insert a duplicate rather than replace anything.

**Still worth doing: fit a hardware RTC.** It is the only fix that also
corrects the session *filename*, which is stamped from the clock at
startup and so is still wrong on an offline boot.

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

Before any model work, from the captures already recorded: warm-up curve
shape, boost actual−requested, rail actual−setpoint deviation, and EGR
control deviation. These need only a read-only script and give immediate,
honest signal — a good Stage 3 warm-up and a way to prove the analytics
approach before investing in the data-model migration.

*(DPF measured−modelled soot agreement was on this list. It is void: the
filter was removed, so both values are the same model and their agreement
means nothing. See [`DPF_SOOT.md`](DPF_SOOT.md).)*

## 5. Highest-diagnostic-value signals to add next

In rough priority **for this car**: **injector correction quantities**,
**EGR actual vs requested**, **boost / rail actual vs setpoint**,
**regeneration distance + count + state**, then **gearbox oil temp /
converter slip** and **tank sensors**. Each ties to a known wear/failure
mode and to a baseline worth trending.

The generic N47 priority would put **DPF differential pressure** and
**exhaust temp pre/post DPF** at the top. They are demoted here for a
vehicle-specific reason: the filter was removed, so those channels carry
no information about this car. Regeneration state stays on the list —
not as filter health, but because the ECU still runs regens against its
model, and their frequency is a real fuel and oil-dilution cost.
