# Session report — run 2

- ECU: 0x12 (ECM-EngineControl)  (addr 18)
- Duration: 41.8 min, 96852 samples across 46 channels
- Started (UTC): 2026-09-11T09:04:06Z
- Session: 01M27VBE11PBYQW3H5E3VA14VC
- Vehicle: F10-520d-dev — ⚠️ configuration is TODAY'S, not this run's

## Key findings

- Cold start captured from 38.0 °C; coolant reached 80 °C in 8.7 min and stabilised near 93.0 °C. Oil ran +0.3 °C against coolant through the ramp, so no lag was seen.
- Ambient/baro cross-check differs by only 5.79 hPa on average — that is the standard OBD baro PID's 1 kPa integer quantisation, i.e. agreement within resolution, not a discrepancy.
- Boost closed-loop control tracked its setpoint to 75.4 mean deviation (max 643.1) over 100.0% coverage within 1.0 s — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- Rail pressure closed-loop control tracked its setpoint to 24.4 mean deviation (max 286.7) over 99.5% coverage within 1.0 s — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- DPF soot 1.3–1.97 g is the **ECU's internal model**, not a measurement of a filter. VOID: this vehicle has no dpf - the channels still report, but they describe hardware that is not there. No filter-loading, restriction or differential-pressure health conclusion is drawn.
- DPF differential pressure reads -9.0–33.0 hPa across an **empty pipe**. VOID: this vehicle has no dpf - the channels still report, but they describe hardware that is not there. The values are real; they are not a restriction baseline and must not be trended as one.
- [CANDIDATE] Exhaust temp before DPF 53–276 °C, before catalyst 102–399 °C — should climb under load; pre-cat typically hotter than pre-DPF. Validates the exhaust-temp scales.
- [CANDIDATE] Distance since regen 38.0–60.8 km — should be a steady value increasing monotonically over the drive (unless a regen completes, resetting it).
- [CANDIDATE] EGR control deviation 0.0–110.1 % — should sit near 0 when the loop is happy; a persistent offset would flag EGR fouling. Baseline for EGR-health trending.
- Regeneration count 97–97. This remains meaningful with no filter fitted: the ECU still commands regens against its internal model, at a real cost in fuel and oil dilution, and they clean nothing. Trend the RATE, not the filter.
- [CANDIDATE] Operating-mode word took 2 distinct value(s) — bit 0x02 is the regeneration-active flag; a change mid-drive would mark a regeneration event.

## Cold-start warm-up

| channel | start | max | →80 °C | unit |
|---|---|---|---|---|
| coolant | 38.0 | 93.0 | 522s | °C |
| n47d_oil_temp | 38.2 | 92.9 | 518s | °C |
| n47d_engine_temp | 38.9 | 92.6 | 523s | °C |
| n47d_charge_air_temp | 32.9 | 45.5 | — | °C |

- Across the warm-up, oil ran **+0.26 °C** against coolant (mean of 49 matched pairs, range -1.10 to +1.30).
  The two track each other to within 1 °C — no lag either way.

## Proprietary DDE vs standard OBD (live cross-check)

| quantity | pairs | window | coverage | median gap | mean |Δ| | max |Δ| | agree |
|---|---|---|---|---|---|---|---|
| coolant °C | 205 | 15.0s | 100.0% | 2.571s | 0.44 | 2.06 | ✅ |
| manifold/boost (hPa vs kPa×10) | 205 | 1.0s | 100.0% | 0.1s | 8.61 | 105.0 | ⚠️ |
| ambient (hPa vs kPa×10) | 205 | 60.0s | 100.0% | 15.69s | 5.79 | 12.0 | ⚠️ |

`coverage` is the share of proprietary readings that had an OBD reading inside the window. A comparison below 50% is reported as insufficient rather than averaged: the number would describe the poll schedule, not the sensors.

## Drive / load behaviour

- max speed 138.0 km/h; 9222 driving / 6284 idle samples (speed>3 km/h = driving).

| loop | pairs | window | coverage | median gap | mean |dev| | max |dev| |
|---|---|---|---|---|---|---|
| boost (act−set) | 198 | 1.0s | 100.0% | 0.09s | 75.4 | 643.1 |
| rail pressure (act−set) | 196 | 1.0s | 99.5% | 0.09s | 24.4 | 286.7 |

