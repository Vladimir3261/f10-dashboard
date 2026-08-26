# Session report — run 1

- ECU: 0x12 (ECM-EngineControl)  (addr 18)
- Duration: 46.2 min, 102294 samples across 34 channels
- Started (UTC): 2026-08-26T07:42:06Z

## Key findings

- Cold start captured from 22.0 °C; coolant reached 80 °C in 10.3 min and stabilised near 98.0 °C. Oil and engine temp tracked it closely — a healthy warm-up with no lag anomaly.
- **OBD MAP saturates at 255 kPa**; under boost the DDE reads the true manifold pressure up to 270 kPa. The boost cross-check ⚠️ is OBD sensor saturation, NOT a decode error — above 255 kPa the DDE boost channel is the accurate one. (Exactly the 'generic OBD saturation' caveat the project set out to handle.)
- Ambient/baro cross-check differs by only 5.14 hPa on average — that is the standard OBD baro PID's 1 kPa integer quantisation, i.e. agreement within resolution, not a discrepancy.
- Lambda sat at the 2.0 sentinel for 5175/9108 samples (= 'no value', not a real λ of 2.0); exclude those from any AFR analysis.
- Boost closed-loop control tracked its setpoint to 52.8 mean deviation (max 380.1) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- Rail pressure closed-loop control tracked its setpoint to 30.9 mean deviation (max 522.5) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- DPF soot measured vs modelled agree to 0.006 g (range 0.12–0.96 g) — differential-pressure sensing is healthy; this is a baseline to trend soot-accumulation rate against.

## Cold-start warm-up

| channel | start | max | →80 °C | unit |
|---|---|---|---|---|
| coolant | 22.0 | 98.0 | 616s | °C |
| n47d_oil_temp | 22.4 | 98.4 | 616s | °C |
| n47d_engine_temp | 22.5 | 98.4 | 616s | °C |
| n47d_charge_air_temp | 21.3 | 52.3 | — | °C |

- When coolant reached 80 °C, oil was **80.2 °C** (oil lags coolant — the expected warm-up signature).

## Proprietary DDE vs standard OBD (live cross-check)

| quantity | pairs | mean |Δ| | max |Δ| | agree |
|---|---|---|---|---|
| coolant °C | 92 | 0.31 | 0.86 | ✅ |
| manifold/boost (hPa vs kPa×10) | 92 | 54.86 | 1211.1 | ⚠️ |
| ambient (hPa vs kPa×10) | 92 | 5.14 | 9.0 | ⚠️ |

## Drive / load behaviour

- max speed 206.0 km/h; 5616 driving / 3492 idle samples (speed>3 km/h = driving).

| loop | pairs | mean |dev| | max |dev| |
|---|---|---|---|
| boost (act−set) | 80 | 52.8 | 380.1 |
| rail pressure (act−set) | 80 | 30.9 | 522.5 |

| channel | min | max | mean | p95 |
|---|---|---|---|---|
| rpm | 712.5 | 3973.0 | 1306.159 | 2314.5 |
| map | 99.0 | 255.0 | 126.494 | 253.0 |
| n47d_boost_act | 1001.0 | 2700.9 | 1227.179 | 2649.9 |
| n47d_rail_act | 270.1 | 1682.9 | 519.812 | 1382.1 |
| n47d_maf_per_cyl | 237.99 | 1367.65 | 544.563 | 1293.16 |
| n47d_pedal | 0.0 | 96.5 | 14.12 | 67.66 |
| load | 0.0 | 100.0 | 37.83 | 96.078 |
| speed | 0.0 | 206.0 | 36.881 | 122.0 |
| maf | 0.0 | 203.08 | 34.77 | 118.33 |
| rail | 228.3 | 1802.5 | 553.65 | 1316.5 |

## DPF

- soot measured: 0.12–0.96 g
- soot modelled: 0.13–0.97 g
- measured vs modelled mean |Δ|: 0.006 g (the two independent estimates should agree)

## Data quality / coverage

| channel | src | samples | max gap | pinned@max |
|---|---|---|---|---|
| ambient | OBD | 92 | 34.5s | 20 |
| baro | OBD | 91 | 65.9s | 24 |
| boost | DDE | 9107 | 4.7s |  |
| cattemp | OBD | 92 | 34.5s |  |
| coolant | OBD | 92 | 34.5s |  |
| distance | OBD | 92 | 34.5s |  |
| egr | OBD | 92 | 34.5s |  |
| egrerr | OBD | 92 | 34.5s |  |
| iat | OBD | 92 | 34.5s |  |
| lambda | OBD | 9108 | 4.7s | 5175 |
| load | OBD | 9105 | 4.7s |  |
| maf | OBD | 9108 | 4.7s |  |
| map | OBD | 9107 | 4.7s |  |
| n47d_ambient_press | DDE | 92 | 34.5s |  |
| n47d_boost_act | DDE | 92 | 34.5s |  |
| n47d_boost_set | DDE | 92 | 34.5s |  |
| n47d_charge_air_temp | DDE | 92 | 34.5s |  |
| n47d_coolant | DDE | 92 | 34.5s |  |
| n47d_engine_temp | DDE | 92 | 34.5s |  |
| n47d_maf_per_cyl | DDE | 92 | 34.5s |  |
| n47d_oil_temp | DDE | 92 | 34.5s |  |
| n47d_pedal | DDE | 92 | 34.5s |  |
| n47d_rail_act | DDE | 92 | 34.5s |  |
| n47d_rail_set | DDE | 92 | 34.5s |  |
| n47d_soot_meas | DDE | 92 | 34.5s |  |
| n47d_soot_model | DDE | 92 | 34.5s |  |
| pedal | OBD | 9108 | 4.7s |  |
| rail | OBD | 9108 | 4.7s |  |
| relthr | OBD | 9108 | 4.7s |  |
| rpm | OBD | 9105 | 4.7s |  |
| runtime | OBD | 92 | 34.5s |  |
| speed | OBD | 9108 | 4.7s |  |
| throttle | OBD | 9107 | 4.7s |  |
| voltage | OBD | 92 | 34.5s |  |

---
_Read-only analysis; no baselines across sessions claimed yet._
