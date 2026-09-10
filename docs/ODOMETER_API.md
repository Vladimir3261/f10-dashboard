# The odometer API (issue #49)

A separate navigation client (an Android app that dead-reckons without
GPS) needs exactly one thing from the car: **the current odometer value
in metres, with a monotonic guarantee**, plus the current speed to
interpolate between odometer samples. It manages its own navigation
sessions. The runtime's drive sessions, modes, share links and Basic
Auth are irrelevant to it, so it gets two paths of its own with their
own credential — and nothing else.

Status: **implemented 2026-09-10, not yet exercised on the car.** The
polling cost below is synthetic (the plan simulation in
`tests/test_polling_pairs.py`); the first drive with the class live
should be compared against it.

## Source

`n47.d72.dyn.44BF` in
`mappings/candidates/bmw/dde/n47/d72n47a0_dpf_egr.yaml` (v4): the DDE's
"distance since last successful regeneration", **u32 metres, 1 m/bit**,
monotonic between regenerations and reset to zero by a successful one.
Verified on the car as the km channel `n47d_dist_since_regen` (29.1 →
37.1 km over ~8 km driven on the validation drive). The API reads a
second signal on the same bytes, **`n47d_odometer_m`** (integer
metres), because the km channel is rounded to two decimals — 10 m
steps — and a decoded `Reading` carries no raw bytes to recover the
metre from.

The request lives in its own polling class, **`odometer`, 1 Hz** —
see [`POLLING_AND_SAFETY.md`](POLLING_AND_SAFETY.md) § "The odometer
class" for what that costs on the wire and why 1 Hz was kept.

Speed is SAE PID `0x0D` (`speed`, integer km/h, `motion` class, 10 Hz).

**Accuracy against true ground distance is UNKNOWN.** 44BF is what the
DDE counts for its regeneration strategy, from the vehicle-speed
signal; it has not been logged next to a GPS track. Until it has, a
client should treat the scale as nominal — 1 m/bit is what the mapping
says, not what a survey measured.

## The accumulator

`live.py` keeps an in-memory `odometer_m` that is **monotonic for the
life of the runtime process**:

- each accepted 44BF sample adds `raw - prev_raw` when that is ≥ 0;
- when `raw < prev_raw` — the ECU counter restarted at zero, which is
  what a successful regeneration looks like from here — it adds `raw`
  (the distance driven after the reset) and counts one `resets` event.
  The metres driven between the last sample and the reset are lost:
  one sample's worth at 1 Hz;
- a quality-flagged reading (`Reading.quality != "ok"`) is ignored,
  never accumulated;
- the first accepted sample of a process is `odometer_m = 0`.

`epoch` is a random id minted when `live.py` starts. It changes
**only** when the process restarts. An ECU reconnect keeps it — the
ECU's counter persists across the link, so the accumulator carries
straight on; if the ECU regenerated while the link was down, that shows
as one reset, not as a jump backwards.

## The contract

### `GET /api/odometer`

`Authorization: Bearer <token>` — answers with `Cache-Control: no-store`:

```json
{
  "t": 1789060085.784,
  "epoch": "9f3a1c6e",
  "odometer_m": 37104,
  "odometer_t": 1789060085.364,
  "speed_kmh": 47,
  "speed_t": 1789060085.751,
  "connected": true,
  "clock_synced": true,
  "resets": 0,
  "source": "n47d_odometer_m",
  "mapping_ver": 4
}
```

- `t`: server time the response was built (UTC epoch seconds).
  `odometer_t` / `speed_t`: acquisition time of the sample the value
  came from. Clients compute staleness as `t - odometer_t`, never from
  their own clock.
- `odometer_m` and `odometer_t` are `null` until the first accepted
  44BF sample of this epoch; `speed_kmh` / `speed_t` are `null` until
  the first speed sample. `connected: false` when the ECU link is down
  (values then stay at their last sample; the app decides).
- `clock_synced`: whether the host clock was NTP-synced for this run
  (`null` before the first connection). The Pi has no RTC; an unsynced
  `odometer_t` is still monotonic but not comparable to anything else.
- `resets`: how many ECU counter resets this epoch has bridged.
- `source` / `mapping_ver`: the channel and the mapping-file version it
  decoded through — the same provenance every recorded sample carries.
