# The DPF soot channels — what they are not

> **Enforced since 2026-09-01.** The fact that this car has no filter is
> no longer only documented — it is configuration that the analytics
> layer reads. See [`VEHICLE_PROFILE.md`](VEHICLE_PROFILE.md). A report
> can no longer state that differential-pressure sensing is healthy on a
> vehicle with no filter, because it asks first.


> **Resolved, 2026-08-30.** **This vehicle has no particulate filter — it
> was removed.** Every anomaly below follows from that, and the DPF
> channels can only ever report the ECU's internal model, never physical
> filter state. `n47d_soot_meas` and `n47d_soot_model` stay `candidate`
> permanently; **no DPF health conclusion can be drawn from this car**,
> and DPF analytics are out of scope for it. The investigation is kept
> because the *method* generalises and because the surviving finding
> below — that the ECU still runs regenerations against its model — is
> worth knowing.

## The short version

`n47d_soot_meas` and `n47d_soot_model` decode to plausible grams and were
marked `verified` on a warm-idle cross-check. Their first real test
failed: the value did not drop across a completed regeneration. The
reason is now known — there is no filter for it to describe.

## What was observed

A regeneration completed between drives on 2026-08-29. Two independent
counters agree, so the event itself is not in doubt:

- `n47d_regen_count` 92 → 93
- `n47d_dist_since_regen` reset to 0

Across that boundary, from the lake:

| day | regen | dist since regen | `soot_meas` |
|---|---|---|---|
| 2026-08-26 | 92 | 29 → 63 km | 0.12 → 1.94 g |
| 2026-08-27 | 92 | 78 → 99 km | 2.52 → 3.23 g |
| 2026-08-28 | 92 | 204 → 228 km | 6.55 → 7.22 g |
| 2026-08-29 | **92 → 93** | **reset to 0** | **7.22 → 9.35 g** |

Two facts, both machine-checked rather than eyeballed:

1. **It never decreases.** Over 1404 points spanning 2026-08-26 to 08-29
   the value decreased exactly **0** times (range 0.12 → 9.35 g).
2. **Within a regen cycle it is a straight line in distance.** Slope ≈
   **0.032 g/km**, consistent to about 2% over 164 km, intercept ≈ 0.

## How the missing filter explains all of it

Each observation that looked anomalous is exactly what a
model-without-feedback does:

- **No drop across a regeneration** — there is no soot to burn out. The
  channel integrates forward with nothing physical to correct it or zero
  it against.
- **Near-perfect linearity in distance** — with no measurable ΔP to
  drive it, the estimate can only be a modelled one. The data said "this
  is a model, not a measurement" before we knew why. *(Distance turned
  out to be the wrong variable — see the third line of evidence below.
  It is time and fuelling.)*
- **`soot_meas` ≈ `soot_model` to ~0.01 g despite scales differing by
  1.53×** — with no real measurement available, "measured" almost
  certainly falls back to the modelled value. They agree because they
  are the same estimate, so their agreement was never the independent
  corroboration the original verification took it for.

## Third line of evidence: it rises with the engine idling, not moving

**Session 9 (2026-08-31)** settles what the linearity meant. The car sat
stationary for 41 minutes, idling from cold, and never moved:

| | |
|---|---|
| `n47d_soot_meas` | 9.35 → 9.52 g (**+0.17 g**) |
| elapsed | 40.9 min |
| **rate** | **0.249 g/hour, at zero distance** |
| `n47d_dist_since_regen` | 45.24 → 45.24 km (**Δ exactly 0.00**) |
| `n47d_regen_count` | 93, unchanged |
| rpm | mean 741, max 1295 — never left idle |

**Distance did not move and soot still rose.** That kills the reading I
had drawn from the 0.032 g/km linearity: distance was *correlating* with
the real driver, not causing anything. Running time was riding along
underneath it the whole time.

The two rates agree, which is the confirmation rather than a coincidence:
0.249 g/h against ~0.028 g/km from drives 7–8 is roughly the same
accumulation at a ~30 km/h average — exactly what you would expect if
time is the driver and distance merely tracked it.

So there are now **three independent behaviours** pointing the same way:

1. it never decreases, including across a confirmed regeneration;
2. it is near-perfectly linear within a regen cycle;
3. it accumulates at a steady per-hour rate with the car stationary.

Together those describe a **cumulative estimate of soot produced** —
integrated from fuelling and running time — not a measurement of what is
in a filter. Which is what you would build if you had no filter to
measure, and is consistent with everything else on this car.

## The finding that survives

**The ECU still commands regenerations against its model.**
`n47d_regen_count` incremented 92 → 93 on a car with nothing to
regenerate. Those cycles are real: post-injection, raised exhaust
temperature, extra fuel, and the classic oil-dilution path — all with no
filter to clean. That is a genuine, actionable fact about this vehicle,
and it is the only DPF-related thing this channel set can tell us.

`n47d_dist_since_regen` and `n47d_regen_count` remain useful for
*that* — observing the ECU's regeneration behaviour. `n47d_soot_meas`,
`n47d_soot_model` and `n47d_dpf_dp` describe hardware that is not there.

## Why the original verification missed it

It was a **single warm-idle reading**. At idle, "current load ≈ 0.09 g"
and "cumulative ≈ 0.09 g" look identical. No single-point reading could
distinguish them.

The lesson is about method rather than this channel, and it generalises
to every scale we validate:

- A scale that reproduces one plausible number has not been validated,
  it has merely not yet been contradicted. A counter needs to be watched
  across the event it counts.
- **Vehicle configuration is part of a channel's provenance.** A
  verification note that records the reading but not the state of the
  hardware it describes can be defeated by a fact nobody wrote down.
  `local/VEHICLES.md` is where such facts belong for this car.

## What was NOT worth doing

Correlating `n47d_soot_meas` against `n47d_dpf_dp` at comparable exhaust
flow was the front-running next test, on the reasoning that real soot
loading raises ΔP while a distance counter does not. **It is void here**:
ΔP across a removed filter measures nothing. Recorded so the test is not
proposed again.

## Consequence for the roadmap

"DPF ΔP vs exhaust flow" was the flagship worked example for the Stage-3
condition-normalized baselines, and it is dead for this vehicle. The
baseline machinery is unaffected — it needs a different first subject.
Candidates, all verified channels with real physics behind them: boost
actual-vs-setpoint tracking, rail pressure tracking, EGR deviation, and
warm-up thermal behaviour. See `docs/ROADMAP.md`.
