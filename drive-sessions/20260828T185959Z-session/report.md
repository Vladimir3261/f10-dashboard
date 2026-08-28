# Session report — run 7

- ECU: 0x12 (ECM-EngineControl)  (addr 18)
- Duration: 7.7 min, 55266 samples across 46 channels
- Started (UTC): 2026-08-28T18:51:40Z

## Key findings

- Cold start captured from 92.0 °C; coolant reached 80 °C in 0.0 min and stabilised near 92.0 °C. Oil and engine temp tracked it closely — a healthy warm-up with no lag anomaly.
- Ambient/baro cross-check differs by only 6.06 hPa on average — that is the standard OBD baro PID's 1 kPa integer quantisation, i.e. agreement within resolution, not a discrepancy.
- Lambda sat at the 2.0 sentinel for 2988/4155 samples (= 'no value', not a real λ of 2.0); exclude those from any AFR analysis.
- Boost closed-loop control tracked its setpoint to 225.5 mean deviation (max 777.0) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- Rail pressure closed-loop control tracked its setpoint to 243.4 mean deviation (max 678.1) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- DPF soot measured vs modelled agree to 0.007 g (range 7.1–7.22 g) — differential-pressure sensing is healthy; this is a baseline to trend soot-accumulation rate against.
- [CANDIDATE] DPF differential pressure 0.0–39.0 hPa — should read low warm-idle and rise with exhaust flow under load; a plausible spread validates the 0x44F8 scale. (Baseline for filter-restriction trending.)
- [CANDIDATE] Exhaust temp before DPF 161–272 °C, before catalyst 202–421 °C — should climb under load; pre-cat typically hotter than pre-DPF. Validates the exhaust-temp scales.
- [CANDIDATE] Distance since regen 221.1–227.6 km — should be a steady value increasing monotonically over the drive (unless a regen completes, resetting it).
- [CANDIDATE] EGR control deviation 0.0–0.0 % — should sit near 0 when the loop is happy; a persistent offset would flag EGR fouling. Baseline for EGR-health trending.
- [CANDIDATE] Operating-mode word took 1 distinct value(s) — bit 0x02 is the regeneration-active flag; a change mid-drive would mark a regeneration event.

## Cold-start warm-up

| channel | start | max | →80 °C | unit |
|---|---|---|---|---|
| coolant | 92.0 | 92.0 | 0s | °C |
| n47d_oil_temp | 92.0 | 92.1 | 1s | °C |
| n47d_engine_temp | 92.3 | 92.3 | 8s | °C |
| n47d_charge_air_temp | 44.9 | 47.3 | — | °C |

- When coolant reached 80 °C, oil was **92.0 °C** (oil lags coolant — the expected warm-up signature).

## Proprietary DDE vs standard OBD (live cross-check)

| quantity | pairs | mean |Δ| | max |Δ| | agree |
|---|---|---|---|---|
| coolant °C | 37 | 0.48 | 1.36 | ✅ |
| manifold/boost (hPa vs kPa×10) | 36 | 18.91 | 99.9 | ⚠️ |
| ambient (hPa vs kPa×10) | 36 | 6.06 | 9.0 | ⚠️ |

## Drive / load behaviour

- max speed 131.0 km/h; 2831 driving / 1324 idle samples (speed>3 km/h = driving).

| loop | pairs | mean |dev| | max |dev| |
|---|---|---|---|
| boost (act−set) | 29 | 225.5 | 777.0 |
| rail pressure (act−set) | 29 | 243.4 | 678.1 |

