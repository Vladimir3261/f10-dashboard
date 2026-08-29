# Session report — run 3

- ECU: 0x12 (ECM-EngineControl)  (addr 18)
- Duration: 14.5 min, 103701 samples across 46 channels
- Started (UTC): 2026-08-29T15:32:19Z

## Key findings

- Cold start captured from 89.0 °C; coolant reached 80 °C in 0.0 min and stabilised near 93.0 °C. Oil and engine temp tracked it closely — a healthy warm-up with no lag anomaly.
- **OBD MAP saturates at 255 kPa**; under boost the DDE reads the true manifold pressure up to 271 kPa. The boost cross-check ⚠️ is OBD sensor saturation, NOT a decode error — above 255 kPa the DDE boost channel is the accurate one. (Exactly the 'generic OBD saturation' caveat the project set out to handle.)
- Ambient/baro cross-check differs by only 4.28 hPa on average — that is the standard OBD baro PID's 1 kPa integer quantisation, i.e. agreement within resolution, not a discrepancy.
- Lambda sat at the 2.0 sentinel for 5773/7797 samples (= 'no value', not a real λ of 2.0); exclude those from any AFR analysis.
- Boost closed-loop control tracked its setpoint to 183.7 mean deviation (max 797.0) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- Rail pressure closed-loop control tracked its setpoint to 244.3 mean deviation (max 1162.0) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- DPF soot measured vs modelled agree to 0.008 g (range 8.38–8.65 g) — differential-pressure sensing is healthy; this is a baseline to trend soot-accumulation rate against.
- [CANDIDATE] DPF differential pressure 1.0–86.0 hPa — should read low warm-idle and rise with exhaust flow under load; a plausible spread validates the 0x44F8 scale. (Baseline for filter-restriction trending.)
- [CANDIDATE] Exhaust temp before DPF 180–289 °C, before catalyst 164–382 °C — should climb under load; pre-cat typically hotter than pre-DPF. Validates the exhaust-temp scales.
- [CANDIDATE] Distance since regen 10.0–19.6 km — should be a steady value increasing monotonically over the drive (unless a regen completes, resetting it).
- [CANDIDATE] EGR control deviation 0.0–0.0 % — should sit near 0 when the loop is happy; a persistent offset would flag EGR fouling. Baseline for EGR-health trending.
- [CANDIDATE] Operating-mode word took 1 distinct value(s) — bit 0x02 is the regeneration-active flag; a change mid-drive would mark a regeneration event.

## Cold-start warm-up

| channel | start | max | →80 °C | unit |
|---|---|---|---|---|
| coolant | 89.0 | 93.0 | 0s | °C |
| n47d_oil_temp | 89.7 | 93.6 | 1s | °C |
| n47d_engine_temp | 90.0 | 93.6 | 8s | °C |
| n47d_charge_air_temp | 52.1 | 56.6 | — | °C |

- When coolant reached 80 °C, oil was **89.7 °C** (oil lags coolant — the expected warm-up signature).

## Proprietary DDE vs standard OBD (live cross-check)

| quantity | pairs | mean |Δ| | max |Δ| | agree |
|---|---|---|---|---|
| coolant °C | 68 | 0.48 | 1.86 | ✅ |
| manifold/boost (hPa vs kPa×10) | 68 | 27.58 | 373.1 | ⚠️ |
| ambient (hPa vs kPa×10) | 67 | 4.28 | 8.0 | ⚠️ |

## Drive / load behaviour

- max speed 111.0 km/h; 6388 driving / 1409 idle samples (speed>3 km/h = driving).

| loop | pairs | mean |dev| | max |dev| |
|---|---|---|---|
| boost (act−set) | 66 | 183.7 | 797.0 |
| rail pressure (act−set) | 66 | 244.3 | 1162.0 |

