# Session report — run 3

- ECU: 0x12 (ECM-EngineControl)  (addr 18)
- Duration: 14.1 min, 100737 samples across 46 channels
- Started (UTC): 2026-08-29T18:09:29Z

## Key findings

- Cold start captured from 88.0 °C; coolant reached 80 °C in 0.0 min and stabilised near 91.0 °C. Oil and engine temp tracked it closely — a healthy warm-up with no lag anomaly.
- **OBD MAP saturates at 255 kPa**; under boost the DDE reads the true manifold pressure up to 279 kPa. The boost cross-check ⚠️ is OBD sensor saturation, NOT a decode error — above 255 kPa the DDE boost channel is the accurate one. (Exactly the 'generic OBD saturation' caveat the project set out to handle.)
- Ambient/baro cross-check differs by only 3.72 hPa on average — that is the standard OBD baro PID's 1 kPa integer quantisation, i.e. agreement within resolution, not a discrepancy.
- Lambda sat at the 2.0 sentinel for 5368/7574 samples (= 'no value', not a real λ of 2.0); exclude those from any AFR analysis.
- Boost closed-loop control tracked its setpoint to 145.1 mean deviation (max 1200.0) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- Rail pressure closed-loop control tracked its setpoint to 111.4 mean deviation (max 722.7) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- DPF soot measured vs modelled agree to 0.008 g (range 8.9–9.16 g) — differential-pressure sensing is healthy; this is a baseline to trend soot-accumulation rate against.
- [CANDIDATE] DPF differential pressure -3.0–107.0 hPa — should read low warm-idle and rise with exhaust flow under load; a plausible spread validates the 0x44F8 scale. (Baseline for filter-restriction trending.)
- [CANDIDATE] Exhaust temp before DPF 169–440 °C, before catalyst 185–570 °C — should climb under load; pre-cat typically hotter than pre-DPF. Validates the exhaust-temp scales.
- [CANDIDATE] Distance since regen 22.3–35.1 km — should be a steady value increasing monotonically over the drive (unless a regen completes, resetting it).
- [CANDIDATE] EGR control deviation 0.0–0.0 % — should sit near 0 when the loop is happy; a persistent offset would flag EGR fouling. Baseline for EGR-health trending.
- [CANDIDATE] Operating-mode word took 1 distinct value(s) — bit 0x02 is the regeneration-active flag; a change mid-drive would mark a regeneration event.

## Cold-start warm-up

| channel | start | max | →80 °C | unit |
|---|---|---|---|---|
| coolant | 88.0 | 91.0 | 0s | °C |
| n47d_oil_temp | 88.4 | 92.4 | 1s | °C |
| n47d_engine_temp | 88.1 | 92.2 | 8s | °C |
| n47d_charge_air_temp | 44.4 | 62.1 | — | °C |

- When coolant reached 80 °C, oil was **88.4 °C** (oil lags coolant — the expected warm-up signature).

## Proprietary DDE vs standard OBD (live cross-check)

| quantity | pairs | mean |Δ| | max |Δ| | agree |
|---|---|---|---|---|
| coolant °C | 66 | 0.47 | 1.64 | ✅ |
| manifold/boost (hPa vs kPa×10) | 66 | 29.43 | 236.0 | ⚠️ |
| ambient (hPa vs kPa×10) | 65 | 3.72 | 9.0 | ⚠️ |

## Drive / load behaviour

- max speed 204.0 km/h; 5274 driving / 2300 idle samples (speed>3 km/h = driving).

| loop | pairs | mean |dev| | max |dev| |
|---|---|---|---|
| boost (act−set) | 66 | 145.1 | 1200.0 |
| rail pressure (act−set) | 66 | 111.4 | 722.7 |

