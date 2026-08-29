# The DPF soot channels — what they are not

`n47d_soot_meas` and `n47d_soot_model` decode to plausible grams and were
marked `verified` on a warm-idle cross-check. Their first real test failed.
They are back to `candidate`, and **no DPF health conclusion should rest on
them** until their meaning is settled.

This matters more than an ordinary wrong channel: a mis-read soot signal does
not fail loudly, it produces confident wrong answers about the filter — which
is exactly the thing this project exists to watch.

## What was observed

A regeneration completed between drives on 2026-08-29. Two independent
counters agree, so the event itself is not in doubt:

- `n47d_regen_count` 92 → 93
- `n47d_dist_since_regen` reset to 0

Across that boundary, from the lake (`vehicle_id` excluding demo data):

| day | regen | dist since regen | `soot_meas` |
|---|---|---|---|
| 2026-08-26 | 92 | 29 → 63 km | 0.12 → 1.94 g |
| 2026-08-27 | 92 | 78 → 99 km | 2.52 → 3.23 g |
| 2026-08-28 | 92 | 204 → 228 km | 6.55 → 7.22 g |
| 2026-08-29 | **92 → 93** | **reset to 0** | **7.22 → 9.35 g** |

Two facts, both machine-checked rather than eyeballed:

1. **It never decreases.** Over 1404 points spanning 2026-08-26 to 08-29 the
   value decreased exactly **0** times (range 0.12 → 9.35 g).
2. **Within a regen cycle it is a straight line in distance.** Slope ≈
   **0.032 g/km**, consistent to about 2% over 164 km, intercept ≈ 0.

## What that rules out

**It is not "current soot load in the filter."** A completed regeneration
burns soot out, so a current-load channel must drop across one. This did not
drop — it kept climbing.

The linearity is the second problem. Real soot accumulation depends on engine
load, exhaust temperature and flow; a channel that is a near-perfect function
of distance alone is not measuring the filter, it is modelling it — or
counting something else entirely.

## What is still open

- **Cumulative since an adaptation reset.** Fits the monotonicity. Would need
  to know what resets it; the value was ~0.09 g at the 2026-08-25 validation
  and 0.12 g when logging began, so *something* zeroed it shortly before.
- **An ash estimate.** Ash genuinely does not burn off at regeneration, which
  fits perfectly — but ash accumulates over tens of thousands of km, not at
  0.032 g/km.
- **A distance-driven soot model** the ECU uses as an input rather than a
  measurement.

A second oddity is unexplained: `soot_meas` and `soot_model` track each other
to within ~0.01 g **despite scales that differ by a factor of 1.53**
(0.015259 vs 0.01). Either both read the same underlying quantity and one
scale is wrong, or the raw registers differ by exactly the inverse ratio.
The original verification read their agreement as two independent estimates
corroborating each other; if they are the same register, that agreement was
never evidence of anything.

## Why the original verification missed it

It was a **single warm-idle reading**. At idle, on a filter that had just
regenerated, "current load ≈ 0.09 g" and "cumulative ≈ 0.09 g" look
identical. No single-point reading could distinguish them. Only straddling a
regeneration could — and the first time we did, it failed.

The lesson is about method rather than this channel: a scale that reproduces
one plausible number has not been validated, it has merely not yet been
contradicted. A counter needs to be watched across the event it counts.

## What would settle it

In rough order of how decisive each would be:

1. **A second regeneration**, captured with logging running throughout. If
   the value again fails to drop, "current load" is dead beyond argument and
   the cumulative hypothesis is the front-runner.
2. **The SGBD definition** for IMRUP (0x44BE) and IMPAS (0x44C1) — the
   authoritative answer, if the tables can be found. See
   `docs/MAPPING_RESEARCH.md`.
3. **Correlate against `n47d_dpf_dp`** (differential pressure) at comparable
   exhaust flow. Real soot loading raises ΔP; a distance counter does not.
   This is the one test available from data already in the lake.

Until then the honest description is: *a monotonic quantity in grams, closely
proportional to distance since regeneration, meaning unknown.*
