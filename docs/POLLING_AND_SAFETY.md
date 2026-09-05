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
| after pair + medium tier (2026-09-01) | 2,855 |

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
scheduler firing. The class still fires once per period, so pairing costs
no extra firings; it shortens the rotation by one slot per pair.

**That is a statement about scheduling, not about the wire.** The
executor runs the two requests sequentially, and each F303 member
normally re-arms its definition first — two setup frames plus the poll.
So a paired slot is *six* exchanges, and the two ECU reads are separated
by however long that takes. Measured cost with setup frames counted:
1,098 → 1,133 exchanges/min in `normal` (+3.2%), worst cycle 8 → 11.

The real separation is recorded, not assumed: each response carries its
own completion timestamp (below). **How large it is on a car is not
established here and needs supervised on-car validation.**

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
26-request cycle is **eleven physical exchanges**, not twenty-six;
batching absorbs the OBD half.

Per-request phase spreading was implemented, measured and removed. It
trades a rare worst cycle of 7 exchanges for 4, and pays by breaking
batches that were free:

| mode | exchanges/min | worst cycle (physical) |
|---|---|---|
| `normal` | 1,133 → 1,115 (−1.6%) | 11 → 8 |
| `long` | 357 → **418 (+16.9%)** | 11 → 7 |
| `sampling` | 552 → **582 (+5.3%)** | 11 → 9 |

Note where the worst cycle comes from: a paired `dde_dyn` slot is two
setup-plus-poll sequences — six exchanges — and phasing the OBD side does
not touch it. Spreading buys less than the logical count suggests *and*
costs continuously.

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

So "the pair is scheduled in one firing" is a static, proven result, and
it is the only claim this work supports on its own. The physical
separation is a setup-plus-poll sequence per member, its size is a
property of the car and the link, and **measuring it needs a car.** The
per-signal timestamps are what will make that measurable on the first
drive rather than assumed now.


## Response correlation, and what happens to a late answer

*(issue #12, 2026-09-05. Verified offline through a scripted socket and
clock; the on-car counts below are what a drive will show, not what one
has shown yet.)*

### The problem

`HsfzClient.request` used to accept the first frame from the right ECU
address whose first byte was the expected positive service id. It
correlated by **address and service**, nothing more. That is enough
while every request gets its answer before the next goes out, and wrong
the moment one does not: a `22 1234` that times out, followed by a
`22 5678`, could have its late `62 1234 …` handed back as the answer to
`22 5678`. The decoder caught the cases where the echoed identifier
differed — and labelled them `decode`, as if the mapping were wrong —
and could not catch the case where it does not differ. That case is the
one this car uses most: every d72 dynamic channel is read through the
same DID (`22 F3 03`), so a late answer under an old definition is
**byte-identical in shape** to the fresh answer under a new one, and
would have been decoded on the wrong scale with no fault anywhere.

Two further gaps: a `responsePending` (NRC `78`) extended the deadline
by 2 s every time it arrived, with no bound, so a stuck ECU could hold
the poll loop indefinitely; and the pre-request "drain" threw away raw
bytes, which could cut a half-received frame in two.

### The rule now

A frame is returned as the answer to a request only when **all** of
these hold:

1. it is a diagnostic frame from the expected ECU address to this tester;
2. its body fits the request's `ResponseExpectation`
   (`bmwdiag/protocol/correlate.py`): the positive service id **and** the
   echoed identifier — DID for `22`, PID for `01`/`09` (any one of a
   multi-PID list), sub-function + DID for `2C`, sub-function for
   `19`/`3E`, one byte for the KWP reads — at the declared minimum
   length; **or** it is `7F <this service> <nrc>`.

The expectation comes from the protocol's echo rule by default. A
mapping that declares `response.prefix` overrides it — that is how a
non-echoing protocol works: `dde7_kwp_local_id.yaml` declares
`prefix: "6C 10"`, so the transport accepts `6C 10 <data>` and does not
apply the `2C` rule on top. The executor hands the transport each
request's expectation labelled with the request id; the connect-time
profile probe does the same for the read it replays; setup frames and
ad-hoc probes get the structural rule. The seam
(`DiagnosticTransport.request(payload, *, dst, timeout, expect)`) stays
transport-agnostic — the expectation is plain data, no callables, so it
can travel to a C runtime unchanged.

