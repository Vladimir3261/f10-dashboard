# Drive 8 (2026-08-29, evening) — session notes

The first drive after the DPF regeneration, and the first hard one:
**204 km/h, 4014 rpm, 1.6 bar boost, 1764 bar rail, 213.7 g/s MAF**.
250,177 samples, all confirmed into the lake (`pending 0` at shutdown).

This directory covers **run 3** — 100,737 samples, 14.1 min, the main
continuous stretch.

## READ THIS FIRST — the clock jumped 76 minutes mid-recording

The Pi has **no RTC**. It booted with a stale clock reading 17:44 local,
started recording immediately, and `systemd-timesyncd` only reached a
time server 47 seconds later:

```
19:01:19 BST  Initial clock synchronization to Sat 2026-08-29 19:01:19
```

The real boot was **19:00:32**; the clock was corrected **forward by
~76.5 minutes** while run 1 was already logging.

Consequences, all confirmed rather than assumed:

- Run 1 contains exactly one gap of **4578.1s at 16:45:01 UTC**. That is
  the jump, **not** missing data.
- Run 1's stated duration (5064s) is fiction. Its real length is
  ~486s (~8 min), and its apparent 11 samples/s (against ~120/s for
  every other run) is the same artifact.
- The first ~18 seconds of run 1 carry timestamps **76 minutes in the
  past**. The session filename `drive-20260829T164441Z` is stamped from
  that same bad clock.
- **This went to ClickHouse with those timestamps.** Any query that
  trends by wall-clock time across this session will see a phantom
  76-minute idle. Runs 2–6 are correctly timestamped.

Runs 2–6 are clean and are what should be analysed. Run 1 should be
treated as suspect for anything time-derived (rates, gradients,
soot-per-km) — its *values* are fine, its *clock* is not.

### Worth fixing before it silently corrupts the baseline model

The whole point of this project is trend detection over months. A host
that can silently rewrite its own timeline is a direct threat to that.
Options, cheapest first:

1. Have `live.py` refuse to start recording until the clock is
   synchronised (`timedatectl show -p NTPSynchronized`), or stamp samples
   from a monotonic clock plus a single corrected epoch.
2. Record the sync state as a run-level flag so the lake can exclude
   pre-sync samples.
3. Fit a hardware RTC to the Pi (the real fix for a car that is often
   offline at boot).

## Fragmentation again — 6 runs

| run | samples | duration | note |
|---|---|---|---|
| 1 | 58,068 | ~8 min real | pre/post clock jump, timestamps suspect |
| 2 | 1,776 | 15s | |
| **3** | **100,737** | **848s** | **the main drive — this report** |
| 4 | 1,172 | 10s | |
| 5 | 10,099 | 84s | |
| 6 | 78,325 | 659s | second substantial stretch, worth its own report |

Same cause as drive 7 (transport failure against a secondary ECU tearing
down a healthy link — see `research/reports/n47-next-session.md` item 0).
**The fix is written but not applied**: it is sitting in `git stash` as
"wip: 0x18 fragmentation fix (incomplete)". It was stashed rather than
committed because it was mid-refactor and unverified.

## Post-regen DPF behaviour — the soot question sharpens

Drive 7 caught the regeneration (`regen_count` 92→93). This drive is the
clean-filter run after it, and it is the best soot evidence yet:

| | dist since regen | soot measured |
|---|---|---|
| drive 7 (after regen) | 9.05 → 19.64 km | 8.35 → 8.65 g |
| **drive 8** | **20.51 → 45.24 km** | **8.70 → 9.35 g** |

`regen_count` held at 93 and the opmode word stayed `0x100001` (regen bit
clear) throughout, so no second regeneration interfered.

Soot rises **smoothly and monotonically with distance**, ~**0.028 g/km**,
across both drives and straight through the regeneration boundary without
resetting. For scale it read 0.09 g on 2026-08-25.

- **Observed:** soot tracks distance travelled, continuously, and was not
  reset by a confirmed regeneration.
- **Inference:** these channels are not "soot currently stored in the
  filter" — a regen would have zeroed that.
- **Still open:** what they *are*. A cumulative-since-new counter should
  read far more than 9 g at 29,205 km, so if it is cumulative it has been
  reset at some point; a wrong scale/offset is equally possible.
- **The decisive test is the next regeneration.** If soot again fails to
  drop, "not current filter load" is settled and the candidate mapping's
  description needs correcting.

## DPF ΔP — the first proper clean-filter baseline

ΔP spanned **-4.03 to 116.96 hPa** (drive 7 peaked at 86.0), reached on a
much harder run. Negative readings at idle are the expected near-zero-flow
sensor offset.

This is the post-regen baseline the plan asked for, and it is the most
useful thing in this session for the long-term model — but it is still
opportunistic sampling, not the **steady-state load points** item 2 calls
for. A future drive holding 60–90s each at fixed speeds would turn this
into an actual ΔP-vs-flow curve.

## Caveats on the generated report

- **Not a cold start.** Run 3 opens at 88 °C, so the "Cold-start warm-up"
  section (`→80 °C in 0s`) is again an artifact. A genuine cold start
  remains uncaptured — five sessions now.
- **MAP ⚠️ is OBD saturation**, not a decode error: OBD pins at 255 kPa
  while the DDE reads to 279 kPa.
- **Ambient ⚠️ is quantisation** — 3.72 hPa mean, the baro PID's 1 kPa step.
- **Lambda is the 2.0 sentinel** for 5,368 of 7,574 samples.
- `n47d_egr_deviation` again read a flat 0.0 % for the entire run. Second
  session in a row; item 3 in the plan.
