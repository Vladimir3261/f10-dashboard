# Session report — run 1

- ECU: 0x12 (ECM-EngineControl)  (addr 18)
- Duration: 40.9 min, 101731 samples across 45 channels
- Started (UTC): 2026-08-31T11:58:09Z

## Key findings

- Cold start captured from 45.0 °C; coolant reached 80 °C in 3.0 min and stabilised near 95.0 °C. Oil ran +0.3 °C against coolant through the ramp, so no lag was seen.
- **OBD MAP saturates at 255 kPa**; under boost the DDE reads the true manifold pressure up to 270 kPa. The boost cross-check ⚠️ is OBD sensor saturation, NOT a decode error — above 255 kPa the DDE boost channel is the accurate one. (Exactly the 'generic OBD saturation' caveat the project set out to handle.)
- Ambient/baro cross-check differs by only 3.49 hPa on average — that is the standard OBD baro PID's 1 kPa integer quantisation, i.e. agreement within resolution, not a discrepancy.
- Lambda sat at the 2.0 sentinel for 145/238 samples (= 'no value', not a real λ of 2.0); exclude those from any AFR analysis.
- Boost closed-loop control tracked its setpoint to 85.5 mean deviation (max 773.0) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- Rail pressure closed-loop control tracked its setpoint to 98.5 mean deviation (max 627.6) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- DPF soot measured vs modelled agree to 0.008 g (range 9.61–10.47 g) — differential-pressure sensing is healthy; this is a baseline to trend soot-accumulation rate against.
- [CANDIDATE] DPF differential pressure -12.0–37.0 hPa — should read low warm-idle and rise with exhaust flow under load; a plausible spread validates the 0x44F8 scale. (Baseline for filter-restriction trending.)
- [CANDIDATE] Exhaust temp before DPF 60–306 °C, before catalyst 114–452 °C — should climb under load; pre-cat typically hotter than pre-DPF. Validates the exhaust-temp scales.
- [CANDIDATE] Distance since regen 45.3–71.5 km — should be a steady value increasing monotonically over the drive (unless a regen completes, resetting it).
- [CANDIDATE] EGR control deviation 0.0–144.4 % — should sit near 0 when the loop is happy; a persistent offset would flag EGR fouling. Baseline for EGR-health trending.
- [CANDIDATE] Operating-mode word took 1 distinct value(s) — bit 0x02 is the regeneration-active flag; a change mid-drive would mark a regeneration event.

## Cold-start warm-up

| channel | start | max | →80 °C | unit |
|---|---|---|---|---|
| coolant | 45.0 | 95.0 | 181s | °C |
| n47d_oil_temp | 45.1 | 94.9 | 185s | °C |
| n47d_engine_temp | 45.4 | 95.1 | 192s | °C |
| n47d_charge_air_temp | 40.9 | 62.0 | — | °C |

- Across the warm-up, oil ran **+0.29 °C** against coolant (mean of 16 matched pairs, range -1.70 to +1.50).
  The two track each other to within 1 °C — no lag either way.

## Proprietary DDE vs standard OBD (live cross-check)

| quantity | pairs | mean |Δ| | max |Δ| | agree |
|---|---|---|---|---|
| coolant °C | 183 | 0.47 | 2.36 | ✅ |
| manifold/boost (hPa vs kPa×10) | 183 | 9.79 | 146.0 | ⚠️ |
| ambient (hPa vs kPa×10) | 182 | 3.49 | 9.0 | ⚠️ |

## Drive / load behaviour

- max speed 131.0 km/h; 13574 driving / 4521 idle samples (speed>3 km/h = driving).

| loop | pairs | mean |dev| | max |dev| |
|---|---|---|---|
| boost (act−set) | 163 | 85.5 | 773.0 |
| rail pressure (act−set) | 163 | 98.5 | 627.6 |

