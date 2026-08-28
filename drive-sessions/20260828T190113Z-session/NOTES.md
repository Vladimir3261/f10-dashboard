# Drive 6 (2026-08-28) — session notes

First drive logged from the **Raspberry Pi in-car host** instead of the
laptop, and the first with live sync to the ClickHouse lake running
during the drive. See `docs/PI_COMMISSIONING.md` for the host setup.

## The session is fragmented into 7 runs

`local/sessions/drive-20260828T181028Z.db` holds **324,977 rows in 7
runs**, not one. `session_report` analyses a single run, so two reports
were generated:

| dir | run | samples | duration | what it is |
|---|---|---|---|---|
| `20260828T190113Z-session/` | **2** | 147,786 | 20.7 min | **the main drive — read this one** |
| `20260828T185959Z-session/` | 7 | 55,266 | 7.7 min | the tail after the last reconnect |

Full run table (UTC):

| run | samples | start | end |
|---|---|---|---|
| 1 | 69,055 | 18:10:30 | 18:20:10 |
| 2 | 147,786 | 18:20:16 | 18:40:57 |
| 3 | 37,281 | 18:41:03 | 18:46:17 |
| 4 | 6,294 | 18:46:20 | 18:47:12 |
| 5 | 3,608 | 18:50:14 | 18:50:44 |
| 6 | 5,687 | 18:50:46 | 18:51:34 |
| 7 | 55,266 | 18:51:40 | 18:59:23 |

## Why it fragmented — the ENET cable lost carrier

Not a software or configuration fault. NetworkManager:

```
19:47:20 (local)  eth0: activated -> unavailable (reason 'carrier-changed')
19:50:06 (local)  eth0: carrier: link connected -> reactivated, same IP
```

The cable physically dropped carrier for **2 m 46 s**, which matches the
gap between run 4 (ends 18:47:12Z) and run 5 (starts 18:50:14Z) exactly.
With no IPv4 address on the interface every socket bind failed:
**82 × `OSError: [Errno 99] Cannot assign requested address`**, plus
5 `TimeoutError`, 1 `HsfzError` (gateway closed the connection) and
2 `HsfzNack: gateway will not route to 0x18` (the EGS gear read).

The short runs 4–6 are the transport reconnecting repeatedly as the link
came back. **Action: check the ENET cable's physical seating at the OBD
port before the next drive** — a Pi bouncing around a moving car is a new
mechanical stress the laptop setup never had.

Only run 7 has `ended_at` set, because only a clean SIGINT writes it.
Runs 1–6 are left open — the same root cause as the 32 open sessions in
ClickHouse noted in `docs/PI_COMMISSIONING.md` §6.

## The finding worth keeping — OBD MAP saturation

From the run 2 report:

> **OBD MAP saturates at 255 kPa**; under boost the DDE reads the true
> manifold pressure up to 272 kPa. The boost cross-check ⚠️ is OBD sensor
> saturation, **not** a decode error — above 255 kPa the DDE boost channel
> is the accurate one.

This is the single-byte SAE PID 0x0B hitting its ceiling, and it explains
the recurring ⚠️ on the boost cross-check line in every previous report.
It is a vindication of the proprietary channels: the DDE gives real data
where the generic OBD sensor simply runs out of range. Any future boost
analytics should prefer `n47d_boost_act` over `map` above 250 kPa.

## Data quality

- 46 channels, ~9 Hz, **0 dropped rows** across the whole session.
- Coolant cross-check DDE vs OBD: mean |Δ| 0.51 °C over 97 pairs ✅.
- Soot measured vs modelled agree to 0.009 g ✅.
- Lambda sat at the 2.0 sentinel for 8,683 / 11,111 samples — exclude
  from AFR analysis, as in every prior session.
- Max speed 131 km/h; gear stepped cleanly through to 8th.

## Trend against the previous session (2026-08-27)

| quantity | 2026-08-27 | 2026-08-28 |
|---|---|---|
| soot measured | 2.94–3.04 g | 6.67–7.22 g |
| distance since regen | 90.7–93.1 km | 205.3–227.6 km |

Consistent: ~130 km further with no regeneration, soot accumulating as
expected. Two comparable points is not yet a rate — but this is exactly
the longitudinal series the lake exists to build.

## Sync

Ran throughout. **324,977 / 324,977 rows shipped, 0 pending, no errors**,
~5 s lag, ~2.4 bytes/sample on the wire.
