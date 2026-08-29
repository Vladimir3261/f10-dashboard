# Drive 7 (2026-08-29) — session notes

First drive after the analytics VPS was replaced, and the first on the
in-car LTE router (`f10-rpi`) rather than a phone hotspot. Sync ran
healthy for the whole drive: **146,863 samples, all of them in the lake**
(`pending 0`, `synced_rowid == max_rowid` at shutdown).

## The session is fragmented into 4 runs

`local/sessions/drive-20260829T152727Z.db` holds 146,863 rows in **4
runs**. `session_report` analyses a single run, so this directory covers
**run 3 — the main drive**.

| run | samples | start (UTC) | end | duration | gap before |
|---|---|---|---|---|---|
| 1 | 22,412 | 15:27:29 | 15:30:37 | 188s | — |
| 2 | 10,862 | 15:30:43 | 15:32:13 | 91s | 5.5s |
| **3** | **103,701** | **15:32:19** | **15:46:49** | **870s** | 5.5s |
| 4 | 9,888 | 15:46:54 | 15:48:01 | 67s | 5.6s |

~16.7s lost to reconnects, **1.35% of wall time**. The raw cost is small;
the real cost is analytical — a drive that cannot be summarised in one
report without stitching.

### Why it fragments — a per-request failure tears down the whole link

Not the ENET cable this time: the kernel logged one `Link is Up` at
plug-in and no carrier flaps. All three breaks were `TimeoutError: timed
out`; earlier drives today also broke on `HsfzNack: gateway will not
route to 0x18` (the EGS).

The mechanism is in `bmwdiag/mapping/execute.py`, `_run_generic`. The
transport call sits **outside** the try/except that guards decoding:

```python
response = self.transport.request(
    bound.payload, dst=bound.dst, timeout=bound.timeout
)
```

Its comment states the intent — *"A transport failure here propagates
like any other: it belongs to the reconnect logic"* — which was right
when every request went to `0x12`. Now that a **secondary** ECU (EGS
`0x18`) is polled, a routing NACK or timeout for `0x18` says nothing
about the health of the `0x12` link, yet it propagates to the catch-all
`except Exception` in `live.py`'s poll loop, which closes the client,
sleeps 2s, re-discovers, and calls `rec.start_run()` → **a new run row**.

The codebase already has the right idiom next door: `ObdSession` retires
an unresponsive PID after 3 strikes instead of dying. **Not yet fixed** —
`bmwdiag` is dependency-free by design, so `HsfzNack` (defined in
`live.py`) cannot simply be caught there; it needs an unroutable-target
exception at the `bmwdiag/protocol/` seam. See
`research/reports/n47-next-session.md`.

## A DPF regeneration completed between drives

The clearest finding of the day, and it was **not** captured mid-drive —
it is visible as a step between sessions:

| | earlier today (13:45–14:53 UTC) | this drive (15:27+ UTC) |
|---|---|---|
| `n47d_regen_count` | 92 | **93** |
| `n47d_dist_since_regen` | 227.7 → 241.6 km | **9.05 → 19.64 km** |
| `n47d_soot_meas` | 7.34 → 7.90 g | **8.35 → 8.65 g** |

`regen_count` incrementing by one and `dist_since_regen` resetting to
near zero are exactly the signature of a completed regeneration. It
happened during or around the 14:57–15:24 UTC session.

**The soot channel did not behave as a regeneration would predict.** A
completed regen burns soot out, so `n47d_soot_meas` should have *dropped*;
instead it is ~0.5 g **higher** than before the event and still rising.
That is a genuine inconsistency with our current reading of the channel.

Stated honestly:

- **Observed:** `regen_count` 92 → 93, `dist_since_regen` reset, `soot_meas`
  rose across the same boundary.
- **Inference:** a regeneration completed. Two independent counters agree,
  so this is solid.
- **Hypothesis (untested):** `n47d_soot_meas` / `n47d_soot_model` are not
  "current soot load in the filter". They may be cumulative-since-new, an
  ash estimate, or carry an offset our candidate scale does not model.
  Both channels are `candidate`, and this is the first regeneration event
  we have straddled — the first real test of that scale, and it did not
  pass.
- **Not claimed:** that the scale is wrong. One event is not a validation.

Next step: query the lake across the regen boundary (the 14:57–15:24
session is in ClickHouse; its local DB was dropped) and see whether soot
stepped, drifted, or simply continued. `analysis/clickhouse/insights.sql`
is the place for it.

## Caveats on the generated report

- **Not a cold start.** Run 3 begins at 89 °C coolant — it is a
  continuation after a reconnect, not a key-on. The "Cold-start warm-up"
  section reports `→80 °C in 0s`, which is an artifact of that, not a
  thermostat finding. Drive 8 should capture a genuine cold start.
- **MAP ⚠️ is sensor saturation, not a decode error.** The OBD MAP PID
  pins at 255 kPa while the DDE reads to 271 kPa. Above 255 kPa the DDE
  channel is the accurate one — the known generic-OBD ceiling.
- **Ambient ⚠️ is quantisation.** 4.28 hPa mean difference is the OBD
  baro PID's 1 kPa integer step; agreement within resolution.
- **Lambda is mostly a sentinel.** 5,773 of 7,797 samples sit at exactly
  2.0, meaning "no value". Exclude them from any AFR work.
- DPF ΔP spanned 0.96–86.0 hPa across the load range; soot measured vs
  modelled agree to 0.008 g, so the ΔP sensing itself looks healthy.

## Infrastructure notes

- Sync stayed `synced` throughout; the LTE router gave 63 ms to the VPS
  (a phone hotspot earlier measured 1.7–2.8 s before it settled).
- Yesterday's session 8 (324,977 samples) is still **unverified** in the
  rebuilt lake — its sync watermark predates the rebuild, so it will not
  re-ship on its own. The Pi has no SSH key on the new VM, so
  `lake_status.sh` cannot run from here.
