# Session report — run 4

- ECU: 0x12 (ECM-EngineControl)  (addr 18)
- Duration: 3.8 min, 26561 samples across 46 channels
- Started (UTC): 2026-08-27T09:53:43Z

## Key findings

- Cold start captured from 95.0 °C; coolant reached 80 °C in 0.0 min and stabilised near 96.0 °C. Oil and engine temp tracked it closely — a healthy warm-up with no lag anomaly.
- Ambient/baro cross-check differs by only 7.53 hPa on average — that is the standard OBD baro PID's 1 kPa integer quantisation, i.e. agreement within resolution, not a discrepancy.
- Lambda sat at the 2.0 sentinel for 1681/1997 samples (= 'no value', not a real λ of 2.0); exclude those from any AFR analysis.
- Boost closed-loop control tracked its setpoint to 167.3 mean deviation (max 1262.0) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- Rail pressure closed-loop control tracked its setpoint to 220.9 mean deviation (max 647.9) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- DPF soot measured vs modelled agree to 0.007 g (range 2.72–2.78 g) — differential-pressure sensing is healthy; this is a baseline to trend soot-accumulation rate against.
- [CANDIDATE] DPF differential pressure -1.0–28.0 hPa — should read low warm-idle and rise with exhaust flow under load; a plausible spread validates the 0x44F8 scale. (Baseline for filter-restriction trending.)
- [CANDIDATE] Exhaust temp before DPF 174–214 °C, before catalyst 169–301 °C — should climb under load; pre-cat typically hotter than pre-DPF. Validates the exhaust-temp scales.
- [CANDIDATE] Distance since regen 86.5–88.1 km — should be a steady value increasing monotonically over the drive (unless a regen completes, resetting it).
- [CANDIDATE] EGR control deviation 0.0–0.0 % — should sit near 0 when the loop is happy; a persistent offset would flag EGR fouling. Baseline for EGR-health trending.
- [CANDIDATE] Operating-mode word took 1 distinct value(s) — bit 0x02 is the regeneration-active flag; a change mid-drive would mark a regeneration event.

## Cold-start warm-up

| channel | start | max | →80 °C | unit |
|---|---|---|---|---|
| coolant | 95.0 | 96.0 | 0s | °C |
| n47d_oil_temp | 95.0 | 96.2 | 1s | °C |
| n47d_engine_temp | 95.4 | 96.1 | 8s | °C |
| n47d_charge_air_temp | 49.4 | 49.4 | — | °C |

- When coolant reached 80 °C, oil was **95.0 °C** (oil lags coolant — the expected warm-up signature).

## Proprietary DDE vs standard OBD (live cross-check)

| quantity | pairs | mean |Δ| | max |Δ| | agree |
|---|---|---|---|---|
| coolant °C | 18 | 0.51 | 1.16 | ✅ |
| manifold/boost (hPa vs kPa×10) | 18 | 13.61 | 94.0 | ⚠️ |
| ambient (hPa vs kPa×10) | 17 | 7.53 | 11.0 | ⚠️ |

## Drive / load behaviour

- max speed 66.0 km/h; 1979 driving / 18 idle samples (speed>3 km/h = driving).

| loop | pairs | mean |dev| | max |dev| |
|---|---|---|---|
| boost (act−set) | 18 | 167.3 | 1262.0 |
| rail pressure (act−set) | 17 | 220.9 | 647.9 |

| channel | min | max | mean | p95 |
|---|---|---|---|---|
| rpm | 754.0 | 2552.0 | 1428.106 | 1975.5 |
| map | 105.0 | 255.0 | 125.84 | 172.0 |
| n47d_boost_act | 1069.0 | 1298.0 | 1153.694 | 1298.0 |
| n47d_rail_act | 274.1 | 1127.3 | 580.476 | 1127.3 |
| n47d_maf_per_cyl | 490.48 | 861.37 | 669.965 | 861.37 |
| n47d_pedal | 0.0 | 63.42 | 17.531 | 63.42 |
| load | 0.0 | 100.0 | 26.309 | 79.608 |
| speed | 0.0 | 66.0 | 26.888 | 51.0 |
| maf | 9.47 | 134.91 | 36.886 | 69.86 |
| rail | 237.2 | 1436.0 | 578.297 | 1045.7 |

## DPF

- soot measured: 2.72–2.78 g
- soot modelled: 2.73–2.78 g
- measured vs modelled mean |Δ|: 0.007 g (the two independent estimates should agree)

## Data quality / coverage

| channel | src | samples | max gap | pinned@max |
|---|---|---|---|---|
| ambient | OBD | 20 | 11.6s | 9 |
| baro | OBD | 20 | 11.6s |  |
| boost | DDE | 1997 | 0.3s |  |
| cattemp | OBD | 20 | 11.6s |  |
| coolant | OBD | 20 | 11.6s |  |
| distance | OBD | 20 | 11.6s | 5 |
| egr | OBD | 20 | 11.6s | 20 |
| egrerr | OBD | 20 | 11.6s | 20 |
| egs_da2e_b0 | DDE | 1997 | 0.3s | 1997 |
| gear | DDE | 1997 | 0.3s |  |
| iat | OBD | 20 | 11.6s |  |
| lambda | OBD | 1997 | 0.3s | 1681 |
| load | OBD | 1997 | 0.3s |  |
| maf | OBD | 1997 | 0.3s |  |
| map | OBD | 1997 | 0.3s |  |
| n47d_ambient_press | DDE | 17 | 13.4s |  |
| n47d_boost_act | DDE | 18 | 13.5s |  |
| n47d_boost_set | DDE | 18 | 13.5s |  |
| n47d_charge_air_temp | DDE | 17 | 13.4s |  |
| n47d_converter_temp | DDE | 17 | 13.3s | 6 |
| n47d_coolant | DDE | 18 | 13.4s |  |
| n47d_dist_since_regen | DDE | 17 | 13.4s |  |
| n47d_dpf_dp | DDE | 18 | 13.4s |  |
| n47d_egr_deviation | DDE | 17 | 13.4s | 17 |
| n47d_engine_temp | DDE | 17 | 13.4s |  |
| n47d_exh_temp_pre_cat | DDE | 17 | 13.3s |  |
| n47d_exh_temp_pre_dpf | DDE | 18 | 13.5s |  |
| n47d_gbx_oil_temp | DDE | 18 | 13.4s |  |
| n47d_maf_per_cyl | DDE | 17 | 13.4s |  |
| n47d_oil_temp | DDE | 18 | 13.4s |  |
| n47d_opmode | DDE | 17 | 13.4s | 17 |
| n47d_pedal | DDE | 17 | 13.4s |  |
| n47d_rail_act | DDE | 17 | 13.4s |  |
| n47d_rail_set | DDE | 17 | 13.4s |  |
| n47d_regen_count | DDE | 17 | 13.4s | 17 |
| n47d_soot_meas | DDE | 18 | 13.4s | 4 |
| n47d_soot_model | DDE | 17 | 13.3s |  |
| n47d_turbine_speed | DDE | 18 | 13.5s |  |
| pedal | OBD | 1997 | 0.3s |  |
| rail | OBD | 1997 | 0.3s |  |
| relthr | OBD | 1997 | 0.3s |  |
| rpm | OBD | 1997 | 0.3s |  |
| runtime | OBD | 20 | 11.6s |  |
| speed | OBD | 1997 | 0.3s |  |
| throttle | OBD | 1997 | 0.3s |  |
| voltage | OBD | 20 | 11.6s |  |

---
_Read-only analysis; no baselines across sessions claimed yet._
