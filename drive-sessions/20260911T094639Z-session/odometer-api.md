# Odometer API — first on-car verification

`F10-520d-dev`, 2026-09-11, mode `normal`, 22.75 km, ~43 min engine-on.
First drive after #49 / PR #50 landed. Everything below is **observed on
the car** unless labelled otherwise; the endpoint was sampled from the
VPS through the public name (TLS + the panel + wg0), not from the Pi, so
these numbers include the whole path a phone would use.

## 1. The measurement the design was waiting on

`docs/ODOMETER_API.md` justified the accumulator's 1,000 m forward slack
by the DDE's own refresh rate for `44BF` being unmeasured, and the PR
review found that a refresh interval past **41.5 s at 130 km/h** would
make the resync fire on every refresh and credit **zero** distance.

Measured over 2,169 consecutive samples (session DB, `n47d_odometer_m`):

| quantity | value |
|---|---|
| our poll interval (class `odometer`) | median 1.090 s, p95 1.280 s |
| **ECU value-change interval** | **median 1.140 s, p95 1.280 s, max 4.330 s** (n=1,335) |
| step size | min **1 m**, median 16 m, p95 37 m, max 67 m |
| backwards raw steps | **0** |
| decode quality | 2,169 / 2,169 `ok`, 0 flagged |

The ECU refreshes at least as fast as we poll, at 1 m granularity. There
is no coarse-step regime at this poll rate, and the worst observed
interval is **9.6× inside** the band that would have broken the design.
The slack stays where it is, now justified by margin rather than by
ignorance (#51).

## 2. Accumulator integrity — exact over the whole drive

| | |
|---|---|
| raw `44BF` span | 37,999 m → 60,753 m = **+22,754 m** |
| accumulator `odometer_m` at end | **22,754 m** |
| difference | **0 m** |

Over 22.75 km the accumulator credited exactly the distance the ECU
counter advanced: nothing lost to a refusal, nothing invented. `rejected`
0, `resets` 0, monotonic violations 0 across 948 endpoint samples in a
single `epoch`.

**The refusal and resync paths were therefore never exercised on the
car.** They remain covered by tests only — an honest gap, not a result.

## 3. The endpoint as a client sees it

948 samples over 36.1 min through `https://<DASHBOARD_DOMAIN>/api/odometer`:

- every response **200**; `connected` true 948/948; `clock_synced` true throughout
- one `epoch` for the whole drive (no runtime restart)
- **served anchor age** (`t - odometer_t`, what the app's interpolation
  corrects for): median 0.56 s, p95 1.14 s, max 5.04 s
- auth, re-checked on the day: no token → 401, wrong token → 401
  `{"error": "unauthorized"}`, valid token → 200; the bearer reaches
  **nothing** else (`/` and `/api/status` both 401 with it)

**Speed-integration cross-check.** Integrating `speed_kmh` over `t` the
way the app interpolates between anchors gives 22,474 m against the
odometer's 22,624 m over the same window — **0.7 % apart**. That
validates the interpolation *method*; both numbers come from the car, so
it says nothing about absolute accuracy.

## 4. Wire cost and health, with the new 1 Hz class live

| | |
|---|---|
| requests sent | 82,219 |
| failed | 21 (**0.0255 %**) |
| transport timeouts / ambiguous / late / decode_failed | 31 / 1 / 0 / 0 |
| `n47.d72.dyn.44BF` | 2,170 sent, 2,169 ok, 1 failed |
| `obd.mode01.0D` (speed, `motion`) | 16,062 sent, 16,058 ok, 4 failed |

The +16 % `normal`-mode bus cost predicted by plan simulation in PR #50
did not produce a fault rate worth noticing. Lake sync shipped all
100,327 rows with 0 pending and no error.

## 5. Two runtime behaviours worth recording

- **A clock step ended a run mid-session, by design.** Run 1
  (09:02:41–09:04:06) opened with an unsynced clock; NTP stepped
  +24.2 s; the runtime closed it and opened run 2 with
  `clock_synced = 1` rather than let one run span the discontinuity.
  The odometer was unaffected — the step widens the forward window
  rather than narrowing it, and the car was stationary.
- **A transport hiccup was bridged, not refused.** Four OBD reads timed
  out at 3.0 s, leaving a 3.5 s hole in the 1 Hz class; the next sample
  was credited +64 m (≈66 km/h across the gap) instead of being refused.
  This is the time-based forward bound behaving as intended.

## 6. Still unknown

**The ECU-vs-ground-truth scale factor.** No GPS track was logged
alongside this drive, so the app's calibration factor stays 1.0 and the
ratio between `44BF` metres and real metres remains unmeasured. That
needs one drive with a phone GPS track over the same route.
