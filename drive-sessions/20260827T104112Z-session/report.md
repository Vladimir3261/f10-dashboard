# Session report — run 3

- ECU: 0x12 (ECM-EngineControl)  (addr 18)
- Duration: 4.7 min, 32630 samples across 46 channels
- Started (UTC): 2026-08-27T10:22:38Z

## Key findings

- Cold start captured from 98.0 °C; coolant reached 80 °C in 0.0 min and stabilised near 99.0 °C. Oil and engine temp tracked it closely — a healthy warm-up with no lag anomaly.
- Ambient/baro cross-check differs by only 8.05 hPa on average — that is the standard OBD baro PID's 1 kPa integer quantisation, i.e. agreement within resolution, not a discrepancy.
- Lambda sat at the 2.0 sentinel for 1891/2453 samples (= 'no value', not a real λ of 2.0); exclude those from any AFR analysis.
- Boost closed-loop control tracked its setpoint to 221.5 mean deviation (max 1066.1) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- Rail pressure closed-loop control tracked its setpoint to 216.4 mean deviation (max 797.5) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- DPF soot measured vs modelled agree to 0.006 g (range 2.94–3.04 g) — differential-pressure sensing is healthy; this is a baseline to trend soot-accumulation rate against.
- [CANDIDATE] DPF differential pressure 3.0–73.0 hPa — should read low warm-idle and rise with exhaust flow under load; a plausible spread validates the 0x44F8 scale. (Baseline for filter-restriction trending.)
- [CANDIDATE] Exhaust temp before DPF 182–276 °C, before catalyst 204–360 °C — should climb under load; pre-cat typically hotter than pre-DPF. Validates the exhaust-temp scales.
- [CANDIDATE] Distance since regen 90.7–93.1 km — should be a steady value increasing monotonically over the drive (unless a regen completes, resetting it).
- [CANDIDATE] EGR control deviation 0.0–0.0 % — should sit near 0 when the loop is happy; a persistent offset would flag EGR fouling. Baseline for EGR-health trending.
- [CANDIDATE] Operating-mode word took 1 distinct value(s) — bit 0x02 is the regeneration-active flag; a change mid-drive would mark a regeneration event.

## Cold-start warm-up

| channel | start | max | →80 °C | unit |
|---|---|---|---|---|
| coolant | 98.0 | 99.0 | 0s | °C |
| n47d_oil_temp | 98.1 | 99.6 | 1s | °C |
| n47d_engine_temp | 96.4 | 99.2 | 8s | °C |
| n47d_charge_air_temp | 41.7 | 51.6 | — | °C |

- When coolant reached 80 °C, oil was **98.1 °C** (oil lags coolant — the expected warm-up signature).

## Proprietary DDE vs standard OBD (live cross-check)

| quantity | pairs | mean |Δ| | max |Δ| | agree |
|---|---|---|---|---|
| coolant °C | 22 | 0.56 | 1.44 | ✅ |
| manifold/boost (hPa vs kPa×10) | 22 | 21.47 | 176.0 | ⚠️ |
| ambient (hPa vs kPa×10) | 21 | 8.05 | 9.0 | ⚠️ |

## Drive / load behaviour

- max speed 103.0 km/h; 1741 driving / 712 idle samples (speed>3 km/h = driving).

| loop | pairs | mean |dev| | max |dev| |
|---|---|---|---|
| boost (act−set) | 22 | 221.5 | 1066.1 |
| rail pressure (act−set) | 21 | 216.4 | 797.5 |

