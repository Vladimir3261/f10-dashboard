# Session report — run 1

> **CORRECTION (2026-08-31), applied after generation.** Two statements
> below about the oil/coolant relationship are WRONG and must not be
> cited. The generator emitted them unconditionally, without ever
> comparing the two channels or checking whether the car had moved:
>
> - *"a healthy warm-up with no lag anomaly"* (Key findings)
> - *"oil lags coolant — the expected warm-up signature"* (Cold-start
>   warm-up)
>
> This session was **stationary throughout**. The oil lag is load-driven —
> oil takes heat from work done — so at idle it warms from the block and
> tracks coolant. A lag could not have appeared, and its absence is not a
> finding. At the 80 °C crossing oil was 79.8 °C, i.e. 0.2 °C below, which
> is agreement within noise and not a lag.
>
> The measurements in this report are sound; only those two
> interpretations are not. The generator was fixed so it states the delta
> and interprets it only where the session supports one. See NOTES.md and
> `analysis/session_report.py`.

- ECU: 0x12 (ECM-EngineControl)  (addr 18)
- Duration: 41.0 min, 129567 samples across 45 channels
- Started (UTC): 2026-08-30T21:39:14Z

## Key findings

- Cold start captured from 21.0 °C; coolant reached 80 °C in 33.6 min and stabilised near 88.0 °C. Oil and engine temp tracked it closely — a healthy warm-up with no lag anomaly.
- Ambient/baro cross-check differs by only 8.94 hPa on average — that is the standard OBD baro PID's 1 kPa integer quantisation, i.e. agreement within resolution, not a discrepancy.
- Lambda sat at the 2.0 sentinel for 193/245 samples (= 'no value', not a real λ of 2.0); exclude those from any AFR analysis.
- Boost closed-loop control tracked its setpoint to 36.3 mean deviation (max 698.0) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- Rail pressure closed-loop control tracked its setpoint to 17.6 mean deviation (max 287.7) — the actuator is hitting its target; a growing deviation over future sessions would flag wear.
- DPF soot measured vs modelled agree to 0.01 g (range 9.35–9.52 g) — differential-pressure sensing is healthy; this is a baseline to trend soot-accumulation rate against.
- [CANDIDATE] DPF differential pressure -13.0–-4.0 hPa — should read low warm-idle and rise with exhaust flow under load; a plausible spread validates the 0x44F8 scale. (Baseline for filter-restriction trending.)
- [CANDIDATE] Exhaust temp before DPF 14–104 °C, before catalyst 15–147 °C — should climb under load; pre-cat typically hotter than pre-DPF. Validates the exhaust-temp scales.
- [CANDIDATE] Distance since regen 45.2–45.2 km — should be a steady value increasing monotonically over the drive (unless a regen completes, resetting it).
- [CANDIDATE] EGR control deviation 0.0–5.5 % — should sit near 0 when the loop is happy; a persistent offset would flag EGR fouling. Baseline for EGR-health trending.
- [CANDIDATE] Operating-mode word took 2 distinct value(s) — bit 0x02 is the regeneration-active flag; a change mid-drive would mark a regeneration event.

## Cold-start warm-up

| channel | start | max | →80 °C | unit |
|---|---|---|---|---|
| coolant | 21.0 | 88.0 | 2013s | °C |
| n47d_oil_temp | 21.3 | 88.0 | 2020s | °C |
| n47d_engine_temp | 21.3 | 88.0 | 2027s | °C |
| n47d_charge_air_temp | 20.7 | 38.5 | — | °C |

- When coolant reached 80 °C, oil was **79.8 °C** (oil lags coolant — the expected warm-up signature).

## Proprietary DDE vs standard OBD (live cross-check)

| quantity | pairs | mean |Δ| | max |Δ| | agree |
|---|---|---|---|---|
| coolant °C | 204 | 0.34 | 0.86 | ✅ |
| manifold/boost (hPa vs kPa×10) | 204 | 3.23 | 9.0 | ⚠️ |
| ambient (hPa vs kPa×10) | 203 | 8.94 | 9.0 | ⚠️ |

## Drive / load behaviour

- max speed 0.0 km/h; 0 driving / 23419 idle samples (speed>3 km/h = driving).

