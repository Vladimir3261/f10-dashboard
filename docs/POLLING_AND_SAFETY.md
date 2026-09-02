# Polling strategy, drive modes, and doing no harm

What we poll, how often, why, and what is still unproven about the
safety of doing it. Started as raw notes on 2026-08-30; the polling and
mode sections are now decisions that have shipped, and the safety
section is narrower than it was — the drain question went away with the
powerbank, and what is left is small.

---

## 1. Can we interrupt car operations, or do long-term harm?

**Mostly settled, and the biggest part of it by deployment rather than by
measurement.** The honest answer is *"probably not, we have reasons to
think so, and the one path that could have done real harm no longer
exists."*

### What is already true (checked)

- **The service allowlist is observational.** Only OBD `0x01`/`0x09`,
  UDS `0x22`, the `0x2C` define/clear/read subfunctions, `0x19` and `0x3E`
  can be sent; the validation tool aborts on anything else at a single
  choke point. No write, no actuator, no coding. Drive modes do not
  widen this: every mode polls a subset of the requests the mappings
  already declare.
- **We send no tester-present.** Grepped: nothing in `live.py` or
  `bmwdiag` emits `0x3E`. So we are not deliberately holding a
  diagnostic session open or keeping ECUs from sleeping.
- **The ENET link is physically separate** from the vehicle buses. We
  talk to the ZGW over Ethernet; it does the routing.

### What is NOT established

- **`0x2C` is not purely a read.** "Dynamically define data identifier"
  *writes ECU state*: it reconfigures what `F303` points at. It is
  session-scoped and every F303 read re-arms it, so it should not
  persist — but calling the runtime strictly read-only is imprecise.
  Worth stating accurately rather than comfortably.
- **~~Battery drain when parked.~~ MOOT for this deployment
  (2026-08-30).** This was the item everything else waited on. It no
  longer applies: the Pi runs from its own **powerbank**, not the car,
  and is powered down by hand before the car is locked. Nothing polls a
  parked car, so there is no drain path to measure.

  Two things follow. The overnight `voltage` test is cancelled rather
  than deferred — it would measure nothing. And the question would come
  straight back if the Pi were ever wired to vehicle power, so this
  paragraph is a condition, not a closure.
- **Gateway/bus load.** Now much lower (see §2), but the drive-7
  timeouts are still unexplained. Per-request faults are recorded to
  `telemetry.channel_errors`, so the next drives answer this with data
  rather than speculation.
- **Wear.** No evidence either way that sustained diagnostic polling has
  any long-term effect. Probably none. Not established.

### How to settle what remains

The drain question is gone with the powerbank. What is left is smaller:

1. Compare fault rates across modes, now that per-request faults are
   recorded — a sleeping EGS and a loaded gateway look different, and
   the Car link tab shows both.
2. Read what the DDE reports about its own sleep state, if such a
   channel exists in the SGBD tables.

`off` mode keeps its value even so: it is how you leave the runtime up
and the link connected while sending nothing, which is the right state
for a parked car if the Pi is ever left running.

---

## 2. Poll rates — **done** (2026-08-30, OBD mapping v2)

Sampling now follows the physics of the channel rather than one global
loop rate.

### Why

From `docs/CHANNEL_CENSUS.md` (3 days, 1.73M rows): 11 OBD channels at
10 Hz produced **83% of all storage** at 0.1–3.8% distinct values, while
the proprietary DDE channels at ~0.09 Hz carried 20–75% distinct in
~1,100 rows each. The information was in the slow channels; the volume
was in the fast ones. Those eleven were fast because the original
hand-written dashboard polled them that way, not because anything about
them moves that fast.

**Nothing is lost by desynchronising them.** Channels do not need a
common cadence to be correlated: ClickHouse joins on nearest timestamp
(`ASOF JOIN`), so `coolant` at 0.1 Hz and `rpm` at 10 Hz still line up.

### The tiers