| channel | min | max | mean | p95 |
|---|---|---|---|---|
| rpm | 722.0 | 2711.5 | 1370.237 | 1952.0 |
| map | 99.0 | 255.0 | 126.228 | 214.0 |
| n47d_boost_act | 997.9 | 2696.0 | 1253.843 | 2114.0 |
| n47d_rail_act | 272.1 | 1422.1 | 571.908 | 1081.5 |
| n47d_maf_per_cyl | 235.99 | 1412.25 | 549.597 | 1098.46 |
| n47d_pedal | 0.0 | 68.97 | 14.439 | 46.38 |
| load | 0.0 | 100.0 | 45.6 | 97.255 |
| speed | 0.0 | 131.0 | 47.918 | 119.0 |
| maf | 1.27 | 132.08 | 31.174 | 82.36 |
| rail | 261.0 | 1447.9 | 570.6 | 1137.3 |

## DPF

- soot measured: 9.61–10.47 g
- soot modelled: 9.62–10.48 g
- measured vs modelled mean |Δ|: 0.008 g (the two independent estimates should agree)

## Data quality / coverage

| channel | src | samples | max gap | pinned@max |
|---|---|---|---|---|
| ambient | OBD | 40 | 60.4s |  |
| baro | OBD | 40 | 60.4s | 40 |
| boost | DDE | 18096 | 3.2s |  |
| cattemp | OBD | 238 | 13.6s |  |
| coolant | OBD | 238 | 13.6s |  |
| distance | OBD | 40 | 60.4s |  |
| egr | OBD | 238 | 13.6s |  |
| egrerr | OBD | 238 | 13.6s |  |
| gear | DDE | 4038 | 15.3s |  |
| iat | OBD | 238 | 13.6s |  |
| lambda | OBD | 238 | 13.6s | 145 |
| load | OBD | 238 | 13.6s |  |
| maf | OBD | 238 | 13.6s |  |
| map | OBD | 18096 | 3.2s |  |
| n47d_ambient_press | DDE | 182 | 16.7s |  |
| n47d_boost_act | DDE | 183 | 16.4s |  |
| n47d_boost_set | DDE | 183 | 16.7s |  |
| n47d_charge_air_temp | DDE | 182 | 16.7s |  |
| n47d_converter_temp | DDE | 183 | 16.7s |  |
| n47d_coolant | DDE | 183 | 16.4s |  |
| n47d_dist_since_regen | DDE | 183 | 16.7s |  |
| n47d_dpf_dp | DDE | 183 | 16.7s |  |
| n47d_egr_deviation | DDE | 182 | 16.7s |  |
| n47d_engine_temp | DDE | 182 | 16.7s |  |
| n47d_exh_temp_pre_cat | DDE | 183 | 16.7s |  |
| n47d_exh_temp_pre_dpf | DDE | 183 | 16.4s |  |
| n47d_gbx_oil_temp | DDE | 182 | 27.0s |  |
| n47d_maf_per_cyl | DDE | 182 | 16.7s |  |
| n47d_oil_temp | DDE | 183 | 16.7s |  |
| n47d_opmode | DDE | 182 | 16.7s | 182 |
| n47d_pedal | DDE | 182 | 16.7s |  |
| n47d_rail_act | DDE | 183 | 16.3s |  |
| n47d_rail_set | DDE | 182 | 16.7s |  |
| n47d_regen_count | DDE | 182 | 16.7s | 182 |
| n47d_soot_meas | DDE | 183 | 16.4s |  |
| n47d_soot_model | DDE | 183 | 16.7s |  |
| n47d_turbine_speed | DDE | 183 | 16.4s |  |
| pedal | OBD | 18095 | 3.2s |  |
| rail | OBD | 238 | 13.6s |  |
| relthr | OBD | 238 | 13.6s |  |
| rpm | OBD | 18096 | 3.2s |  |
| runtime | OBD | 40 | 60.4s |  |
| speed | OBD | 18095 | 3.3s |  |
| throttle | OBD | 238 | 13.6s |  |
| voltage | OBD | 238 | 13.6s |  |

---
_Read-only analysis; no baselines across sessions claimed yet._
