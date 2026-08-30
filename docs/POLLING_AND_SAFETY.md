# Polling strategy, drive modes, and doing no harm

What we poll, how often, why, and what is still unproven about the
safety of doing it. Started as raw notes on 2026-08-30; the polling and
mode sections are now decisions that have shipped, and the safety
section is still open.

---

## 1. Can we interrupt car operations, or do long-term harm?

**Still open.** The honest answer remains *"probably not, and we have
reasons to think so, but we have not proven it."*

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
- **Battery drain when parked.** The real long-term risk, and untested.
  If the Pi keeps the ENET link up and keeps polling with the ignition
  off, does the ZGW stay awake? Does that hold other modules awake? A
  car that will not sleep flattens its battery over days.
  **This is the single most important thing left to measure.**
- **Gateway/bus load.** Now much lower (see §2), but the drive-7
  timeouts are still unexplained. Per-request faults are recorded to
  `telemetry.channel_errors`, so the next drives answer this with data
  rather than speculation.
- **Wear.** No evidence either way that sustained diagnostic polling has
  any long-term effect. Probably none. Not established.

### How to settle it

1. **Park connected overnight in `off` mode**, watching `voltage` and
   whether the link stays up. `off` exists for exactly this: connected
   but silent, which isolates "does the cable keep the car awake?" from
   "does the polling keep the car awake?". Then repeat in `normal` to
   separate the two.
2. Compare fault rates across modes now that per-request faults are
   recorded — a sleeping EGS and a loaded gateway look different.
3. Read what the DDE reports about its own sleep state, if such a
   channel exists in the SGBD tables.

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

| class | rate | channels | why |
|---|---|---|---|
| `motion` | 10 Hz | rpm, speed, map, pedal | genuinely fast; the basis of load context |
| `context` | 1/10 s | load, throttle, relthr, torque, maf, rail, lambda | characterises a driving phase, not a single transient |
| `slow` | 1/10 s | coolant, oil, iat, voltage, fuelrate, cattemp, egr, egrerr | thermal mass and electrics: minutes, not seconds |
| `rare` | 1/60 s | ambient, baro, fuel, runtime, distance | weather and counters: hours, or monotonic |
| `dde_dyn` | ~1/11 s each | the 22 proprietary DDE reads | round-robin, one per 0.5 s |
| `egs` | 2 Hz | engaged gear | was 4 Hz; the EGS is the ECU that sleeps |

`map` is the one channel kept fast for a display reason rather than a
physical one: the derived `boost` (`map - baro`) is the Drive view's
hero gauge, and at 0.1 Hz it reads as a broken instrument.

### The result

Measured over a simulated minute at the 10 Hz loop rate:

| | requests/min |
|---|---|
| before (v1 + EGS at 4 Hz + DDE) | 7,740 |
| after (`normal`) | 2,735 |

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

1. **The overnight battery test** (§1) — it gates how relaxed we can be
   about everything else, and costs one night. `off` mode now makes it a
   clean experiment.
2. **Fault rates per mode** — free, since the next drives record them.
3. **Stage-1 data quality** — populate the `quality` column
   (saturated / sentinel / stale); nothing writes it today.
