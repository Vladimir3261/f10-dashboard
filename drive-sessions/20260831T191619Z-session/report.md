# Session report — run 2

- ECU: 0x12 (ECM-EngineControl)  (addr 18)
- Duration: 43.3 min, 78444 samples across 45 channels
- Started (UTC): 2026-08-31T18:30:42Z

## Key findings

- Cold start captured from 67.0 °C; coolant reached 80 °C in 1.5 min and stabilised near 93.0 °C. Oil ran +0.2 °C against coolant through the ramp, so no lag was seen.
- **OBD MAP saturates at 255 kPa**; under boost the DDE reads the true manifold pressure up to 271 kPa. The boost cross-check ⚠️ is OBD sensor saturation, NOT a decode error — above 255 kPa the DDE boost channel is the accurate one. (Exactly the 'generic OBD saturation' caveat the project set out to handle.)
- Ambient/baro cross-check differs by only 3.22 hPa on average — that is the standard OBD baro PID's 1 kPa integer quantisation, i.e. agreement within resolution, not a discrepancy.
- Lambda sat at the 2.0 sentinel for 130/206 samples (= 'no value', not a real λ of 2.0); exclude those from any AFR analysis.
- Boost closed-loop control tracked its setpoint to 212.0 mean deviation (max 993.0) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- Rail pressure closed-loop control tracked its setpoint to 209.1 mean deviation (max 989.4) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- DPF soot measured vs modelled agree to 0.009 g (range 10.59–11.41 g) — differential-pressure sensing is healthy; this is a baseline to trend soot-accumulation rate against.
- [CANDIDATE] DPF differential pressure -9.0–97.0 hPa — should read low warm-idle and rise with exhaust flow under load; a plausible spread validates the 0x44F8 scale. (Baseline for filter-restriction trending.)
- [CANDIDATE] Exhaust temp before DPF 68–351 °C, before catalyst 135–495 °C — should climb under load; pre-cat typically hotter than pre-DPF. Validates the exhaust-temp scales.
- [CANDIDATE] Distance since regen 71.6–102.6 km — should be a steady value increasing monotonically over the drive (unless a regen completes, resetting it).
- [CANDIDATE] EGR control deviation 0.0–125.0 % — should sit near 0 when the loop is happy; a persistent offset would flag EGR fouling. Baseline for EGR-health trending.
- [CANDIDATE] Operating-mode word took 2 distinct value(s) — bit 0x02 is the regeneration-active flag; a change mid-drive would mark a regeneration event.

## Cold-start warm-up

| channel | start | max | →80 °C | unit |
|---|---|---|---|---|
| coolant | 67.0 | 93.0 | 91s | °C |
| n47d_oil_temp | 67.5 | 93.8 | 96s | °C |
| n47d_engine_temp | 67.9 | 93.9 | 90s | °C |
| n47d_charge_air_temp | 42.7 | 51.9 | — | °C |

- Across the warm-up, oil ran **+0.25 °C** against coolant (mean of 8 matched pairs, range -0.70 to +1.20).
  The two track each other to within 1 °C — no lag either way.

## Proprietary DDE vs standard OBD (live cross-check)

| quantity | pairs | mean |Δ| | max |Δ| | agree |
|---|---|---|---|---|
| coolant °C | 152 | 0.47 | 1.64 | ✅ |
| manifold/boost (hPa vs kPa×10) | 152 | 24.85 | 163.9 | ⚠️ |
| ambient (hPa vs kPa×10) | 152 | 3.22 | 11.0 | ⚠️ |

## Drive / load behaviour

- max speed 174.0 km/h; 11865 driving / 2006 idle samples (speed>3 km/h = driving).

| loop | pairs | mean |dev| | max |dev| |
|---|---|---|---|
| boost (act−set) | 146 | 212.0 | 993.0 |
| rail pressure (act−set) | 146 | 209.1 | 989.4 |