| channel | min | max | mean | p95 |
|---|---|---|---|---|
| rpm | 731.0 | 4005.0 | 1481.463 | 3072.0 |
| map | 105.0 | 255.0 | 140.216 | 255.0 |
| n47d_boost_act | 1053.0 | 2786.0 | 1418.739 | 2702.9 |
| n47d_rail_act | 288.1 | 1764.5 | 656.102 | 1609.2 |
| n47d_maf_per_cyl | 458.37 | 1279.66 | 695.32 | 1274.75 |
| n47d_pedal | 0.0 | 100.01 | 18.37 | 94.13 |
| load | 0.0 | 100.0 | 39.2 | 99.608 |
| speed | 0.0 | 204.0 | 54.955 | 193.0 |
| maf | 9.11 | 213.66 | 49.583 | 166.94 |
| rail | 233.2 | 1819.3 | 660.599 | 1613.1 |

## DPF

- soot measured: 8.9–9.16 g
- soot modelled: 8.9–9.16 g
- measured vs modelled mean |Δ|: 0.008 g (the two independent estimates should agree)

## Data quality / coverage

| channel | src | samples | max gap | pinned@max |
|---|---|---|---|---|
| ambient | OBD | 76 | 11.3s | 20 |
| baro | OBD | 76 | 11.3s | 76 |
| boost | DDE | 7574 | 0.3s |  |
| cattemp | OBD | 76 | 11.3s |  |
| coolant | OBD | 76 | 11.3s |  |
| distance | OBD | 76 | 11.3s |  |
| egr | OBD | 76 | 11.3s | 76 |
| egrerr | OBD | 76 | 11.3s | 76 |
| egs_da2e_b0 | DDE | 7574 | 0.3s | 7574 |
| gear | DDE | 7574 | 0.3s |  |
| iat | OBD | 76 | 11.3s |  |
| lambda | OBD | 7574 | 0.3s | 5368 |
| load | OBD | 7574 | 0.3s |  |
| maf | OBD | 7574 | 0.3s |  |
| map | OBD | 7574 | 0.3s |  |
| n47d_ambient_press | DDE | 65 | 13.0s |  |
| n47d_boost_act | DDE | 66 | 13.0s |  |
| n47d_boost_set | DDE | 66 | 13.0s |  |
| n47d_charge_air_temp | DDE | 66 | 13.0s |  |
| n47d_converter_temp | DDE | 66 | 13.0s | 25 |
| n47d_coolant | DDE | 66 | 13.0s |  |
| n47d_dist_since_regen | DDE | 66 | 13.0s |  |
| n47d_dpf_dp | DDE | 66 | 13.0s |  |
| n47d_egr_deviation | DDE | 65 | 13.0s | 65 |
| n47d_engine_temp | DDE | 66 | 13.0s |  |
| n47d_exh_temp_pre_cat | DDE | 66 | 13.0s |  |
| n47d_exh_temp_pre_dpf | DDE | 66 | 13.0s |  |
| n47d_gbx_oil_temp | DDE | 66 | 13.0s |  |
| n47d_maf_per_cyl | DDE | 66 | 13.0s |  |
| n47d_oil_temp | DDE | 66 | 13.0s |  |
| n47d_opmode | DDE | 66 | 13.0s | 66 |
| n47d_pedal | DDE | 65 | 13.0s |  |
| n47d_rail_act | DDE | 66 | 13.0s |  |
| n47d_rail_set | DDE | 66 | 13.0s |  |
| n47d_regen_count | DDE | 66 | 13.0s | 66 |
| n47d_soot_meas | DDE | 66 | 13.0s |  |
| n47d_soot_model | DDE | 66 | 13.0s |  |
| n47d_turbine_speed | DDE | 66 | 13.0s |  |
| pedal | OBD | 7574 | 0.3s |  |
| rail | OBD | 7574 | 0.3s |  |
| relthr | OBD | 7574 | 0.3s |  |
| rpm | OBD | 7574 | 0.3s |  |
| runtime | OBD | 76 | 11.3s |  |
| speed | OBD | 7574 | 0.3s |  |
| throttle | OBD | 7574 | 0.3s |  |
| voltage | OBD | 76 | 11.3s |  |

---
_Read-only analysis; no baselines across sessions claimed yet._