Everything else from that ECU is an **orphan**. It is never returned.
It is counted, traced, and reported:

- if it fits the expectation of a request to that ECU that previously
  timed out, it is a **`late_response`**, recorded in `channel_errors`
  under *that* request's id and ticked onto its `late` counter in
  `/api/diagnostics` — the other half of the timeout that was already
  recorded;
- otherwise it is an **`unexpected_response`**, recorded under the
  pseudo request id `hsfz:0x<ecu>`.

A `7F` naming a *different* service is an orphan too: a refusal of
somebody else's request is not a refusal of this one.

### The bounds for what content cannot settle

Content correlation cannot tell apart two reads of the same identifier
— the F303 case, or any DID re-polled after its own timeout. Three
bounds cover that:

- **Settle window.** After a request to an ECU times out, the next
  request to that ECU is preceded by a listen: until the line has been
  quiet for `SETTLE_QUIET` (0.2 s), at most `SETTLE_MAX` (1.0 s).
  Whatever arrives is classified against the outstanding request, so
  its late answer is attributed and discarded *before* the next request
  is on the wire. Runs once per timeout. If the window passes with the
  late answer still unaccounted for and the new request could not be
  told apart from it by content, that is counted as an
  `ambiguous_resends` — the residual, made visible rather than silent.
- **Re-arm on fault.** A fault anywhere in a define-then-read sequence
  makes the executor forget that the DID is armed, so the next read of
  it re-sends the clear and define. Those are two exchanges the ECU
  answers in order, and they sit between the old poll and the new one:
  a late `62 F3 03` cannot arrive after `6C 03 F3 03` and `6C 01 F3 03`
  on an ECU that processes one request at a time.