| class | period | channels | why |
|---|---|---|---|
| `motion` | 0.1 s | rpm, speed, map, pedal | genuinely fast; the basis of load context |
| `control_ctx` | 1 s | load, maf | conditions control-loop analysis, so it must be comparable against it — at 10 s these gave 19.2% coverage inside the 1 s alignment window, below the 50% the contract calls usable |
| `context` | 10 s | throttle, relthr, torque, rail, lambda | characterises a driving phase, not a single transient |
| `slow` | 10 s | coolant, oil, iat, voltage, fuelrate, cattemp, egr, egrerr | thermal mass and electrics: minutes, not seconds |
| `rare` | 60 s | ambient, baro, fuel, runtime, distance | weather and counters: hours, or monotonic |
| `dde_dyn` | 0.5 s x 22 | the 22 proprietary DDE reads | round-robin: one per firing, so ~11 s per channel |
| `egs` | 0.5 s | engaged gear | was 0.25 s; the EGS is the ECU that sleeps |

`map` is the one channel kept fast for a display reason rather than a
physical one: the derived `boost` (`map - baro`) is the Drive view's
hero gauge, and at one reading per 10 s it reads as a broken instrument.

All periods are declared in **seconds** — the only unit the format has.
`hz`, `every` and `cycles` were retired on 2026-08-30 and are refused at
load time; see [`MAPPING_ARCHITECTURE.md`](MAPPING_ARCHITECTURE.md).

### The result

Measured over a simulated minute at the 10 Hz loop rate:

| | requests/min |
|---|---|
| before (v1 + EGS at 4 Hz + DDE) | 7,740 |
| after (`normal`) | 2,735 |
| after pair + medium tier (2026-09-01) | 2,854 |

**A 65% cut**, with no channel lost and no decode changed. `long` mode
takes it to 637/min (−92%).

### Still open

- Does anything actually need 10 Hz? `rpm` was 3.8% distinct even at
  10 Hz, which suggests even it is oversampled. Deliberately left as-is
  for now: `motion` is the tier that would show a transient, and the
  storage argument against it is much weaker now that it is four
  channels rather than eleven.
- The DDE round-robin is unchanged. It is already the cheapest tier
  per channel and the most information-dense.

---

## 3. Drive modes — **done** (2026-08-30)

A mode scales the polling classes the mappings declare. It is **not** a
second scheduler: there stays exactly one mechanism deciding when a
request is due (`bmwdiag/mapping/modes.py`, applied in `polling.py`).

| mode | what it does | requests/12 min |
|---|---|---|
| `off` | connected but silent — no request is sent | 0 |
| `sampling` | 120 s awake, 600 s asleep; slow tiers exempt | 5,531 |
| `long` | motorway cruising — motion at 2 Hz | 6,564 |
| `normal` | exactly what the mappings declare | 29,940 |
| `debug` | the pre-v2 behaviour, for investigating a problem | 85,560 |

Switch from the dashboard's `mode` chip, or start in one with
`./run_car.sh --mode long`.

### Decisions worth keeping

- **`normal` scales nothing, by definition.** It *is* the declared
  rates, so "what rate was this recorded at?" has one answer — the
  mapping version plus the mode — and not a third source of truth.
- **A switch rescales from the declared rates**, never from the current
  (already scaled) ones, so `debug → long → normal` returns exactly to
  normal rather than compounding multipliers.
- **A mode change starts a new run.** A run has exactly one sampling
  configuration; `sessions.mode` records it. This is the point that
  makes modes safe to use at all: a baseline built from `debug` data and
  one from `long` are not comparable, and without a recorded mode
  nothing would say so. Drives spanning a switch are reassembled from
  consecutive sessions.
- **The duty cycle never silences the slow tiers.** `sampling` exempts
  `slow`, `rare` and `dde_dyn`, because the events worth catching on a
  long drive — a thermal excursion, a regeneration — are exactly the
  ones that would start and finish inside a sleep window.
- **`sampling` is quieter than `long`**, which is not obvious and was
  measured rather than assumed: it silences the fast tiers entirely for
  ten minutes in twelve, where `long` merely slows them. They are
  different trades — full resolution in bursts vs. coarse resolution
  throughout — not just different amounts.
- **`off` is not the same as not running.** The link stays up and the
  process keeps recording nothing, which is what makes it the right tool
  for the parked-battery test in §1.

### Still open

- The duty-cycle window (120 s / 600 s) is a first guess, not a measured
  optimum.
