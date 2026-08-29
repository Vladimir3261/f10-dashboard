# Open threads: polling strategy, drive modes, and doing no harm

Raw notes from 2026-08-30, captured to pick up next session. Nothing here is
decided. Where a claim is checkable it has been checked and marked; the rest
is explicitly open.

---

## 1. Can we interrupt car operations, or do long-term harm?

The question to answer before increasing anything. Right now the honest
answer is *"probably not, and we have reasons to think so, but we have not
proven it."*

### What is already true (checked)

- **The service allowlist is observational.** Only OBD `0x01`/`0x09`,
  UDS `0x22`, the `0x2C` define/clear/read subfunctions, `0x19` and `0x3E`
  can be sent; the validation tool aborts on anything else at a single choke
  point. No write, no actuator, no coding.
- **We send no tester-present.** Grepped: nothing in `live.py` or `bmwdiag`
  emits `0x3E`. So we are not deliberately holding a diagnostic session open
  or keeping ECUs from sleeping. That removes the most obvious battery-drain
  mechanism — but see below, it does not clear it entirely.
- **The ENET link is physically separate** from the vehicle buses. We talk to
  the ZGW over Ethernet; it does the routing.

### What is NOT established

- **`0x2C` is not purely a read.** "Dynamically define data identifier"
  *writes ECU state*: it reconfigures what `F303` points at. It is
  session-scoped and every F303 read re-arms it, so it should not persist —
  but calling the runtime strictly read-only is imprecise. Worth stating
  accurately rather than comfortably.
- **Battery drain when parked.** The real long-term risk, and untested. If
  the Pi keeps the ENET link up and keeps polling with the ignition off,
  does the ZGW stay awake? Does that hold other modules awake? A car that
  will not sleep flattens its battery over days. **This is the single most
  important thing to measure**, and it is measurable: leave it connected
  overnight and watch `voltage` (already a channel).
- **Gateway/bus load.** We poll ~45 channels at up to 10 Hz. The gateway
  prioritises vehicle-function traffic over diagnostics, so we are probably
  being deprioritised rather than crowding anything out — but the timeouts
  from drive 7 are unexplained, and "we are loading the gateway" is a live
  hypothesis for them.
- **Wear.** No evidence either way that sustained diagnostic polling has any
  long-term effect. Probably none. Not established.

### How to settle it

1. Park connected overnight with logging on; watch `voltage` and whether the
   link stays up. That answers the drain question directly.
2. Compare timeout rates at 10 Hz versus a much slower poll — now possible,
   since faults are recorded per request (`telemetry.channel_errors`).
3. Read what the DDE reports about its own sleep state, if such a channel
   exists in the SGBD tables.

---

## 2. Poll most metrics far less often

Almost certainly right, and the census already shows it. Most channels change
far more slowly than we sample them.

From `docs/CHANNEL_CENSUS.md` (3 days, 1.73M rows):

- 11 OBD channels at 10 Hz produce **83% of all storage** at 0.1–3.8%
  distinct.
- The proprietary DDE channels at 0.09 Hz carry **20–75% distinct** in ~1,100
  rows each.

The information is in the slow channels; the volume is in the fast ones.

**The idea that makes this safe:** ClickHouse joins on nearest timestamp
anyway, so channels do not need a common cadence. `coolant` at 0.1 Hz and
`rpm` at 10 Hz still correlate fine — `ASOF JOIN` exists for exactly this.
Sampling rate should follow the *physics of the channel*, not a global loop
rate.

Rough first cut, to be argued with:

| kind of channel | suggested | why |
|---|---|---|
| rpm, speed, pedal, boost | 5–10 Hz | genuinely fast, and the basis of load context |
| gear | 4 Hz | shift takes 200–400 ms (already done) |
| temperatures (coolant, oil, IAT) | 0.1 Hz | thermal mass; minutes, not seconds |
| DPF soot, dist-since-regen, regen count | 0.02 Hz | change over km |
| barometric, ambient | 0.01 Hz | change over hours |

Open: does anything actually need 10 Hz, or is that inherited from the
original OBD dashboard rather than from an analytical requirement? `rpm` at
3.8% distinct suggests even it is oversampled.

---

## 3. Drive modes

Wanted: a mode switch, ideally from the dashboard, changing polling as a
whole rather than per channel.

Sketch, as described:

| mode | idea |
|---|---|
| **off** | log nothing; link idle |
| **debug** | what we do today — everything, fast. For investigating a specific problem |
| **normal** | adjusted intervals per the table above |
| **long drive** | much longer intervals; on cruise control the car can run for kilometres at one gear and speed, so most samples are redundant |
| **sampling** | duty-cycled: wake ~15 min, collect, sleep ~15 min, repeat. For a 5–6 hour drive. Interval illustrative, not fixed |

### Things to think through

- **Where does mode live?** The polling classes are already mapping data
  (`{hz: 4}`, `{every: 5, stagger: true}`). A mode could be a *multiplier* on
  the resolved classes rather than a second scheduling system — that keeps
  one mechanism instead of two.
- **Mode must be recorded.** A baseline built from `normal` and one from
  `debug` are not comparable unless the analysis knows which was in force.
  This wants a column on `sessions` (or samples), the same way `mapping_ver`
  was added — otherwise mode becomes an invisible confound in exactly the
  longitudinal comparisons the project exists for.
- **Sampling mode has a subtlety**: duty-cycling loses the *transitions*.
  A regeneration that starts and finishes inside a sleep window would be
  invisible — and the DPF work is precisely about catching those. Possibly
  slow channels stay always-on while fast ones duty-cycle.
- **Switching mid-drive** must not fragment the run, which is why the
  `execute.py` reconnect fix matters first.
- **`off` is not the same as not running.** Worth deciding whether it means
  "connected but silent" or "no link at all" — they differ for question 1.

---

## Order these probably want doing in

1. **The overnight battery test** — it gates how relaxed we can be about
   everything else, and costs one night.
2. **Per-channel rates** (#2) — mostly a mapping edit, immediate storage win,
   and it feeds the timeout question with real data.
3. **Modes** (#3) — the biggest design, and worth doing after #2 has settled
   what the sensible per-channel rates actually are.
