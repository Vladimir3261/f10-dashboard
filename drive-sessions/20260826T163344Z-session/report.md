# Session report — run 5

- ECU: 0x12 (ECM-EngineControl)  (addr 18)
- Duration: 16.7 min, 103284 samples across 45 channels
- Started (UTC): 2026-08-26T16:14:49Z

## Key findings

- Cold start captured from 87.0 °C; coolant reached 80 °C in 0.0 min and stabilised near 92.0 °C. Oil and engine temp tracked it closely — a healthy warm-up with no lag anomaly.
- **OBD MAP saturates at 255 kPa**; under boost the DDE reads the true manifold pressure up to 267 kPa. The boost cross-check ⚠️ is OBD sensor saturation, NOT a decode error — above 255 kPa the DDE boost channel is the accurate one. (Exactly the 'generic OBD saturation' caveat the project set out to handle.)
- Ambient/baro cross-check differs by only 3.84 hPa on average — that is the standard OBD baro PID's 1 kPa integer quantisation, i.e. agreement within resolution, not a discrepancy.
- Lambda sat at the 2.0 sentinel for 5965/8397 samples (= 'no value', not a real λ of 2.0); exclude those from any AFR analysis.
- Boost closed-loop control tracked its setpoint to 240.6 mean deviation (max 863.0) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- Rail pressure closed-loop control tracked its setpoint to 212.8 mean deviation (max 910.7) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- DPF soot measured vs modelled agree to 0.009 g (range 1.63–1.94 g) — differential-pressure sensing is healthy; this is a baseline to trend soot-accumulation rate against.
- [CANDIDATE] DPF differential pressure -4.0–54.0 hPa — should read low warm-idle and rise with exhaust flow under load; a plausible spread validates the 0x44F8 scale. (Baseline for filter-restriction trending.)
- [CANDIDATE] Exhaust temp before DPF 167–326 °C, before catalyst 198–404 °C — should climb under load; pre-cat typically hotter than pre-DPF. Validates the exhaust-temp scales.
- [CANDIDATE] Distance since regen 49.7–63.2 km — should be a steady value increasing monotonically over the drive (unless a regen completes, resetting it).
- [CANDIDATE] EGR control deviation 0.0–0.0 % — should sit near 0 when the loop is happy; a persistent offset would flag EGR fouling. Baseline for EGR-health trending.
- [CANDIDATE] Operating-mode word took 1 distinct value(s) — bit 0x02 is the regeneration-active flag; a change mid-drive would mark a regeneration event.

## Cold-start warm-up

| channel | start | max | →80 °C | unit |
|---|---|---|---|---|
| coolant | 87.0 | 92.0 | 0s | °C |
| n47d_oil_temp | 87.1 | 92.2 | 1s | °C |
| n47d_engine_temp | 87.7 | 92.8 | 9s | °C |
| n47d_charge_air_temp | 35.6 | 48.9 | — | °C |

- When coolant reached 80 °C, oil was **87.1 °C** (oil lags coolant — the expected warm-up signature).

## Proprietary DDE vs standard OBD (live cross-check)

| quantity | pairs | mean |Δ| | max |Δ| | agree |
|---|---|---|---|---|
| coolant °C | 73 | 0.46 | 2.56 | ✅ |
| manifold/boost (hPa vs kPa×10) | 73 | 30.32 | 144.9 | ⚠️ |
| ambient (hPa vs kPa×10) | 73 | 3.84 | 11.0 | ⚠️ |

## Drive / load behaviour

- max speed 123.0 km/h; 6297 driving / 2100 idle samples (speed>3 km/h = driving).

| loop | pairs | mean |dev| | max |dev| |
|---|---|---|---|
| boost (act−set) | 61 | 240.6 | 863.0 |
| rail pressure (act−set) | 61 | 212.8 | 910.7 |