- Mode is not yet chosen automatically. A speed- or trip-length-based
  auto-switch is plausible but would make the recorded mode depend on
  driving, which complicates exactly the comparisons modes exist to
  protect. Manual for now.

---

## Order the remaining work probably wants doing in

1. ~~Stage-1 data quality~~ — **done** (2026-08-31, proven on drive 11).
2. **Fault rates per mode** — free, since the drives record them now.
3. ~~A genuine cold start~~ — **captured** (session 9, 21 °C, one
   unbroken run; four independent temperature channels agreed within
   0.6 °C). The load-driven oil-lag signature still wants a cold start
   followed by driving.

*(The overnight battery test used to head this list. It is cancelled —
see §1: the Pi runs from a powerbank.)*


## Paired requests, and why ordering was not enough

Two channels of a control loop — an actual and its setpoint — are only
comparable if they were sampled close together. The staggered `dde_dyn`
class sends one member per firing, so where a pair lands in the rotation
decides whether the comparison means anything.

That used to be an accident. `n47d_boost_act` and `n47d_boost_set`
happened to land in adjacent slots (0.5 s apart, inside their 1 s
window); `n47d_rail_act` and `n47d_rail_set` landed three slots apart
(1.5 s), **outside** theirs — so rail act-vs-setpoint had ~0% usable
coverage. Neither outcome was chosen: both fell out of the order the
loader produced across files, and a reordering could have silently
swapped which pair worked.

A request may now declare a pair tag:

```yaml
polling: {class: dde_dyn, pair: rail}
```

Members sharing a tag occupy **one rotation slot** and go out in the same
firing — therefore under the same recorded timestamp, so the gap is zero
rather than merely small. The class still fires once per period, so
pairing costs no extra firings; it shortens the rotation by one slot per
pair, which raises that class's request rate in proportion. Measured:
+119 requests/min in `normal`, +4.4%.

`long` mode is where this mattered most and was least obvious. Its
`dde_dyn` multiplier of 2.0 doubles the class period, so even the
adjacent boost pair sat 1.0 s apart — right on its window edge, at 34.6%
coverage. Pairing fixes that too.

## The burst was measured and left alone

#19 also asks for phase spreading: several non-staggered classes share a
period, so they become due on the same instant — 26 logical requests at
every minute boundary against a baseline of 4.

Measured in the unit that matters, that burst is not what it looks like.
`MappingExecutor._run_obd` hands every OBD request due in a cycle to
`ObdSession.read`, which packs **six PIDs into one Mode 01 exchange**. The
26-request cycle is **seven physical exchanges**; batching had already
absorbed it.

Per-request phase spreading was implemented, measured and removed. It
trades a rare worst cycle of 7 exchanges for 4, and pays by breaking
batches that were free:

| mode | exchanges/min | worst cycle (physical) |
|---|---|---|
| `normal` | 870 → 852 | 7 → 4 |
| `long` | 226 → **286 (+26.8%)** | 7 → 3 |
| `sampling` | 289 → **319 (+10.2%)** | 7 → 5 |

`long` exists to reduce link load on a motorway drive. Phasing would add
a quarter to its wire traffic to save three exchanges on a cycle that
happens once a minute. The rationale sits in `polling.py` next to where
the code would have gone, so it is not re-litigated from the logical
count.

**Logical requests are not wire exchanges.** Any future scheduling change
must be judged in exchanges; `tests/test_polling_pairs.py` encodes the
6-PID rule so that accounting is available without a car.

## Acquisition timestamps

Requests in one cycle are executed **sequentially**, so they do not share
an instant. The recorder used to stamp every value in a cycle with one
`time.time()`, which was harmless while the staggered class sent one
member per firing.

Pair slots broke that assumption: two independent ECU reads landing in
one cycle would both inherit the cycle timestamp, and the alignment
matcher would report a gap of exactly zero however far apart the two
exchanges really were. That is measuring the recorder, not the car — and
for a project whose alignment contract exists to prevent plausible
misaligned conclusions, it would have been the worst possible way to
"prove" pairing works.

Each response now carries its own completion time (`DecodedResponse.at`),
and the recorder stores it per signal. Derived channels have no exchange
of their own and keep the cycle timestamp.

So "the pair is scheduled in one firing" is a static, proven result. The
physical separation is one exchange, and **measuring it needs a car.**