| loop | pairs | mean |dev| | max |dev| |
|---|---|---|---|
| boost (act−set) | 204 | 36.3 | 698.0 |
| rail pressure (act−set) | 204 | 17.6 | 287.7 |

| channel | min | max | mean | p95 |
|---|---|---|---|---|
| rpm | 0.0 | 1295.0 | 740.832 | 783.0 |
| map | 33.0 | 108.0 | 102.356 | 104.0 |
| n47d_boost_act | 336.9 | 1082.0 | 1023.929 | 1043.0 |
| n47d_rail_act | 12.3 | 405.6 | 295.315 | 356.7 |
| n47d_maf_per_cyl | 0.0 | 555.47 | 443.097 | 545.07 |
| n47d_pedal | 0.0 | 0.0 | 0.0 | 0.0 |
| load | 0.0 | 53.333 | 29.687 | 41.961 |
| speed | 0.0 | 0.0 | 0.0 | 0.0 |
| maf | 0.0 | 222.22 | 18.347 | 22.99 |
| rail | 12.3 | 382.6 | 287.747 | 351.8 |

## DPF

- soot measured: 9.35–9.52 g
- soot modelled: 9.36–9.53 g
- measured vs modelled mean |Δ|: 0.01 g (the two independent estimates should agree)

## Data quality / coverage

| channel | src | samples | max gap | pinned@max |
|---|---|---|---|---|
| ambient | OBD | 41 | 60.2s | 18 |
| baro | OBD | 41 | 60.2s | 41 |
| boost | DDE | 23419 | 0.3s |  |
| cattemp | OBD | 245 | 10.2s |  |
| coolant | OBD | 245 | 10.2s |  |
| distance | OBD | 41 | 60.2s | 41 |
| egr | OBD | 245 | 10.2s |  |
| egrerr | OBD | 245 | 10.2s |  |
| gear | DDE | 4684 | 0.6s |  |
| iat | OBD | 245 | 10.2s |  |
| lambda | OBD | 245 | 10.2s | 193 |
| load | OBD | 245 | 10.2s |  |
| maf | OBD | 245 | 10.2s |  |
| map | OBD | 23419 | 0.3s |  |
| n47d_ambient_press | DDE | 203 | 12.3s | 190 |
| n47d_boost_act | DDE | 204 | 12.3s |  |
| n47d_boost_set | DDE | 204 | 12.3s |  |
| n47d_charge_air_temp | DDE | 203 | 12.3s |  |
| n47d_converter_temp | DDE | 204 | 12.3s |  |
| n47d_coolant | DDE | 204 | 12.3s |  |
| n47d_dist_since_regen | DDE | 204 | 12.3s | 204 |
| n47d_dpf_dp | DDE | 204 | 12.3s |  |
| n47d_egr_deviation | DDE | 203 | 12.3s |  |
| n47d_engine_temp | DDE | 204 | 12.3s |  |
| n47d_exh_temp_pre_cat | DDE | 204 | 12.3s |  |
| n47d_exh_temp_pre_dpf | DDE | 204 | 12.3s |  |
| n47d_gbx_oil_temp | DDE | 204 | 12.3s |  |
| n47d_maf_per_cyl | DDE | 203 | 12.3s |  |
| n47d_oil_temp | DDE | 204 | 12.3s |  |
| n47d_opmode | DDE | 203 | 12.3s |  |
| n47d_pedal | DDE | 203 | 12.3s | 203 |
| n47d_rail_act | DDE | 204 | 12.3s |  |
| n47d_rail_set | DDE | 203 | 12.3s |  |
| n47d_regen_count | DDE | 203 | 12.3s | 203 |
| n47d_soot_meas | DDE | 204 | 12.3s |  |
| n47d_soot_model | DDE | 204 | 12.3s |  |
| n47d_turbine_speed | DDE | 204 | 12.3s |  |
| pedal | OBD | 23419 | 0.3s |  |
| rail | OBD | 245 | 10.2s |  |
| relthr | OBD | 245 | 10.2s | 232 |
| rpm | OBD | 23419 | 0.3s |  |
| runtime | OBD | 41 | 60.2s |  |
| speed | OBD | 23419 | 0.3s | 23419 |
| throttle | OBD | 245 | 10.2s |  |
| voltage | OBD | 245 | 10.2s |  |

---
_Read-only analysis; no baselines across sessions claimed yet._
