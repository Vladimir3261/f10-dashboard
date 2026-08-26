# Session report — run 2

- ECU: 0x12 (ECM-EngineControl)  (addr 18)
- Duration: 16.1 min, 37566 samples across 41 channels
- Started (UTC): 2026-08-26T15:17:01Z

## Key findings

- Cold start captured from 36.0 °C; coolant reached 80 °C in 7.9 min and stabilised near 88.0 °C. Oil and engine temp tracked it closely — a healthy warm-up with no lag anomaly.
- Ambient/baro cross-check differs by only 6.64 hPa on average — that is the standard OBD baro PID's 1 kPa integer quantisation, i.e. agreement within resolution, not a discrepancy.
- Lambda sat at the 2.0 sentinel for 1436/3323 samples (= 'no value', not a real λ of 2.0); exclude those from any AFR analysis.
- Boost closed-loop control tracked its setpoint to 174.4 mean deviation (max 1195.9) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- Rail pressure closed-loop control tracked its setpoint to 233.0 mean deviation (max 962.8) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- DPF soot measured vs modelled agree to 0.008 g (range 0.96–1.24 g) — differential-pressure sensing is healthy; this is a baseline to trend soot-accumulation rate against.
- [CANDIDATE] DPF differential pressure -13.0–35.0 hPa — should read low warm-idle and rise with exhaust flow under load; a plausible spread validates the 0x44F8 scale. (Baseline for filter-restriction trending.)
- [CANDIDATE] Exhaust temp before DPF 28–244 °C, before catalyst 59–305 °C — should climb under load; pre-cat typically hotter than pre-DPF. Validates the exhaust-temp scales.
- [CANDIDATE] Distance since regen 29.1–35.5 km — should be a steady value increasing monotonically over the drive (unless a regen completes, resetting it).
- [CANDIDATE] EGR control deviation 0.0–51.3 % — should sit near 0 when the loop is happy; a persistent offset would flag EGR fouling. Baseline for EGR-health trending.
- [CANDIDATE] Operating-mode word took 1 distinct value(s) — bit 0x02 is the regeneration-active flag; a change mid-drive would mark a regeneration event.

## Cold-start warm-up

| channel | start | max | →80 °C | unit |
|---|---|---|---|---|
| coolant | 36.0 | 88.0 | 472s | °C |
| n47d_oil_temp | 36.2 | 88.2 | 474s | °C |
| n47d_engine_temp | 36.4 | 88.5 | 458s | °C |
| n47d_charge_air_temp | 28.8 | 44.9 | — | °C |

- When coolant reached 80 °C, oil was **82.9 °C** (oil lags coolant — the expected warm-up signature).

## Proprietary DDE vs standard OBD (live cross-check)

| quantity | pairs | mean |Δ| | max |Δ| | agree |
|---|---|---|---|---|
| coolant °C | 34 | 0.55 | 1.66 | ✅ |
| manifold/boost (hPa vs kPa×10) | 33 | 29.15 | 305.1 | ⚠️ |
| ambient (hPa vs kPa×10) | 33 | 6.64 | 7.0 | ⚠️ |

## Drive / load behaviour

- max speed 106.0 km/h; 2785 driving / 538 idle samples (speed>3 km/h = driving).

| loop | pairs | mean |dev| | max |dev| |
|---|---|---|---|
| boost (act−set) | 30 | 174.4 | 1195.9 |
| rail pressure (act−set) | 30 | 233.0 | 962.8 |

| channel | min | max | mean | p95 |
|---|---|---|---|---|
| rpm | 726.0 | 3300.0 | 1317.02 | 2027.5 |
| map | 99.0 | 255.0 | 119.081 | 184.0 |
| n47d_boost_act | 1000.0 | 2234.9 | 1162.47 | 1613.9 |
| n47d_rail_act | 280.1 | 1408.0 | 594.933 | 1260.6 |
| n47d_maf_per_cyl | 252.27 | 1294.55 | 495.11 | 819.97 |
| n47d_pedal | 0.0 | 62.72 | 11.232 | 61.98 |
| load | 0.0 | 100.0 | 42.022 | 92.549 |
| speed | 0.0 | 106.0 | 31.343 | 75.0 |
| maf | 0.0 | 159.94 | 30.233 | 81.44 |
| rail | 227.3 | 1593.2 | 553.629 | 1091.5 |

## DPF

- soot measured: 0.96–1.24 g
- soot modelled: 0.97–1.23 g
- measured vs modelled mean |Δ|: 0.008 g (the two independent estimates should agree)

## Data quality / coverage

| channel | src | samples | max gap | pinned@max |
|---|---|---|---|---|
| ambient | OBD | 34 | 33.3s |  |
| baro | OBD | 34 | 33.3s | 34 |
| boost | DDE | 3324 | 228.9s |  |
| cattemp | OBD | 34 | 33.3s |  |
| coolant | OBD | 34 | 33.3s |  |
| distance | OBD | 34 | 33.3s |  |
| egr | OBD | 34 | 33.3s |  |
| egrerr | OBD | 34 | 33.3s |  |
| iat | OBD | 34 | 33.3s |  |
| lambda | OBD | 3323 | 3.3s | 1436 |
| load | OBD | 3324 | 228.9s |  |
| maf | OBD | 3324 | 228.9s |  |
| map | OBD | 3324 | 228.9s |  |
| n47d_ambient_press | DDE | 33 | 32.2s | 21 |
| n47d_boost_act | DDE | 33 | 35.1s |  |
| n47d_boost_set | DDE | 33 | 35.1s |  |
| n47d_charge_air_temp | DDE | 33 | 35.1s |  |
| n47d_coolant | DDE | 34 | 35.1s |  |
| n47d_dist_since_regen | DDE | 33 | 35.1s |  |
| n47d_dpf_dp | DDE | 34 | 33.3s |  |
| n47d_egr_deviation | DDE | 33 | 32.2s |  |
| n47d_engine_temp | DDE | 33 | 35.1s |  |
| n47d_exh_temp_pre_cat | DDE | 33 | 35.1s |  |
| n47d_exh_temp_pre_dpf | DDE | 34 | 35.1s |  |
| n47d_maf_per_cyl | DDE | 33 | 35.1s |  |
| n47d_oil_temp | DDE | 34 | 34.3s |  |
| n47d_opmode | DDE | 33 | 35.1s | 33 |
| n47d_pedal | DDE | 33 | 32.2s |  |
| n47d_rail_act | DDE | 33 | 35.1s |  |
| n47d_rail_set | DDE | 33 | 35.1s |  |
| n47d_regen_count | DDE | 33 | 35.1s | 33 |
| n47d_soot_meas | DDE | 34 | 35.1s |  |
| n47d_soot_model | DDE | 33 | 35.1s |  |
| pedal | OBD | 3324 | 228.9s |  |
| rail | OBD | 3324 | 228.9s |  |
| relthr | OBD | 3323 | 3.3s |  |
| rpm | OBD | 3324 | 228.9s |  |
| runtime | OBD | 34 | 33.3s |  |
| speed | OBD | 3323 | 228.9s |  |
| throttle | OBD | 3324 | 228.9s |  |
| voltage | OBD | 34 | 33.3s |  |

---
_Read-only analysis; no baselines across sessions claimed yet._