- Errors: **401** with `WWW-Authenticate: Bearer` for a missing,
  malformed, unknown or revoked token — one JSON body,
  `{"error": "unauthorized"}`, that names nothing; **503**
  `{"error": "odometer channel not loaded"}` when this run has no
  odometer channel (a bare `live.py`, or the DPF/EGR file not loaded —
  `./run_car.sh` loads it). Auth is checked first: an anonymous caller
  gets the 401, not the 503.

### `GET /api/odometer/stream`

Same credential, `text/event-stream`. One `data: {same JSON}` line
whenever a new 44BF or speed sample lands (or the link state changes),
**coalesced to at most 10 events per second** — a burst inside one
100 ms window becomes one event carrying the latest state — and a
keepalive comment line (`: keepalive`) after 15 s without an event.
The first event is the current state, sent immediately. Same 401/503.

### What is not there

No VIN, no run or session id, no history, no gateway or ECU identity —
nothing of the telemetry session, on either path. `POST` is not
served. **`/s/api/odometer` is not on the share allowlist**: a share
link opens the live view and nothing here, and a bearer token opens
these two paths and nothing else (not the dashboard, not the panel).

## Client rules

1. **Count deltas within one epoch only.** Distance travelled is
   `odometer_m(now) - odometer_m(then)` for two samples with the same
   `epoch`. When `epoch` changes, the runtime restarted and
   `odometer_m` restarted at 0: **re-base** — take the new value as the
   new origin, never subtract across epochs.
2. **Interpolate with speed between samples**, then clamp:
   `displayed = odometer_m + speed_mps * (t_now - odometer_t)` with
   `speed_mps = speed_kmh / 3.6`, and never let the displayed value go
   backwards (a new sample that lands below the interpolated value is
   caught up to, not jumped back to).
3. **A `speed_t` older than 1 s is unknown speed.** Speed is polled at
   10 Hz; a second without one means the link is stalling — stop
   interpolating and hold the last odometer sample.
4. **Watch `connected`.** While it is `false` the values are the last
   known and `odometer_t` stops advancing; on reconnect the same epoch
   continues.
5. **Judge staleness by `t - odometer_t`**, both server timestamps —
   the phone's clock need not agree with the Pi's.

## Drive modes

The `odometer` class runs at 1 Hz in **`normal`, `long` and `debug`**.
In **`sampling`** it is **not exempt** from the duty cycle — it sleeps
with the fast tiers for 600 s in every 720 — and in `off` it is silent,
like everything else. **Navigation needs `normal` or `long`.** A quiet
bus is what `sampling` is for, and continuity was not worth giving that
up; the mode chip on the dashboard says which one is running.

## Tokens

```bash
python3 tools/api_token.py mint android-nav      # prints the token ONCE
python3 tools/api_token.py list                  # names + dates, never tokens
python3 tools/api_token.py revoke android-nav
```

The store is `local/api-tokens.json` (gitignored, mode 0600; `live.py
--api-tokens <path>` to move it). `live.py` re-reads it whenever the
file changes, so a mint or a revoke takes effect without a restart.
Tokens are compared in constant time, never logged, and
`/api/diagnostics` reports only how many there are. A missing file
means no token is valid — the endpoint fails closed.

## Reaching it

- From anywhere, through the VPS (Case B in
  [`infra/NETWORK.md`](../infra/NETWORK.md)):
  `https://<DASHBOARD_DOMAIN>/api/odometer` with the bearer. nginx has
  `auth_basic off` on exactly these two paths (`location =`, so nothing
  else under `/api/` opens) and forwards `Authorization` as it does
  everywhere; the Pi's panel passes the two paths through without its
  login and forwards the header **only there**; `live.py` judges the
  token.
- On the car's own network: `http://<pi-lan-ip>:8088/api/odometer`
  (the panel's LAN listener) — plain HTTP, so the token travels in the
  clear there; the Pi's `:8080` is loopback-only.
- On a laptop running `./run_car.sh` directly: `http://localhost:8080/api/odometer`.

## Not in scope

Heading. The DSC wheel speeds and yaw rate are not in the research
pipeline; a separate issue if ever.