| channel | min | max | mean | p95 |
|---|---|---|---|---|
| rpm | 742.5 | 3658.5 | 1558.465 | 2481.5 |
| map | 106.0 | 255.0 | 147.965 | 255.0 |
| n47d_boost_act | 1062.9 | 2666.0 | 1499.149 | 2353.9 |
| n47d_rail_act | 288.1 | 1497.6 | 766.389 | 1402.1 |
| n47d_maf_per_cyl | 520.58 | 1409.76 | 776.68 | 1257.57 |
| n47d_pedal | 0.0 | 99.68 | 19.43 | 73.42 |
| load | 0.0 | 100.0 | 38.13 | 98.824 |
| speed | 0.0 | 123.0 | 58.301 | 106.0 |
| maf | 9.77 | 195.44 | 50.894 | 129.49 |
| rail | 239.2 | 1803.5 | 757.899 | 1364.3 |

## DPF

- soot measured: 1.63–1.94 g
- soot modelled: 1.64–1.95 g
- measured vs modelled mean |Δ|: 0.009 g (the two independent estimates should agree)

## Data quality / coverage

| channel | src | samples | max gap | pinned@max |
|---|---|---|---|---|
| ambient | OBD | 84 | 12.1s |  |
| baro | OBD | 84 | 12.1s | 73 |
| boost | DDE | 8397 | 0.4s |  |
| cattemp | OBD | 84 | 12.1s |  |
| coolant | OBD | 84 | 12.1s |  |
| distance | OBD | 84 | 12.1s | 24 |
| egr | OBD | 84 | 12.1s | 84 |
| egrerr | OBD | 84 | 12.1s | 84 |
| gear | DDE | 8397 | 0.4s |  |
| iat | OBD | 84 | 12.1s |  |
| lambda | OBD | 8397 | 0.4s | 5965 |
| load | OBD | 8397 | 0.4s |  |
| maf | OBD | 8397 | 0.4s |  |
| map | OBD | 8397 | 0.4s |  |
| n47d_ambient_press | DDE | 73 | 14.0s |  |
| n47d_boost_act | DDE | 73 | 14.0s |  |
| n47d_boost_set | DDE | 73 | 14.0s |  |
| n47d_charge_air_temp | DDE | 73 | 14.0s |  |
| n47d_converter_temp | DDE | 73 | 14.0s |  |
| n47d_coolant | DDE | 73 | 14.0s |  |
| n47d_dist_since_regen | DDE | 73 | 14.0s |  |
| n47d_dpf_dp | DDE | 74 | 14.0s |  |
| n47d_egr_deviation | DDE | 73 | 13.9s | 73 |
| n47d_engine_temp | DDE | 73 | 14.0s |  |
| n47d_exh_temp_pre_cat | DDE | 73 | 14.0s |  |
| n47d_exh_temp_pre_dpf | DDE | 73 | 13.9s |  |
| n47d_gbx_oil_temp | DDE | 73 | 14.0s | 30 |
| n47d_maf_per_cyl | DDE | 73 | 14.0s |  |
| n47d_oil_temp | DDE | 73 | 14.0s |  |
| n47d_opmode | DDE | 73 | 14.0s | 73 |
| n47d_pedal | DDE | 73 | 14.0s |  |
| n47d_rail_act | DDE | 73 | 14.0s |  |
| n47d_rail_set | DDE | 73 | 14.0s |  |
| n47d_regen_count | DDE | 73 | 14.0s | 73 |
| n47d_soot_meas | DDE | 73 | 13.9s | 17 |
| n47d_soot_model | DDE | 73 | 14.0s |  |
| n47d_turbine_speed | DDE | 73 | 14.0s |  |
| pedal | OBD | 8397 | 0.4s |  |
| rail | OBD | 8397 | 0.4s |  |
| relthr | OBD | 8397 | 0.4s |  |
| rpm | OBD | 8397 | 0.4s |  |
| runtime | OBD | 84 | 12.1s |  |
| speed | OBD | 8397 | 0.4s |  |
| throttle | OBD | 8397 | 0.4s |  |
| voltage | OBD | 84 | 12.1s |  |

---
_Read-only analysis; no baselines across sessions claimed yet._