- **Absolute pending deadline.** A `responsePending` extends the wait by
  `PENDING_EXTENSION` (2 s) but never past `PENDING_MAX_TOTAL` (5 s,
  ISO 14229's P2\*server) from the send. Past that the request fails as
  `HsfzPendingTimeout` — a `TimeoutError`, so to the executor it is one
  exchange that did not complete, skipped rather than reconnected — but
  recorded under its own kind, **`pending_timeout`**, so the lake can
  tell "the ECU kept saying wait and then went silent" from "the ECU
  said nothing" (`transport_timeout`). The answer that eventually
  arrives is a `late_response` like any other. A `7F xx 78` that
  arrives *late* — after its request timed out — is not that answer
  either: it is a promise of one, so the outstanding record is kept and
  its clock restarted, and the answer it announces is attributed when
  it comes.

An outstanding request is forgotten when its late answer shows up, when
the ECU answers a later request that could be told apart from it (it
answers in order, so it has moved past the old one — unless it had
promised the old answer with a `78`, in which case the record stays: a
long job can run beside short ones), when it is older than
`PENDING_MAX_TOTAL` (no request outlives that, so nothing arriving
later can be its answer; counted as `outstanding_expired`), or on
reconnect (a new TCP session carries nothing from the old one).

**The residuals, stated honestly.** Two cases remain that content
cannot settle and the bounds only narrow:

1. *Requests with a setup sequence* (the F303 reads): a late answer
   that arrives after the settle window *and* after the ECU has
   answered the two re-arm exchanges is assumed impossible on an
   in-order ECU. If this DDE reorders — nothing observed suggests it,
   and nothing proves it does not — `unexpected_response` and the
   per-request `late` counts are where it would first show.
2. *Requests with no setup* (EGS `22 DA2E`, OBD batches, static DIDs),
   re-polled after their own timeout: if the late answer lands after
   the 0.2 s quiet window and *during the next identical request's
   wait*, it is byte-for-byte what that request expects and **is
   returned as its answer** — a valid reading, possibly one period
   old. Nothing in the bytes can tell. What the code does about it:
   the transport **flags** that answer (`HsfzClient.last_answer_ambiguous`,
   counted as `ambiguous_answers`), the executor records the readings
   decoded from it with quality **`stale`** instead of `ok` — the
   number is kept bit-exact in SQLite and the lake, the display and
   the derived channels drop it, and the request's `ambiguous` count
   in `/api/diagnostics` ticks — and the answer that then arrives for
   the flagged request is attributed as a `late_response` ("answered
   ambiguously … ago") rather than returned to the request after it —
   the request after a flagged answer pays one more 0.2 s quiet window
   for that, and is itself never flagged by the kept record. So the
   one-period shift the residual used to cause is bounded to
   one flagged sample; it does not propagate. What is *not* fixed: the
   flag is a suspicion, not a proof — when the late answer never
   comes (the ECU answered once, late, and that was it) the flagged
   sample was in fact fresh, and it is dropped for nothing. That is
   the conservative side to be wrong on, and rare: it needs a timeout
   followed by an answer arriving in the 0.2–0.6 s after the settle
   window closed. A real fix would need a sequence number the
   protocol does not have.

### What to look at after a drive

`/api/diagnostics` → `transport`: `timeouts`, `pending_seen`,
`pending_exhausted`, `late_response`, `unexpected_response`,
`settle_runs`, `settle_caught`, `ambiguous_resends`,
`ambiguous_answers`, `outstanding_expired`, what is `outstanding`, the
bounds in force, and the last 48 frames on the wire with a note on each
orphan. The Car link tab shows the tally on the session card and a
`late` / `ambiguous` mark on any request that has one. In the lake,
`telemetry.channel_errors` gains the three new `kind` values
(`late_response`, `unexpected_response`, `pending_timeout`; the column
is a string; no migration) and `samples.quality` gains its first
`stale` rows (the label was in the enum from Stage 1, never written
until now).

What the counters mean, so nobody hunts for a fault that is not one:

- `unexpected_response` should stay at zero. A frame from an ECU that
  matches nothing asked of it is either reordering or a request this
  code did not send.
- `ambiguous_resends` is **not** a zero counter. It ticks every time
  an identifier is re-polled while its previous answer is still
  unaccounted for — the settle window closed without it — which is
  what every retry of a *silent* identifier looks like: three EGS
  attempts at key-on with the gearbox asleep are `settle_runs = 2,
  ambiguous_resends = 2` (synthetic, `tests/test_hsfz_correlation.py`
  `test_settle_cost_of_repolling_a_silent_identifier`). It measures
  exposure to residual 2, not damage. The damage counter is
  `ambiguous_answers`: an answer actually returned under that
  exposure, with its readings flagged `stale`.
- `late_response` tracks `timeouts` on a **slow** ECU — one that
  answers, late. A *sleeping* ECU never answers, so on an EGS that is
  asleep `late_response` stays at zero while `timeouts` climbs, and
  that pairing is equally expected.
- `outstanding_expired` counts timed-out requests found, at the next
  contact with that ECU, to be older than `PENDING_MAX_TOTAL` with
  their answer never seen. On a sleeping EGS that is one per attempt
  that follows a rest — and that attempt pays no settle window, since
  nothing can still be coming.

### What the settle window costs (synthetic, from the code)

Settle is keyed per ECU, so a DDE request after an EGS timeout pays
nothing (scripted: 0.02 s, the fake ECU's answer delay, and
`test_no_settle_is_paid_by_the_other_ecu` pins it). But the poll loop
is sequential: while the EGS settles nothing else is polled. With
`timeout: 0.4` on the `egs` class, the three attempts of a rest cycle
cost 0.40 / 0.60 / 0.60 s — the second and third each pay one 0.2 s
quiet window — so **1.6 s per rest cycle instead of 1.2 s**, before the
5 → 10 → 20 → 40 → 60 s rest. At the 60 s ceiling that is ~2.6 % of
loop time against ~2.0 % before this change, i.e. about two extra
`motion` samples lost per EGS attempt. Accepted: the alternative was a
silent one-period shift on every channel of that ECU.
