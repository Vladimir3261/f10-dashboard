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

### 0. PRECONDITION — stop the session fragmenting

Every drive on 2026-08-29 split into 3–18 runs. `session_report` analyses
one run at a time, so a fragmented drive cannot be summarised without
stitching, and any condition-normalised baseline is built on partial
segments. **Fix this before the next analysis-grade drive.**

Cause is traced: in `bmwdiag/mapping/execute.py`, `_run_generic`, the
transport call sits outside the try/except that guards decoding, so a
timeout or an `HsfzNack` against the *secondary* EGS at `0x18` tears down
a healthy `0x12` link and starts a new run. The fix is an
unroutable-target / transient-request exception at the
`bmwdiag/protocol/` seam that `_run_generic` can catch per request —
`bmwdiag` is dependency-free, so it cannot catch `live.py`'s `HsfzNack`
directly. Mirror `ObdSession`'s existing 3-strikes retirement idiom.

### 1. A genuine cold start (still never captured)

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

### 2. DPF ΔP vs exhaust flow — the first real baseline

The project's headline goal is condition-normalised baselines, and DPF
restriction is the best-defined one. Drive 7 saw ΔP span 0.96–86.0 hPa,
but scattered across whatever load happened to occur.

What is needed instead is **steady-state load points held long enough to
average**: roughly 60–90 s each at a constant speed/gear, at 3–4
different loads (e.g. 50, 80, 100, 120 km/h in top gear on a level road),
so ΔP can be plotted against MAF rather than against time. Repeat the
same points on later drives and the curve becomes a trend.

This is what turns "DPF ΔP has crept from 24–27 to 35 mbar" from an
anecdote into a measurement. Note the regen just reset the filter's
state, so **now is an ideal moment to start the clean-filter baseline.**

### 3. Channels that look wrong or unexercised

- **`n47d_egr_deviation` (0x487A)** read a flat 0.0 % for the whole of
  drive 7 (103,701 samples), yet read 13.65 % earlier the same day at
  warm idle. Confirm it actually varies under load rather than being
  pinned by our decode.
- **`lambda`** sits at exactly 2.0 for 5,773 of 7,797 samples — the
  "no value" sentinel. Either find the DDE's real lambda DID or mark the
  OBD channel unusable so it stops polluting reports.
- **`egs_da2e_b0`** is still constant 0. Retire it or explain it.
- **P/R/N/D selector and reverse encoding** remain unfound (needs a
  `0x18`/`0x63` DID scan).

### 4. Superseded / historical

The original experiments 1 and 2 (boost under load, transmission
channels) are done — see the DONE sections above. Experiment 3
(cold-start ramp) is now item 1. Experiment 4 (DPF under regeneration)
is partly done and its follow-up is the soot analysis above.

## Not blocked on the car

- Runtime polish: the F303 channels are all `slow` class; a full slow
  cycle now sends ~13 x (2 setup + 1 poll) frames. If that stutters the
  fast OBD channels, stagger the F303 reads across cycles or add a
  slower polling class for them. Watch the dashboard Hz with
  `--extra-mappings` enabled and tune if needed.
- Consider a dedicated `dpf`/`trans` polling class in the mapping so the
  heavy proprietary reads don't compete with fast OBD.
