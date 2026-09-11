# Chassis / manoeuvre data on F10-520d-dev: a read-only scan, and what is not there

**Date:** 2026-09-11 · **Vehicle:** `F10-520d-dev` · **Link:** ENET/HSFZ via
the in-car Pi · **Services used:** UDS `0x22` (read), `0x3E` (TesterPresent),
OBD mode `0x01`. No write, no control, no session change (`0x10` was never
sent). `live.py` was stopped throughout — the ZGW serves one HSFZ client.

## Question

Can this car supply manoeuvre data — wheel speeds, steering angle, yaw rate,
lateral/longitudinal acceleration — over the diagnostic link, to support
dead-reckoning navigation (the odometer API, #49/#50, has no heading)?

**Answer: no, not through static UDS reads.** One useful channel was found
(brake pedal); every dynamics hypothesis died against evidence. Details and
the single untried door below.

## Method

1. TesterPresent sweep `0x00`–`0xFF` (`tools/egs.py find`).
2. `sparse` per ECU: 4 sample DIDs per 256-DID block, to find populated blocks.
3. `scan` of the promising blocks: every DID, keep those that answer.
4. Log candidates over a **32.7 min drive** (0–153 km/h) beside OBD speed.
5. Physical-cause tests, car stationary, engine running: steering lock-to-lock,
   indicators left/right/hazard, brake pedal held ~8 s.

Everything below is `wire_observation` on this car unless marked otherwise.

## What answers on this bus

**19 ECUs** answered TesterPresent: `0x00 0x01 0x02 0x10 0x12 0x17 0x18 0x29
0x2A 0x40 0x56 0x5E 0x60 0x63 0x64 0x67 0x72 0x78 0x86`. The runtime reports
`other_ecus: []` only because discovery stops at the engine.

**No ECU on this car answers a name DID.** `F197`/`F187`/`F193`/`F195` are
silent on all 19; only `F18C` (serial) returns. So ECUs cannot be identified
by asking them — identity must come from behaviour. This extends the DDE
finding already in `CLAUDE.md` to the whole bus.

Incidental: several modules report a blank VIN, and **one module reports a
different VIN from this car** — consistent with a used replacement part.
(Values live only in gitignored `local/chassis-scan/`.)

## The one positive result

**Brake pedal — ECU `0x72`, DID `0xD577`, 4 bytes.**

| state | payload |
|---|---|
| released | `00 00 00 00` |
| pressed | `00 01 00 01` |

Identified by cause and effect: the transition to `00010001` lasted
**8.1 s** against a requested ~8 s hold, with the car stationary and nothing
else touched. Confidence: high, but **one press** — not yet a validated
channel, and no candidate mapping is written from it here.

## What is NOT reachable (the point of this report)

- **`0x29` — the classic DSC address — has no live-data mechanism.** It
  answers, and holds ~24 readable DIDs across blocks `0x10/0x20/0x30/0x40/0x50`,
  all configuration-shaped. Blocks `0xF3xx`, `0xDAxx` and `0xD0xx` return
  **zero** DIDs, so there is no dynamic-define path and no BMW live-data block.
- **`0x86`'s changing block is analog drift, not dynamics.** `0x4041`
  (16 bytes) returned 769 distinct values across 788 drive samples, and its
  four trailing u16 fields looked exactly like four wheel speeds. They are
  not: the same fields read ~735 both parked and at 150 km/h, and drift
  smoothly. Temperatures or voltages.
- **Steering angle: absent.** Two independent stationary lock-to-lock tests
  across `0x29`, `0x86` and all 54 live-block DIDs of `0x72`. Nothing moved.
- **Indicators: absent** from the same 54.
- **Wheel speeds / yaw / lateral acceleration: not found anywhere.**

Of `0x72`'s 52 short live-block DIDs, exactly **six** responded to any physical
input during testing, and five of those moved only at the end of a block in a
way no single action explains; only `0xD577` mapped cleanly to a cause.

**Interpretation (`inference`, not observation):** on F-series these signals
are exchanged cyclically between DSC, ICM and SZL on FlexRay/K-CAN. A UDS read
returns only what an ECU chooses to publish as a measurement DID, and these
modules publish configuration, not dynamics. Nothing here proves the data is
absent from the vehicle — only that it is not exposed to a diagnostic reader.

## Inventory (for whoever tries next)

- `0x72` — 19 populated blocks; **59 DIDs in `0xD5xx`/`0xDDxx`, 52 of them ≤4 bytes**:
  `D531 D533 D537 D539 D53D D540 D541 D547 D54B D54C D54D D551 D552 D554 D557
  D559 D55B D55D D561 D562 D563 D565 D566 D568 D56A D56C D573 D574 D575 D577
  D579 D57B D57C D57E D57F D580 D581 D582 D583 D584 D586 D58B D58C D594 D5BE
  D5E5 D5E6 DD40 DD41 DD42 DD43 DD45`
- `0x86` — 14 blocks; `0xDAxx` holds only `DA00` (1 byte) and `DA0F` (2 bytes).
- `0x40` — 11 blocks, **including `0xF3xx`** — see below.
- `0x17`, `0x2A`, `0x56`, `0x67`, `0x78` — populated, nothing live found.

## The only untried door

**`0x40` exposes an `0xF3xx` block** — the same dynamically-defined-DID
mechanism the DDE's live data reaches us through (`2C 03 F3 03` / `2C 01 …`,
see `mappings/candidates/bmw/dde/n47/`). That is where live data would live if
it is readable at all.

It cannot be tried by guessing: a dynamic define needs the ECU's internal
**measurement identifiers**, which are SGBD data. We have none for `0x40`, and
inventing them is forbidden (`docs/MAPPING_RESEARCH.md`). This is a research
task requiring a real source, not another parked afternoon.

## Cost, honestly

~50 min of engine idling plus a 33 min drive, for one channel and five
documented negatives. The negatives are the deliverable: they close a question
that would otherwise be re-opened every time dead reckoning comes up.

## Follow-ups

1. `tools/egs.py` (and `live.py`'s `find_link_local_ip`) shell out to
   `ifconfig`, which is **not installed on the Pi** — the tool exits with
   "no 169.254.x.x interface" and every invocation needs `--local-ip`.
   `CLAUDE.md` predicted this; it should use `ip addr` / pure Python.
2. `egs.py` is named for the gearbox but is a general read-only UDS explorer.
   If chassis work continues, rename to `tools/ecu_explore.py`.
3. A candidate mapping for `0x72 0xD577` (brake) once it is confirmed on a
   second occasion — one press is a lead, not a verification.
