# Session report — run 2

- ECU: 0x12 (ECM-EngineControl)  (addr 18)
- Duration: 20.7 min, 147786 samples across 46 channels
- Started (UTC): 2026-08-28T18:20:16Z

## Key findings

- Cold start captured from 87.0 °C; coolant reached 80 °C in 0.0 min and stabilised near 98.0 °C. Oil and engine temp tracked it closely — a healthy warm-up with no lag anomaly.
- **OBD MAP saturates at 255 kPa**; under boost the DDE reads the true manifold pressure up to 272 kPa. The boost cross-check ⚠️ is OBD sensor saturation, NOT a decode error — above 255 kPa the DDE boost channel is the accurate one. (Exactly the 'generic OBD saturation' caveat the project set out to handle.)
- Ambient/baro cross-check differs by only 6.41 hPa on average — that is the standard OBD baro PID's 1 kPa integer quantisation, i.e. agreement within resolution, not a discrepancy.
- Lambda sat at the 2.0 sentinel for 8683/11111 samples (= 'no value', not a real λ of 2.0); exclude those from any AFR analysis.
- Boost closed-loop control tracked its setpoint to 155.9 mean deviation (max 1139.9) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- Rail pressure closed-loop control tracked its setpoint to 159.2 mean deviation (max 1063.4) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- DPF soot measured vs modelled agree to 0.009 g (range 6.67–7.06 g) — differential-pressure sensing is healthy; this is a baseline to trend soot-accumulation rate against.
- [CANDIDATE] DPF differential pressure -10.0–107.0 hPa — should read low warm-idle and rise with exhaust flow under load; a plausible spread validates the 0x44F8 scale. (Baseline for filter-restriction trending.)
- [CANDIDATE] Exhaust temp before DPF 154–400 °C, before catalyst 194–537 °C — should climb under load; pre-cat typically hotter than pre-DPF. Validates the exhaust-temp scales.
- [CANDIDATE] Distance since regen 205.3–220.5 km — should be a steady value increasing monotonically over the drive (unless a regen completes, resetting it).
- [CANDIDATE] EGR control deviation 0.0–6.7 % — should sit near 0 when the loop is happy; a persistent offset would flag EGR fouling. Baseline for EGR-health trending.
- [CANDIDATE] Operating-mode word took 1 distinct value(s) — bit 0x02 is the regeneration-active flag; a change mid-drive would mark a regeneration event.

## Cold-start warm-up

| channel | start | max | →80 °C | unit |
|---|---|---|---|---|
| coolant | 87.0 | 98.0 | 0s | °C |
| n47d_oil_temp | 87.3 | 98.4 | 1s | °C |
| n47d_engine_temp | 87.3 | 98.2 | 8s | °C |
| n47d_charge_air_temp | 32.2 | 53.1 | — | °C |

- When coolant reached 80 °C, oil was **87.3 °C** (oil lags coolant — the expected warm-up signature).

## Proprietary DDE vs standard OBD (live cross-check)

| quantity | pairs | mean |Δ| | max |Δ| | agree |
|---|---|---|---|---|
| coolant °C | 97 | 0.51 | 1.66 | ✅ |
| manifold/boost (hPa vs kPa×10) | 97 | 22.87 | 172.9 | ⚠️ |
| ambient (hPa vs kPa×10) | 96 | 6.41 | 11.0 | ⚠️ |

## Drive / load behaviour

- max speed 198.0 km/h; 7615 driving / 3496 idle samples (speed>3 km/h = driving).

| loop | pairs | mean |dev| | max |dev| |
|---|---|---|---|
| boost (act−set) | 97 | 155.9 | 1139.9 |
| rail pressure (act−set) | 97 | 159.2 | 1063.4 |