| channel | min | max | mean | p95 |
|---|---|---|---|---|
| rpm | 740.0 | 4000.0 | 1425.809 | 2338.5 |
| map | 105.0 | 255.0 | 133.658 | 236.0 |
| n47d_boost_act | 1061.9 | 2712.9 | 1360.008 | 2377.9 |
| n47d_rail_act | 275.2 | 1746.6 | 667.002 | 1205.1 |
| n47d_maf_per_cyl | 471.48 | 1267.97 | 671.742 | 1225.66 |
| n47d_pedal | 0.0 | 76.66 | 15.22 | 55.48 |
| load | 0.0 | 100.0 | 32.993 | 98.824 |
| speed | 0.0 | 111.0 | 40.239 | 91.0 |
| maf | 8.99 | 205.36 | 40.762 | 106.99 |
| rail | 226.3 | 1827.3 | 650.842 | 1244.8 |

## DPF

- soot measured: 8.38–8.65 g
- soot modelled: 8.39–8.66 g
- measured vs modelled mean |Δ|: 0.008 g (the two independent estimates should agree)

## Data quality / coverage

| channel | src | samples | max gap | pinned@max |
|---|---|---|---|---|
| ambient | OBD | 78 | 11.3s | 52 |
| baro | OBD | 78 | 11.3s | 78 |
| boost | DDE | 7797 | 0.3s |  |
| cattemp | OBD | 78 | 11.3s |  |
| coolant | OBD | 78 | 11.3s |  |
| distance | OBD | 78 | 11.3s |  |
| egr | OBD | 78 | 11.3s |  |
| egrerr | OBD | 78 | 11.3s | 76 |
| egs_da2e_b0 | DDE | 7797 | 0.3s | 7797 |
| gear | DDE | 7797 | 0.3s |  |
| iat | OBD | 78 | 11.3s |  |
| lambda | OBD | 7797 | 0.3s | 5773 |
| load | OBD | 7797 | 0.3s |  |
| maf | OBD | 7797 | 0.3s |  |
| map | OBD | 7797 | 0.3s |  |
| n47d_ambient_press | DDE | 67 | 13.0s |  |
| n47d_boost_act | DDE | 68 | 13.0s |  |
| n47d_boost_set | DDE | 68 | 13.0s |  |
| n47d_charge_air_temp | DDE | 67 | 13.0s |  |
| n47d_converter_temp | DDE | 68 | 13.0s |  |
| n47d_coolant | DDE | 68 | 13.0s |  |
| n47d_dist_since_regen | DDE | 68 | 13.0s |  |
| n47d_dpf_dp | DDE | 68 | 13.0s |  |
| n47d_egr_deviation | DDE | 67 | 13.0s | 67 |
| n47d_engine_temp | DDE | 68 | 13.0s |  |
| n47d_exh_temp_pre_cat | DDE | 68 | 13.0s |  |
| n47d_exh_temp_pre_dpf | DDE | 68 | 13.0s |  |
| n47d_gbx_oil_temp | DDE | 68 | 13.0s |  |
| n47d_maf_per_cyl | DDE | 68 | 13.1s |  |
| n47d_oil_temp | DDE | 68 | 13.0s |  |
| n47d_opmode | DDE | 68 | 13.1s | 68 |
| n47d_pedal | DDE | 67 | 13.0s |  |
| n47d_rail_act | DDE | 68 | 13.0s |  |
| n47d_rail_set | DDE | 68 | 13.0s |  |
| n47d_regen_count | DDE | 68 | 13.0s | 68 |
| n47d_soot_meas | DDE | 68 | 13.0s |  |
| n47d_soot_model | DDE | 68 | 13.0s |  |
| n47d_turbine_speed | DDE | 68 | 13.0s |  |
| pedal | OBD | 7797 | 0.3s |  |
| rail | OBD | 7797 | 0.3s |  |
| relthr | OBD | 7797 | 0.3s |  |
| rpm | OBD | 7797 | 0.3s |  |
| runtime | OBD | 78 | 11.3s |  |
| speed | OBD | 7797 | 0.3s |  |
| throttle | OBD | 7797 | 0.3s |  |
| voltage | OBD | 78 | 11.3s |  |

---
_Read-only analysis; no baselines across sessions claimed yet._
