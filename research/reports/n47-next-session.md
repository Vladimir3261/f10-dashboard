# N47 — next on-car session (planned)

Picking up after 2026-08-25. The core telemetry is done: 13 d72n47a0
dynamic channels are `verified` on F10-520d-dev (idle OBD cross-check +
throttle sweep), wired into the runtime behind `--extra-mappings`, and
shown on the 3-mode dashboard. See `n47-oncar-results.md` for what was
proven. This file is the to-do for the next session.

## Setup reminders (every session)

- Stop `live.py` before running any validation tool — the ZGW serves one
  HSFZ client at a time.
- Laptop needs a `169.254.x.x` link-local address on the ENET cable.
- Ignition on; engine running for anything load- or flow-dependent.
- Artifacts land in `validation-runs/` (tracked, VIN-redacted) and
  `local/validation-runs-raw/` (gitignored, VIN).
- The next drive is the issue #15 final test: follow
  [`docs/FINAL_TEST.md`](../../docs/FINAL_TEST.md) — the ordered
  checklist with every command, in the order § 3b gives.

## DONE — engaged gear found (2026-08-27)

The engaged 1..8 gear is **EGS 0x18, 22 DA2E, byte 1**. Confirmed on a
road drive: it steps monotonically with speed (gear 1 avg 2 km/h, 2->16,
3->27, 4->38, 5->48, ranges overlapping on shifts) - textbook ZF 8HP.
Verified, wired to the dashboard, syncing to ClickHouse as
`transmission.gear`. The earlier parking-lot rejection was a false
negative (a ZF 8HP won't engage high gears at standstill). Byte 0 of
DA2E is constant 0 (not the selector the OBDb map claimed). D031 carries
no gear. 0xFF at standstill is clamped out (valid 1..8).

Still open: a clean P/R/N/D **selector** (not found yet - would need a
0x18/0x63 DID scan). Reverse-gear encoding also uncaptured.

## DONE — the DPF/EGR channels are validated and live

`mappings/candidates/bmw/dde/n47/d72n47a0_dpf_egr.yaml` carries
`verification.status: verified` (40-min drive, 2026-08-26) and all 7
channels ship in the runtime via `--extra-mappings`: DPF differential
pressure (0x44F8), exhaust temp before DPF (0x44EF) and before catalyst
(0x44F2), distance-since-regen (0x44BF), successful-regen count (0x44B8),
operating-mode status word (0x467E), EGR control deviation (0x487A).

Two of them have since been exercised much harder than that first drive:
0x467E was caught mid-regeneration on 2026-08-29 (see below), and 0x44B8
/ 0x44BF were seen stepping and resetting across the same event. The two
still not fully trusted are the **soot pair** (0x44BE / 0x44C1, see the
contradiction below) and **EGR deviation** (0x487A, item 3).

## DONE — boost under real load (2026-08-29)

Superseded experiment 1 below. Road drives on 2026-08-29 stretched every
flow channel to the top of its range: boost act to 2712.9 hPa, rail to
1746.6 bar (set 1128+), MAF to 205.4 g/s, MAF/cyl to 1268 mg/hub, turbine
speed to 2122+ rpm, load to 100%. The scales hold under real demand.

Also **superseded experiment 2**: the transmission channels are built,
verified and running — gearbox oil temp, turbine speed and converter
temp ship in `d72n47a0_gearbox.yaml`, and engaged gear comes from EGS
`0x18` directly. No SG_FUNKTIONEN derivation needed.

## DONE — a regeneration was captured live (2026-08-29)

Experiment 4 below is **partly achieved**, and it produced the sharpest
open question in the project.

The operating-mode word `CoEOM_stOpModeAct` (0x467E) had only ever been
seen regen-inactive. On 2026-08-29 it was observed in **three** states
across the day:

| session (UTC) | opmode | bits | regen bit 0x02 |
|---|---|---|---|
| 13:15–13:45 | 1048577 | `0x100001` | no |
| **13:45–14:53** | **1310722** | **`0x140002`** | **ACTIVE** |
| 15:27–15:48 | 1048577 | `0x100001` | no |

Corroborated by two independent counters either side of it:
`n47d_regen_count` 92 → **93**, and `n47d_dist_since_regen` reset from
241.6 km to ~9 km. **The regen bit's meaning is now behaviourally
confirmed, not just source-claimed** — the strongest evidence we have for
0x467E. Note `0x40000` also appears only in the regen state and `0x1`
only outside it; those two look regen-correlated and are worth pinning.

### The contradiction to resolve: soot did not fall

A completed regeneration burns soot out, so `n47d_soot_meas` (0x44BE) /
`n47d_soot_model` (0x44C1) should have **dropped** across that boundary.
They did the opposite:

| | before | during regen | after |
|---|---|---|---|
| `soot_meas` | 7.34 → 7.90 g | 7.86 g | **8.35 → 8.65 g** |

For scale, on 2026-08-25 the same channel read **0.09 g**. So it has gone
0.09 → 8.65 g over four days *and through a regeneration*.

- **Observed:** soot rose monotonically across a confirmed regen.
- **Inference:** these channels are not "current soot load in the filter".
- **Hypotheses (untested, in order of cheapness to test):** cumulative
  soot produced since new, rather than currently stored; an ash estimate;
  a wrong scale/offset in the candidate mapping; or a unit that is not
  grams.
- **Not claimed:** that the scale is wrong. One event is not a
  validation, and the channel is still `candidate`.

**This is an analysis task, not a drive task.** The 14:57–15:24 session
that straddles the regen is in ClickHouse (its local DB was dropped).
Query it first — `analysis/clickhouse/insights.sql` — and plot soot
against opmode over that window before planning any new on-car work. If
soot steps down *at any point* the "cumulative" hypothesis dies.

## Planned experiments, in priority order

### 0. ~~PRECONDITION — stop the session fragmenting~~ DONE 2026-08-30

Every drive on 2026-08-29 split into 3–18 runs, because in
`bmwdiag/mapping/execute.py` the transport call sat outside the
try/except guarding decoding: a timeout or an `HsfzNack` against the
*secondary* EGS at `0x18` tore down a healthy `0x12` link.

Fixed in `69d879f`. Note the approach differs from what was planned
here: rather than adding typed exceptions at the `bmwdiag/protocol/`
seam, faults are classified **structurally** (`_is_request_fault`), so
`bmwdiag` still imports nothing about the transport. A consecutive-fault
budget distinguishes one unreachable ECU from a dead link, and every
fault is recorded to `telemetry.channel_errors`.

A follow-up on branch `fix/transport-failure-scope` rests a request that
keeps failing and stops a nack counting against the link budget —
reviewed, awaiting merge.

### 1. DONE — the cold start was captured (2026-08-30)

Captured stationary, idling from **21.0 °C** against 17–19 °C ambient over
41 minutes in one unbroken run
(`drive-sessions/20260830T222056Z-session/`). Coolant and oil track to
within 0.1 min at 40/60/80/85 °C — the strongest validation the
temperature scales have had, two independently-sourced channels agreeing
across a 67-degree climb.

**But the load-driven oil lag is still uncaptured.** At idle oil ran a
mean +0.27 °C *above* coolant, not below: with no load, oil takes its heat
from the block rather than from work done. What remains open is a cold
start followed by **driving**, which is the only way to see the lag the
plan predicted. Keep this item open for that.

Two channels also resolved themselves in the same session, both from item
3 below: the operating-mode word has a distinct cold-start state
(`0x80870001`, first ~1.2 min), and `n47d_egr_deviation` is not dead — it
spans 0.00–5.54 % during the warm-up transient and settles to 0.0 once
warm, so EGR health should be trended on warm-up, not cruise.

### 1b. Still open — a cold start WITH driving

The single most valuable *on-car* item, and still open after four
sessions — every run so far has begun warm. Drive 7's run 3 opened at
89 °C, which is why its report shows a nonsense "→80 °C in 0s".

Requires: car stood **overnight**, `f10-dashboard` started *before*
cranking, and run 1 kept unfragmented (see item 0).

Capture, in one unbroken run: `coolant` (OBD 0x05), `n47d_coolant`
(461B), `n47d_oil_temp` (4517), `n47d_engine_temp` (4BC3),
`n47d_charge_air_temp` (4843), plus `ambient` and `iat` for the starting
reference.

Expect coolant and oil to climb from ambient toward ~90 °C with oil
lagging coolant throughout. A clean ramp tracking OBD PID 0x05 the whole
way is the strongest temperature validation available, and it is the
last unproven part of the temperature scales.

### 2. ~~DPF ΔP vs exhaust flow~~ VOID — this car has no filter

**Do not run this experiment.** It was the planned first baseline, and it
is dead: **the particulate filter was removed from this vehicle**
(established 2026-08-30). `n47d_dpf_dp` measures the pressure drop across
an empty pipe, and the two soot channels report an ECU model with no
physical feedback. See [`docs/DPF_SOOT.md`](../../docs/DPF_SOOT.md).

Recorded here explicitly because this page proposed it four sessions
running, and the next reader would otherwise plan a drive around it.

**The steady-state method is still right — point it at a live channel.**
Hold 60–90 s each at 3–4 constant speed/gear load points on a level road,
so the y-axis can be plotted against MAF rather than against time, and
repeat the same points on later drives.

The replacement baseline is **boost actual vs setpoint**
(`n47d_boost_act` / `n47d_boost_set`): two verified channels, real
physics, and a deviation that is directly interpretable as turbo/VNT
health. Rail actual-vs-setpoint and EGR deviation are the next two.

### 3. Channels that look wrong or unexercised

- **`n47d_egr_deviation` (0x487A)** — *no longer suspected dead.*
  Session 9 showed it moving 0.00–5.54 % across 26 distinct values, all
  in the warm-up transient and back to 0.0 once warm. So EGR health
  belongs on the **warm-up**, not on cruise, which is where it was being
  looked for.

  **One open outlier.** Drive 10 (`drive-20260831T115807Z`, run 1,
  11:58:44Z) has a single sample at **144.43 %**, 35 s in at 1272 rpm,
  26 km/h, 29.6 % pedal — a gentle pull-away, not an extreme. p90 was
  4.17 %; the max is suspect and should not be quoted.

  A signed-read-as-unsigned artifact was proposed and **does not fit**:
  144.43 % is raw 11832 (0x2E38) at `scale: 0.012207`, which is positive
  under either interpretation, and a negative `int16` misread would land
  near full scale (800 %), not 144 %.

  The likelier mechanism is **F303 cross-talk**. Every DDE dynamic read
  shares dynamic DID `0xF303` and re-arms it per read; `d72n47a0_dynamic.yaml`
  already warns that a stale define decodes one measurement's bytes as
  another's. A single outlier at a benign operating point is what that
  would look like. To settle it, capture the raw bytes at that timestamp
  — the decoded value alone cannot distinguish the two.

  Until then: **p90 is the usable number.** Adding `valid_min`/`valid_max`
  to the decode would stop it being stored as truth, but would currently
  *drop* it silently — which is why this wants the Stage-1 quality flag
  work rather than a clamp.

- **`maf` reads exactly 222.22 g/s with the engine stopped** (found by
  F10-VM in the lake, 2026-08-31): 13,622 samples at exactly 222.22
  (raw 22222), **13,601 of them at rpm = 0**, where the channel
  alternates between ~1.1 g/s and this value. 222 g/s with the engine
  off is impossible; it looks like a DDE default/init value, but 22222
  is not a natural sentinel bit pattern and no source documents it, so
  declaring it `invalid:` would be invented data. **Car-side capture
  needed, same as the EGR case:** with ignition on and engine off,
  record the raw PID `0x10` response bytes. Until then, treat MAF at
  rpm = 0 as unusable and gate MAF analytics on engine-running.
- **~~`lambda` sentinel pollution~~ — FIXED (Stage 1, 2026-08-31).**
  0xFFFF is declared `invalid:` in engine.yaml v4; the rows now carry
  `quality='sentinel'` and are excluded from display and analysis.
  Finding the DDE's real lambda DID remains open as a nice-to-have.
- **`egs_da2e_b0`** is still constant 0. Retire it or explain it.
- **P/R/N/D selector and reverse encoding** remain unfound (needs a
  `0x18`/`0x63` DID scan).

### 3b. Issue #15 candidates — the drives that would verify them (2026-09-06)

The offline half of issue #15 landed as seven candidate files plus a
read-only DTC tool, all `verification.status: candidate`, none in
`run_car.sh`. `docs/TELEMETRY_CANDIDATES.md` has the hypothesis, the
exact invocation and the pass/fail per domain; this is the to-do. Each
one: stop `live.py`, `identify`, the sweep below, commit the artifact,
then flip the file to `verified` (version 2, artifact named in
`verification.method`) or `rejected`.

1. **Injector corrections** (`d72n47a0_injectors.yaml`) — warm idle,
   `run --all` then `sweep --all --seconds 180`. Pass: four
   corrections within ±2 mg/hub summing to ~0, setpoint a few mg/hub.
   Watch for values clustering at ±20 — that would be the **d73 scale**
   (`×0.156863 −20`, byte-wide), which disagrees with the d72 row and
   is noted in the file.
2. **EGR position pair** (`d72n47a0_egr.yaml`) — 120 s sweep with two
   hard accelerations, then a drive with `./run_car.sh --extra-mappings
   <file>` so `n47d_egr_deviation` (0x487A) is in the same session.
   This is what `docs/HEALTH_MODELS.md` § EGR (`unavailable`) is waiting
   for. Bonus: a 3-request rotation change is a chance to re-test the
   F303 cross-talk hypothesis from item 3 above with the raw bytes.
3. **VNT / swirl / throttle pairs + governor deviation**
   (`d72n47a0_airpath.yaml`) — 180 s sweep, then a drive with
   full-throttle pulls under `--extra-mappings`. Pass includes
   `n47d_boost_gov_dev` matching `boost_set − boost_act` (verified pair)
   in sign and within ~50 hPa. If the plain `TrbCh_*` rows are constant,
   record it and try the `*VNT` rows next time — not before.
4. **IBS** (`d72n47a0_ibs.yaml`) — idle, 120 s sweep of 4286 + 428D with
   a known load step (dipped beam ~8–9 A, rear defroster ~15–20 A), then
   `run --all`. Pass: the step reproduces within 2×, IBS voltage within
   0.3 V of PID 0x42.
5. **EGS speeds** (`egs/f10_transmission_speeds.yaml`) — 300 s sweep
   (no `--ecu`: the sweep now routes each request to the file's fixed
   `target: 0x18`; the EGS answers no OBD PID and cannot be *discovered*)
   then a drive under `--extra-mappings` with locked-up
   cruise. The drive **assigns** the two `DA2A` words (turbine vs
   output shaft, by ratio against `0x46ED` and road speed per gear) and
   fits `DA12` against `0x46F0` — the file names nothing until then.
6. **Tank** (`d72n47a0_tank.yaml`) — `run` at two known fuel levels
   across a refuel. The KOMBI senders have **no traceable source**; the
   owner's `local/captures/kombi_dids.json` (not on the build host) is
   to be mined first — see the doc for what to look for (≥2-byte 0x63
   DIDs that move with fuel level, a left/right pair, vs `IFTNK`).
7. **DTC readout** — `python3 tools/dtc.py --count --detail` once per
   ECU (0x12, then `--ecu 0x18` — the read address only; discovery
   still finds the engine by capability), the first `validation-runs/*-dtc/`
   artifact. Read-only by construction (0x19 only; 0x14 has no code
   path). The count must agree with PID 0x01 (next item).
8. **SAE extras** (`obd/engine_sae_extra.yaml`) — 60 s sweep at idle
   through key-off with `tools/dtc.py --count` for the cross-check.
   On pass: PIDs 0x01 and 0x4C move into `engine.yaml` as **v6** with a
   pin re-base; the other eight unpolled advertised PIDs stay out for
   the reasons in the doc.

Rotation cost if *all* were loaded (measured offline by the dress
rehearsal in `tests/test_final_test_readiness.py`, 2026-09-10):
`dde_dyn` 23 → 34 requests (~14 s per member) plus a new `dde_slow`
rotation of 15, `egs` 1 → 2, `egs_slow` 1. Since `config/modes.yaml`
**v3** `dde_slow` and `egs_slow` are named in every mode and treated
like `slow`: exempt from `sampling`'s sleep, ×1.0 in `long`, ×0.1 in
`debug`. Validate one file at a time — `./run_car.sh --candidate egr`
loads one for the drive half without editing the script; what enters
the default set afterwards is a separate decision per file. The
driver's-seat checklist for the whole day is
[`docs/FINAL_TEST.md`](../../docs/FINAL_TEST.md).

### 4. Superseded / historical

The original experiments 1 and 2 (boost under load, transmission
channels) are done — see the DONE sections above. Experiment 3
(cold-start ramp) is now item 1. Experiment 4 (DPF under regeneration)
is partly done and its follow-up is the soot analysis above.

## Not blocked on the car

- **~~Runtime polish: stagger the F303 reads.~~ DONE.** They have their
  own staggered `dde_dyn` class — one member per firing, so a member
  refreshes every ~11 s and the fast channels are never blocked for more
  than one exchange. The EGS has its own class too. Rates are wall-clock
  per channel and scaled by drive mode; see
  [`docs/POLLING_AND_SAFETY.md`](../../docs/POLLING_AND_SAFETY.md).
- **~~TECH DEBT: the host clock.~~ FIXED 2026-08-30.** The Pi has no RTC
  and corrected itself forward 76.5 minutes mid-recording on 2026-08-29,
  corrupting run 1's timeline and shipping it to the lake that way. Now:
  the services wait on `time-sync.target`, the runtime waits briefly and
  labels every run with `clock_synced`, and a mid-run step ends the run
  so none spans a discontinuity. Filter on `sessions.clock_synced = 1`.
  Runs recorded before the flag existed stay unknown (NULL) and must
  still be treated as suspect. See `docs/ROADMAP.md`.
- **~~The fragmentation fix is written but unapplied.~~** Landed as
  `69d879f`; the stash it referred to was dropped after review. See
  item 0 above.