| channel | min | max | mean | p95 |
|---|---|---|---|---|
| rpm | 730.5 | 3766.5 | 1383.544 | 2875.0 |
| map | 100.0 | 255.0 | 133.481 | 255.0 |
| n47d_boost_act | 1007.9 | 2722.9 | 1308.197 | 2588.0 |
| n47d_rail_act | 268.2 | 1680.8 | 620.146 | 1471.8 |
| n47d_maf_per_cyl | 246.87 | 1374.95 | 647.608 | 1288.96 |
| n47d_pedal | 0.0 | 100.01 | 16.41 | 85.64 |
| load | 0.0 | 100.0 | 33.966 | 98.824 |
| speed | 0.0 | 198.0 | 44.167 | 135.0 |
| maf | 0.0 | 203.55 | 42.453 | 154.74 |
| rail | 228.3 | 1790.4 | 586.149 | 1541.6 |

## DPF

- soot measured: 6.67–7.06 g
- soot modelled: 6.67–7.08 g
- measured vs modelled mean |Δ|: 0.009 g (the two independent estimates should agree)

## Data quality / coverage

| channel | src | samples | max gap | pinned@max |
|---|---|---|---|---|
| ambient | OBD | 112 | 11.3s | 47 |
| baro | OBD | 112 | 11.3s | 41 |
| boost | DDE | 11111 | 0.3s |  |
| cattemp | OBD | 112 | 11.3s |  |
| coolant | OBD | 112 | 11.3s |  |
| distance | OBD | 112 | 11.3s |  |
| egr | OBD | 112 | 11.3s |  |
| egrerr | OBD | 112 | 11.3s |  |
| egs_da2e_b0 | DDE | 11111 | 0.3s | 11111 |
| gear | DDE | 11111 | 0.3s |  |
| iat | OBD | 112 | 11.3s |  |
| lambda | OBD | 11111 | 0.3s | 8683 |
| load | OBD | 11111 | 0.3s |  |
| maf | OBD | 11111 | 0.3s |  |
| map | OBD | 11111 | 0.3s |  |
| n47d_ambient_press | DDE | 96 | 13.0s |  |
| n47d_boost_act | DDE | 97 | 13.0s |  |
| n47d_boost_set | DDE | 97 | 13.0s |  |
| n47d_charge_air_temp | DDE | 96 | 13.0s |  |
| n47d_converter_temp | DDE | 97 | 13.0s |  |
| n47d_coolant | DDE | 97 | 13.0s |  |
| n47d_dist_since_regen | DDE | 97 | 13.0s |  |
| n47d_dpf_dp | DDE | 97 | 13.0s |  |
| n47d_egr_deviation | DDE | 96 | 13.0s |  |
| n47d_engine_temp | DDE | 97 | 13.0s |  |
| n47d_exh_temp_pre_cat | DDE | 97 | 13.0s |  |
| n47d_exh_temp_pre_dpf | DDE | 97 | 13.0s |  |
| n47d_gbx_oil_temp | DDE | 97 | 13.0s |  |
| n47d_maf_per_cyl | DDE | 96 | 13.0s |  |
| n47d_oil_temp | DDE | 97 | 13.0s |  |
| n47d_opmode | DDE | 96 | 13.0s | 96 |
| n47d_pedal | DDE | 96 | 13.0s |  |
| n47d_rail_act | DDE | 97 | 13.0s |  |
| n47d_rail_set | DDE | 96 | 13.0s |  |
| n47d_regen_count | DDE | 96 | 13.0s | 96 |
| n47d_soot_meas | DDE | 97 | 13.0s |  |
| n47d_soot_model | DDE | 97 | 13.0s |  |
| n47d_turbine_speed | DDE | 97 | 13.0s |  |
| pedal | OBD | 11111 | 0.3s |  |
| rail | OBD | 11111 | 0.3s |  |
| relthr | OBD | 11111 | 0.3s |  |
| rpm | OBD | 11111 | 0.3s |  |
| runtime | OBD | 112 | 11.3s |  |
| speed | OBD | 11111 | 0.3s |  |
| throttle | OBD | 11111 | 0.3s |  |
| voltage | OBD | 112 | 11.3s |  |

---
_Read-only analysis; no baselines across sessions claimed yet._
