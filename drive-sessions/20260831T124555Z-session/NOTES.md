# Drive 10 (2026-08-31) — the fragmentation fix earns its keep

**103,089 samples, 26.2 km, 41 minutes, and 98.7% of it in ONE run.**

This is the first drive recorded on the fixed transport-fault handling,
and the first with three real per-request faults absorbed without the
link being torn down. Report covers **run 1** — 101,731 samples, 40.9 min
unbroken.

| run | samples | span (UTC) | duration |
|---|---|---|---|
| **1** | **101,731** | 11:58:09–12:38:07 | **2398s** |
| 2 | 1,358 | 12:39:04–12:39:29 | 25s (shutdown tail) |

For comparison: drive 7 split into 4 runs, drive 8 into 6, over similar
distances. The single break here came at the very end.

## The `errors` table is proven on the car

Session 9 recorded it as *"correct shape, 0 rows — untested, not
proven"*. It is now proven, by the vehicle rather than by a fake
transport:

    12:00:03Z  n47.d72.dyn.46F0     transport_timeout
    12:10:47Z  egs.selector.DA2E    transport_timeout
    12:26:35Z  egs.selector.DA2E    transport_timeout

Two of the three are the **gear channel on the EGS at `0x18`** — the
exact ECU whose faults used to tear down a healthy link and split the
drive. Each was classified, absorbed, recorded, and attributed. The whole
chain fired end to end: executor classification → link survives →
`on_error` → `rec.error` → row in `errors` → per-request attribution in
`/api/diagnostics`.

Run 1 continued straight through all three.

## Two things the diagnostics view gets wrong

Both found while watching this drive. Neither is dangerous, both are the
kind of thing that quietly erodes trust in the panel.

**1. `errors` and `/api/diagnostics` do not count the same thing.**
`/api/diagnostics` reported **6** failed requests; `errors` holds **3**
rows. The missing four are OBD PIDs (`0C`, `0B`, `0D`, `49` — rpm, MAP,
speed and one more), which fail inside `ObdSession`. That layer has its
own retire-after-three-strikes policy and absorbs the failure without
ever calling `on_error`, so it is counted in the executor's stats but
never recorded as a row.

Arguably correct — the OBD path owns its own retry policy — but it means
**the `errors` table under-reports total faults**, and anyone reading only
that table would call this a 3-fault drive rather than a 6-fault one. The
two numbers need documenting as different measurements, or the OBD path
needs to report too. All four also failed on the same cycle, which points
at one dropped multi-PID exchange rather than four independent events.

**2. `success_pct` reads 100.0 with a non-zero `failed` count.**
6,963 of 6,964 rounds to 100.0. Not wrong, but "100%" sitting beside
`failed: 1` is exactly what makes someone stop believing the panel. It
should carry a second decimal, or floor below 100 whenever failures
exist.

## Soot: a second, much stronger rate measurement

Session 9 measured soot accumulating at **0.249 g/h while stationary**.
This drive gives the loaded number:

    soot          9.61 -> 10.47 g   (+0.86 g)
    distance      45.34 -> 71.55 km (26.21 km)
    duration      41.1 min

    per km        0.0328 g/km   (drives 7-8: ~0.028 — consistent)
    per hour      1.255 g/h     (session 9 idle: 0.249)

**Driving accumulates soot 5.0x faster per hour than idling.** That is
the shape fuel burn has, and it is not the shape distance has: a
stationary engine covers no distance at all yet still accumulates.

The per-km figure being stable across three drives (0.028, 0.028, 0.0328)
is why the distance reading looked convincing for so long — at typical
average speeds the two are hard to tell apart. Session 9's zero-distance
measurement is what separates them.

`regen_count` held at 93 and the opmode word stayed `0x100001` (regen bit
clear) for the whole drive, so nothing interfered.

## EGR deviation spiked to 144% — one sample, unexplained

`n47d_egr_deviation` now has a third behaviour. Idle (session 9) gave
0.00–5.54%. This drive: median **0.00**, p90 **4.17**, and a single
sample at **144.43%**, with 6 of 184 samples above 20%.

The peak landed at 11:58:44Z, 35 seconds into the drive, at 1272 rpm /
26 km/h / 29.6% pedal — a gentle pull-away, not an extreme condition.

Stated honestly:

- **Observed:** one sample at 144.43%, five others above 20%, median 0.
- **Not claimed:** that EGR is faulty. A control *deviation* of 144% is
  not obviously physical, and a single outlier at a benign operating
  point is at least as likely to be a decode artifact — a signed value
  read unsigned, or a transient the ECU publishes mid-transition.
- **Next:** check whether the raw bytes at that timestamp are near a
  sign boundary, and whether the spike recurs at pull-away on other
  drives. Until then treat the p90 (4.17%) as the useful number and the
  max as suspect.

This is the channel that session 9 rescued from the "looks dead" list. It
is clearly alive; its top of range is now the open question.

## Everything else

- **Warm start, not cold** — run 1 opens at 45 °C. The generated report's
  cold-start section is about a warm restart and its 3.0 min to 80 °C is
  not comparable with session 9's 33.6 min from 21 °C.
- **The report generator fix is verified.** `e20914c` now renders *"Oil
  ran +0.3 °C against coolant through the ramp, so no lag was seen"*
  where the old code asserted a lag unconditionally. Correct, and the
  first artifact produced by the fixed generator.
- **Envelope:** 131 km/h max, 2711 rpm, gears 1–8, boost to 1.56 bar
  (2696 hPa), rail to 1422 bar, MAF to 222 g/s, exh pre-cat to 452 °C.
- **DPF ΔP** −12.0 to 37.0 hPa. Lower peak than drive 8's 117 hPa, on a
  gentler drive — still opportunistic sampling, not the steady-state load
  points the plan asks for.
- **Provenance intact on both runs**: `mapping_set` populated, 12
  `run_mappings` rows, `clock_synced=1`. First drive where run 1 carries
  its own provenance.
- All 103,089 samples in the lake, `pending 0`.
