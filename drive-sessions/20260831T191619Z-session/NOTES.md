# Drive 11 (2026-08-31, evening) — first drive on the data-quality layer

**101,420 samples, 31.1 km, 50.8 min, 4 runs, 1,997 samples/min.**
Recorded on `acf91dd` (master head), which carries Stage 1. Report covers
**run 2** — 78,444 samples, 43.3 min, the main continuous stretch.

This is the acceptance test for the data-quality layer against the car,
and it passes: **716 rows carry a non-`ok` label** where previously every
one of them was either silently dropped or stored as if it were a real
measurement.

## Quality labels — the first ever recorded

| quality | rows | channel |
|---|---|---|
| `ok` | 100,704 | — |
| `saturated` | 523 | `map` |
| `sentinel` | 130 | `lambda` |
| `clipped` | 63 | `gear` |

`map` saturating at 255 kPa under boost and `lambda` sitting at its
`0xFFFF` no-value sentinel are both **expected and predicted**. They are
now labelled rather than being indistinguishable from real readings —
which is the entire point of the layer.

### `gear = 10` — new, unexplained, and previously invisible

The third label was not predicted by anyone, and it is the most
interesting thing in this drive.

- All 63 clipped rows hold **exactly the value 10**, on
  `transmission.gear`. The mapping declares `valid_min: 1, valid_max: 8`,
  so 10 falls outside the range and is labelled.
- It occurs in **3 clusters** across the drive, at **low speed (≤8 km/h)**
  and idle rpm.
- **Value 10 has never been stored before**: zero occurrences in 90,239
  gear samples on 2026-08-29 and 5,729 on 08-30.

The reason it is new is the layer itself: the old decoder **dropped
out-of-range values silently**, so `gear = 10` was indistinguishable from
"not polled". The layer surfaced something real on its first drive that
had been invisible for the entire project.

Stated honestly:

- **Observed:** 63 samples of exactly 10, in 3 clusters, at ≤8 km/h.
- **Not claimed:** what 10 means. A shift transient, a state code, and a
  decode artifact are all live explanations and this data does not
  separate them.
- **Worth noting only as a direction:** the project has never found a
  clean **P/R/N/D selector** (`n47-next-session.md`, still open), and a
  value outside the drive-gear range appearing only at very low speed is
  at least *consistent* with a selector or park/neutral code. That is a
  hypothesis to test, not a finding.

Added to the car-side capture list alongside the MAF and EGR items: the
next step is raw `22 DA2E` bytes while the value is 10.

**A guess I got wrong, recorded because it was wrong:** I first assumed
these were `0xFF` at standstill being clipped. They are not — the stored
value is 10, and if `0xFF` were being clipped the value would be 255,
because the layer keeps the decoded number rather than discarding it.
Caught by F10-VM reading the lake.

## Run structure — 4 runs, all link faults

| run | samples | span (UTC) | duration | ended by |
|---|---|---|---|---|
| 1 | 17,751 | 18:24:59–18:30:37 | 338s | `TimeoutError` |
| **2** | **78,444** | **18:30:42–19:05:16** | **2073s** | `HsfzError: gateway closed the connection` |
| 3 | 2,285 | 19:14:02–19:14:45 | 43s | `TimeoutError` |
| 4 | 2,940 | 19:14:50–19:15:47 | 56s | end of drive |

**No split was a mode change or a clock step** — all four runs are
`mode=normal`, `clock_synced=1`, provenance populated (12 `run_mappings`
rows). That distinction is exactly what the run-boundary work exists to
make, and it held.

Run 2 alone carried 77% of the drive in one unbroken 34-minute stretch.
The `gateway closed the connection` at 19:05 is a different failure from
the timeouts — the ZGW dropping the session rather than a request going
unanswered — and it is the one the fault classifier deliberately does
*not* absorb, correctly.

## Faults: 15, and a new kind

    no_response         13
    transport_timeout    2

`no_response` on OBD PIDs is a kind I have not seen before. Four fired at
the same instant at 18:31:04Z, which looks like **one dropped multi-PID
exchange rather than four independent events** — worth confirming, since
it affects how error rates should be counted.

None caused a channel to be retired; nothing was resting at the end.

## Soot — third rate measurement, consistent

    soot      +0.95 g over 31.11 km / 50.7 min
    per km     0.0305 g/km   (drives 7-8: 0.028, drive 10: 0.0328)
    per hour   1.125 g/h     (drive 10: 1.255 g/h; session 9 idle: 0.249)

Consistent with the established picture: soot follows **fuel burnt, not
distance**. `regen_count` held at 93 throughout and `dist_since_regen`
ran 71.55 → 102.66 km with no reset, so no regeneration interfered.

## Known display defects — NOT new faults

- **`success_pct` renders 100.0 beside a non-zero failure count.** The
  floor fix is **half-applied**: `live.py:1417` (per-request) floors
  correctly and says why; `live.py:1507` (totals) still calls
  `round(100.0 * ok / sent, 1)`. This drive lands exactly in the gap —
  40952/40964 = 99.97%, which floors to 99.9 but rounds to 100.0. Still
  open on master. One-line change, not made here.
- **Totals do not balance.** `sent=40964, ok=40952, failed=8` leaves **4
  unaccounted**. In-flight at the sampling instant is a plausible
  explanation but it is an explanation, not a measurement. Recorded as
  unexplained.
- **Boost/MAP tiles blank above ~1.56 bar gauge** when MAP rails — this
  is expected and correct, not a fault.

## What was NOT checked

- **Neither raw-byte capture was taken.** The MAF `0x10` one needs
  ignition-on/engine-off; I flagged it before the crank and the window
  closed. The EGR `0x487A` one needs a deviation spike to coincide with
  someone looking. Both remain outstanding — **not attempted, not
  failed.**
- **EGR deviation peaked at 125.00%** this drive (0.00 last). The 144.43%
  outlier from drive 10 therefore was not a one-off, but I did not
  capture the raw bytes and cannot say whether it is physical or a decode
  artifact. Unresolved.
- The lake side was not verified from this host; F10-VM confirms from the
  VPS.
- No cold start here — run 2 opens at 67 °C. The generated report's
  warm-up section describes a warm restart and its 1.5 min to 80 °C is
  not comparable with session 9's 33.6 min from 21 °C.

## Envelope

174 km/h max, 3971 rpm, boost to 1.55 bar, rail to 1758 bar, MAF to
222.22 g/s (the known artifact ceiling), DPF ΔP −11.0 to 96.9 hPa,
coolant 45 → 93 °C.