| channel | min | max | mean | p95 |
|---|---|---|---|---|
| rpm | 740.5 | 3436.0 | 1564.928 | 2319.5 |
| map | 105.0 | 255.0 | 149.908 | 246.0 |
| n47d_boost_act | 1055.0 | 2454.9 | 1502.959 | 2440.9 |
| n47d_rail_act | 292.0 | 1443.9 | 685.669 | 1386.1 |
| n47d_maf_per_cyl | 495.29 | 1248.46 | 724.632 | 1202.37 |
| n47d_pedal | 0.0 | 81.8 | 23.799 | 60.27 |
| load | 0.0 | 100.0 | 38.199 | 98.039 |
| speed | 0.0 | 131.0 | 64.16 | 121.0 |
| maf | 0.0 | 190.41 | 51.534 | 116.97 |
| rail | 233.2 | 1724.8 | 682.821 | 1272.7 |

## DPF

- soot measured: 7.1–7.22 g
- soot modelled: 7.1–7.23 g
- measured vs modelled mean |Δ|: 0.007 g (the two independent estimates should agree)

## Data quality / coverage

| channel | src | samples | max gap | pinned@max |
|---|---|---|---|---|
| ambient | OBD | 42 | 11.3s | 16 |
| baro | OBD | 42 | 11.3s | 19 |
| boost | DDE | 4155 | 0.3s |  |
| cattemp | OBD | 42 | 11.3s |  |
| coolant | OBD | 42 | 11.3s |  |
| distance | OBD | 42 | 11.3s | 15 |
| egr | OBD | 42 | 11.3s | 42 |
| egrerr | OBD | 42 | 11.3s | 42 |
| egs_da2e_b0 | DDE | 4155 | 0.3s | 4155 |
| gear | DDE | 4155 | 0.3s | 840 |
| iat | OBD | 42 | 11.3s |  |
| lambda | OBD | 4155 | 0.3s | 2988 |
| load | OBD | 4155 | 0.3s |  |
| maf | OBD | 4155 | 0.3s |  |
| map | OBD | 4155 | 0.3s |  |
| n47d_ambient_press | DDE | 36 | 13.0s |  |
| n47d_boost_act | DDE | 36 | 13.0s |  |
| n47d_boost_set | DDE | 36 | 13.0s |  |
| n47d_charge_air_temp | DDE | 36 | 13.0s |  |
| n47d_converter_temp | DDE | 36 | 13.0s | 10 |
| n47d_coolant | DDE | 37 | 13.0s |  |
| n47d_dist_since_regen | DDE | 36 | 13.0s |  |
| n47d_dpf_dp | DDE | 37 | 13.0s |  |
| n47d_egr_deviation | DDE | 36 | 13.0s | 36 |
| n47d_engine_temp | DDE | 36 | 13.0s |  |
| n47d_exh_temp_pre_cat | DDE | 36 | 13.0s |  |
| n47d_exh_temp_pre_dpf | DDE | 36 | 13.0s |  |
| n47d_gbx_oil_temp | DDE | 36 | 13.0s |  |
| n47d_maf_per_cyl | DDE | 36 | 13.0s |  |
| n47d_oil_temp | DDE | 37 | 13.0s |  |
| n47d_opmode | DDE | 36 | 13.0s | 36 |
| n47d_pedal | DDE | 36 | 13.0s |  |
| n47d_rail_act | DDE | 36 | 13.0s |  |
| n47d_rail_set | DDE | 36 | 13.0s |  |
| n47d_regen_count | DDE | 36 | 13.0s | 36 |
| n47d_soot_meas | DDE | 36 | 13.0s | 10 |
| n47d_soot_model | DDE | 36 | 13.0s | 8 |
| n47d_turbine_speed | DDE | 36 | 13.0s |  |
| pedal | OBD | 4155 | 0.3s |  |
| rail | OBD | 4155 | 0.3s |  |
| relthr | OBD | 4155 | 0.3s |  |
| rpm | OBD | 4155 | 0.3s |  |
| runtime | OBD | 42 | 11.3s |  |
| speed | OBD | 4155 | 0.3s |  |
| throttle | OBD | 4155 | 0.3s |  |
| voltage | OBD | 42 | 11.3s |  |

---
_Read-only analysis; no baselines across sessions claimed yet._