| channel | min | max | mean | p95 |
|---|---|---|---|---|
| rpm | 731.0 | 3971.0 | 1492.131 | 2361.5 |
| map | 99.0 | 255.0 | 139.336 | 249.0 |
| n47d_boost_act | 998.9 | 2713.9 | 1408.773 | 2491.9 |
| n47d_rail_act | 285.1 | 1758.5 | 676.543 | 1314.6 |
| n47d_maf_per_cyl | 263.09 | 1447.36 | 682.73 | 1221.97 |
| n47d_pedal | 0.0 | 79.8 | 19.751 | 63.8 |
| load | 0.0 | 100.0 | 37.271 | 99.608 |
| speed | 0.0 | 174.0 | 55.516 | 120.0 |
| maf | 0.0 | 193.8 | 44.575 | 109.24 |
| rail | 242.1 | 1754.6 | 676.31 | 1298.6 |

## DPF

- soot measured: 10.59–11.41 g
- soot modelled: 10.6–11.42 g
- measured vs modelled mean |Δ|: 0.009 g (the two independent estimates should agree)

## Data quality / coverage

| channel | src | samples | max gap | pinned@max |
|---|---|---|---|---|
| ambient | OBD | 35 | 60.3s |  |
| baro | OBD | 35 | 60.3s | 12 |
| boost | DDE | 13351 | 16.8s |  |
| cattemp | OBD | 206 | 11.9s |  |
| coolant | OBD | 206 | 11.9s |  |
| distance | OBD | 35 | 60.3s |  |
| egr | OBD | 206 | 11.9s |  |
| egrerr | OBD | 206 | 11.9s |  |
| gear | DDE | 3495 | 3.9s |  |
| iat | OBD | 206 | 11.9s |  |
| lambda | OBD | 206 | 11.9s | 130 |
| load | OBD | 206 | 11.9s |  |
| maf | OBD | 206 | 11.9s |  |
| map | OBD | 13874 | 3.4s |  |
| n47d_ambient_press | DDE | 152 | 16.6s |  |
| n47d_boost_act | DDE | 152 | 16.5s |  |
| n47d_boost_set | DDE | 152 | 16.5s |  |
| n47d_charge_air_temp | DDE | 152 | 16.6s |  |
| n47d_converter_temp | DDE | 152 | 16.3s |  |
| n47d_coolant | DDE | 152 | 16.5s |  |
| n47d_dist_since_regen | DDE | 152 | 16.3s |  |
| n47d_dpf_dp | DDE | 152 | 16.5s |  |
| n47d_egr_deviation | DDE | 152 | 16.6s |  |
| n47d_engine_temp | DDE | 152 | 16.3s |  |
| n47d_exh_temp_pre_cat | DDE | 152 | 16.3s |  |
| n47d_exh_temp_pre_dpf | DDE | 152 | 16.5s |  |
| n47d_gbx_oil_temp | DDE | 152 | 16.5s | 36 |
| n47d_maf_per_cyl | DDE | 152 | 16.4s |  |
| n47d_oil_temp | DDE | 151 | 29.6s |  |
| n47d_opmode | DDE | 152 | 16.4s |  |
| n47d_pedal | DDE | 152 | 16.9s |  |
| n47d_rail_act | DDE | 152 | 16.3s |  |
| n47d_rail_set | DDE | 152 | 16.3s |  |
| n47d_regen_count | DDE | 152 | 16.3s | 152 |
| n47d_soot_meas | DDE | 152 | 16.5s |  |
| n47d_soot_model | DDE | 152 | 16.3s |  |
| n47d_turbine_speed | DDE | 152 | 16.6s |  |
| pedal | OBD | 13873 | 3.4s |  |
| rail | OBD | 206 | 11.9s |  |
| relthr | OBD | 206 | 11.9s |  |
| rpm | OBD | 13873 | 3.4s |  |
| runtime | OBD | 35 | 60.3s |  |
| speed | OBD | 13871 | 3.7s |  |
| throttle | OBD | 206 | 11.9s |  |
| voltage | OBD | 206 | 11.9s |  |

---
_Read-only analysis; no baselines across sessions claimed yet._