| channel | min | max | mean | p95 |
|---|---|---|---|---|
| rpm | 0.0 | 3356.0 | 1157.023 | 1866.0 |
| map | 33.0 | 254.0 | 117.446 | 177.0 |
| n47d_boost_act | 835.0 | 2573.9 | 1194.331 | 2091.9 |
| n47d_rail_act | 13.3 | 1400.0 | 514.826 | 1063.6 |
| n47d_maf_per_cyl | 0.0 | 1398.36 | 533.684 | 1143.36 |
| n47d_pedal | 0.0 | 62.2 | 11.46 | 40.99 |
| load | 0.0 | 100.0 | 37.898 | 87.843 |
| speed | 0.0 | 138.0 | 33.237 | 94.0 |
| maf | 0.0 | 222.22 | 33.342 | 87.47 |
| rail | 13.3 | 1342.4 | 506.346 | 1003.8 |

## DPF

- soot measured: 1.3–1.97 g
- soot modelled: 1.3–1.98 g
- **VOID: this vehicle has no dpf - the channels still report, but they describe hardware that is not there**
- the soot figures above are the ECU's internal model, reported as that and nothing more

## Data quality / coverage

179 samples were flagged by the decoder and excluded from every statistic above - sentinels the ECU returned to mean "no value", sensors pinned on a rail, and values outside a declared range. They are counted per channel below rather than silently dropped.

| channel | src | samples | max gap | flagged | pinned@max |
|---|---|---|---|---|---|
| ambient | OBD | 39 | 60.4s |  |  |
| baro | OBD | 39 | 60.4s |  | 8 |
| boost | DDE | 15424 | 7.7s |  |  |
| cattemp | OBD | 238 | 10.5s |  |  |
| coolant | OBD | 238 | 10.5s |  |  |
| distance | OBD | 39 | 60.4s |  |  |
| egr | OBD | 238 | 10.5s |  |  |
| egrerr | OBD | 238 | 10.5s |  |  |
| gear | DDE | 4044 | 9.2s | clipped 49, stale 1 |  |
| iat | OBD | 238 | 10.5s |  |  |
| lambda | OBD | 193 | 251.6s | sentinel 45 |  |
| load | OBD | 2112 | 4.6s |  |  |
| maf | OBD | 2112 | 4.6s |  |  |
| map | OBD | 15424 | 7.8s | saturated 84 |  |
| n47d_ambient_press | DDE | 205 | 15.3s |  |  |
| n47d_boost_act | DDE | 205 | 15.3s |  |  |
| n47d_boost_set | DDE | 205 | 15.3s |  |  |
| n47d_charge_air_temp | DDE | 205 | 15.3s |  |  |
| n47d_converter_temp | DDE | 203 | 27.0s |  |  |
| n47d_coolant | DDE | 205 | 15.3s |  |  |
| n47d_dist_since_regen | DDE | 2112 | 4.6s |  |  |
| n47d_dpf_dp | DDE | 205 | 15.3s |  |  |
| n47d_egr_deviation | DDE | 205 | 15.2s |  |  |
| n47d_engine_temp | DDE | 203 | 27.0s |  |  |
| n47d_exh_temp_pre_cat | DDE | 204 | 15.0s |  |  |
| n47d_exh_temp_pre_dpf | DDE | 205 | 15.3s |  |  |
| n47d_gbx_oil_temp | DDE | 205 | 15.3s |  |  |
| n47d_maf_per_cyl | DDE | 205 | 15.2s |  |  |
| n47d_odometer_m | DDE | 2112 | 4.6s |  |  |
| n47d_oil_temp | DDE | 205 | 15.3s |  |  |
| n47d_opmode | DDE | 205 | 15.2s |  |  |
| n47d_pedal | DDE | 205 | 15.3s |  |  |
| n47d_rail_act | DDE | 204 | 15.2s |  |  |
| n47d_rail_set | DDE | 203 | 27.3s |  |  |
| n47d_regen_count | DDE | 204 | 15.1s |  | 204 |
| n47d_soot_meas | DDE | 205 | 15.3s |  |  |
| n47d_soot_model | DDE | 204 | 15.3s |  |  |
| n47d_turbine_speed | DDE | 205 | 15.3s |  |  |
| pedal | OBD | 15508 | 3.8s |  |  |
| rail | OBD | 238 | 10.5s |  |  |
| relthr | OBD | 238 | 10.5s |  |  |
| rpm | OBD | 15507 | 3.8s |  |  |
| runtime | OBD | 39 | 60.4s |  |  |
| speed | OBD | 15506 | 3.8s |  |  |
| throttle | OBD | 238 | 10.5s |  |  |
| voltage | OBD | 238 | 10.5s |  |  |

`flagged` is declared by the mapping and is authoritative. `pinned@max` is a heuristic over what remains, kept to surface saturation nobody has declared **yet** - a lead to investigate, not a finding.

---
_Read-only analysis; no baselines across sessions claimed yet._