| channel | min | max | mean | p95 |
|---|---|---|---|---|
| rpm | 742.0 | 3589.5 | 1396.866 | 2633.5 |
| map | 105.0 | 255.0 | 133.985 | 255.0 |
| n47d_boost_act | 1057.0 | 2282.0 | 1275.464 | 1881.0 |
| n47d_rail_act | 283.0 | 1559.4 | 606.067 | 1244.8 |
| n47d_maf_per_cyl | 490.38 | 1253.27 | 678.269 | 1186.76 |
| n47d_pedal | 0.0 | 76.7 | 18.877 | 67.66 |
| load | 0.0 | 100.0 | 32.716 | 97.647 |
| speed | 0.0 | 103.0 | 34.471 | 88.0 |
| maf | 9.58 | 186.74 | 41.302 | 129.72 |
| rail | 232.2 | 1770.4 | 628.439 | 1406.0 |

## DPF

- soot measured: 2.94–3.04 g
- soot modelled: 2.96–3.04 g
- measured vs modelled mean |Δ|: 0.006 g (the two independent estimates should agree)

## Data quality / coverage

| channel | src | samples | max gap | pinned@max |
|---|---|---|---|---|
| ambient | OBD | 25 | 11.6s | 25 |
| baro | OBD | 25 | 11.6s | 25 |
| boost | DDE | 2453 | 0.3s |  |
| cattemp | OBD | 25 | 11.6s |  |
| coolant | OBD | 25 | 11.6s |  |
| distance | OBD | 25 | 11.6s |  |
| egr | OBD | 25 | 11.6s | 25 |
| egrerr | OBD | 25 | 11.6s | 25 |
| egs_da2e_b0 | DDE | 2453 | 0.3s | 2453 |
| gear | DDE | 2453 | 0.3s |  |
| iat | OBD | 25 | 11.6s |  |
| lambda | OBD | 2453 | 0.3s | 1891 |
| load | OBD | 2453 | 0.3s |  |
| maf | OBD | 2453 | 0.3s |  |
| map | OBD | 2453 | 0.3s |  |
| n47d_ambient_press | DDE | 21 | 13.4s |  |
| n47d_boost_act | DDE | 22 | 13.4s |  |
| n47d_boost_set | DDE | 21 | 13.4s |  |
| n47d_charge_air_temp | DDE | 21 | 13.4s |  |
| n47d_converter_temp | DDE | 21 | 13.4s |  |
| n47d_coolant | DDE | 22 | 13.4s |  |
| n47d_dist_since_regen | DDE | 21 | 13.4s |  |
| n47d_dpf_dp | DDE | 22 | 13.4s |  |
| n47d_egr_deviation | DDE | 21 | 13.4s | 21 |
| n47d_engine_temp | DDE | 21 | 13.4s |  |
| n47d_exh_temp_pre_cat | DDE | 21 | 13.4s |  |
| n47d_exh_temp_pre_dpf | DDE | 22 | 13.4s |  |
| n47d_gbx_oil_temp | DDE | 22 | 13.4s |  |
| n47d_maf_per_cyl | DDE | 21 | 13.4s |  |
| n47d_oil_temp | DDE | 22 | 13.4s |  |
| n47d_opmode | DDE | 21 | 13.4s | 21 |
| n47d_pedal | DDE | 21 | 13.4s |  |
| n47d_rail_act | DDE | 21 | 13.4s |  |
| n47d_rail_set | DDE | 21 | 13.4s |  |
| n47d_regen_count | DDE | 21 | 13.4s | 21 |
| n47d_soot_meas | DDE | 22 | 13.4s |  |
| n47d_soot_model | DDE | 21 | 13.4s |  |
| n47d_turbine_speed | DDE | 22 | 13.4s |  |
| pedal | OBD | 2453 | 0.3s |  |
| rail | OBD | 2453 | 0.3s |  |
| relthr | OBD | 2453 | 0.3s |  |
| rpm | OBD | 2453 | 0.3s |  |
| runtime | OBD | 25 | 11.6s |  |
| speed | OBD | 2453 | 0.3s |  |
| throttle | OBD | 2453 | 0.3s |  |
| voltage | OBD | 25 | 11.6s |  |

---
_Read-only analysis; no baselines across sessions claimed yet._
