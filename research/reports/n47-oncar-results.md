# N47 on-car validation results — F10-520d-dev

First on-car validation session, 2026-08-25, warm idle. Read-only
throughout (`tools/validate_candidate.py`, service allowlist enforced).
Raw artifacts: `validation-runs/`; unredacted copies (VIN) under the
gitignored `local/validation-runs-raw/`.

## Headline

**The F-series dynamic `0xF303` measurement path works on the target
F10, and every decoded value is physically correct.** The exact DDE SGBD
variant was not resolvable from an ident DID (F191/F194/F197/F18A all
return NRC 0x31 on this ECU), but it is resolved *behaviourally*: the
`2C 03 F3 03` / `2C 01 F3 03 …` / `22 F3 03` sequence is **accepted**
(not `7F 22 31`), which is the F-series UDS d72-family behaviour — and
three channels decode to the same value as the standard OBD PID for the
same quantity.

## Environment

- Vehicle: F10-520d-dev (VIN matches; in `local/VEHICLES.md`).
- Engine ECU: `0x12` (ECM-EngineControl), 30 OBD PIDs, found by
  functional broadcast — by capability, never by assumed address.
- Gateway: link-local, ENET/HSFZ. Round-trip latencies 27–31 ms (real
  wire, vs the simulator's 0 ms).

## The decisive cross-check — DDE F303 vs standard OBD Mode 01

Same engine ECU, two independent decode paths, same moment, warm idle:

| quantity | standard OBD | DDE `0xF303` | agree |
|---|---|---|---|
| coolant | 82 °C (PID 0x05) | 82.4 °C (ITKUM 0x461B) | ✅ |
| baro / ambient | 99 kPa (PID 0x33) | 99.9 kPa (IPUMG 0x4CF0) | ✅ |
| manifold / boost | 103 kPa (PID 0x0B) | 103.7 kPa (IPLAD 0x4841) | ✅ |
| rpm | 779 (PID 0x0C) | — (sanity: idle) | — |

Agreement within sensor/timing noise means the SG_FUNKTIONEN scales are
correct — this is the strongest validation short of a reference tool.

## All measured channels (warm idle)

| signal | id | raw | decoded | plausibility |
|---|---|---|---|---|
| oil temp | 0x4517 | 0x44A2 / 0x445C | 75.7 / 75.0 °C | warm idle ✓ |
| DPF soot measured | 0x44BE | 0x0006 | 0.09 g | clean/regenerated ✓ |
| DPF soot modelled | 0x44C1 | 0x000A | 0.10 g | ≈ measured ✓ (physics) |
| engine temp | 0x4BC3 | 0x0D90 / 0x0D9B | 74.1 / 75.2 °C | ≈ oil ≈ coolant ✓ |
| coolant | 0x461B | 0x46A6 | 80.9–82.4 °C | = OBD 0x05 ✓ |
| boost actual | 0x4841 | 0x2C33 | 1035.9 hPa | = atmospheric (no boost at idle) ✓ |
| boost setpoint | 0x42C8 | 0x2D7B | 1066.0 hPa | ≈ atmospheric ✓ |
| rail actual | 0x4746 | 0x19AD | 300.9 bar | textbook diesel idle ✓ |
| rail setpoint | 0x4715 | 0x1999 | 300.0 bar | ≈ actual (closed loop) ✓ |
| air mass / cyl | 0x47DD | 0x4F22 | 494.6 mg/hub | plausible idle charge ✓ |
| charge-air temp | 0x4843 | 0x37D8 | 43.0 °C | warm engine bay ✓ |
| ambient pressure | 0x4CF0 | 0x7FDE | 999.0 hPa | = atmospheric ✓ (= OBD 0x33) |
| pedal (filtered) | 0x4232 | 0x0000 | 0.0 % | foot off ✓ |

Independent anchors, not just "looks about right": ambient pressure must
be ~1013 hPa (read 999); pedal must be 0 with foot off (read 0.00);
rail actual must track setpoint under closed-loop control (300.9 vs
300.0); the two DPF soot estimates must nearly agree (0.09 vs 0.10);
and three channels match an independent standard PID.

## Throttle sweep — scales hold across the operating range

A 25 s `sweep` of the four load channels while blipping the throttle in
Neutral (`validation-runs/20260825T195407Z-sweep`, 70 rounds). Every
channel moved to a physically-correct loaded value:

| channel | idle/min | loaded max | note |
|---|---|---|---|
| pedal (0x4232) | 0.0 % | 18.9 % | tracks the foot |
| rail actual (0x4746) | 264 bar | 648 bar | rises with demand; +0.43 corr. with pedal |
| air mass/cyl (0x47DD) | 247 mg/hub | 626 mg/hub | airflow ~2.5× |
| boost actual (0x4841) | 1021 hPa | 1249 hPa | ~0.25 bar built |

This confirms the SG_FUNKTIONEN scales across the range, not just at the
single idle point. Instantaneous pedal↔boost/MAF correlation is weak,
which is expected and not a mapping issue: the tool polls the four
channels round-robin (~270 ms apart, so samples are not co-temporal),
turbochargers lag pedal, and revving in Neutral builds little boost (no
engine load demanding air). Rail, which responds fastest to demand,
shows the clearest positive correlation. The peak *values* are all
correct; only the timing alignment is washed out by the sampling method.

## Promotions

`mappings/candidates/bmw/dde/n47/d72n47a0_dynamic.yaml` (4 channels) and
`…/d72n47a0_flow.yaml` (9 channels) → `verification.status: verified`,
vehicle `F10-520d-dev`. They remain `production: false`: wiring them into
the live runtime needs a variant-aware capability provider (the
`sgbd_variant` match), which is a separate integration step — see below.

> **Superseded (2026-09-05):** the runtime integration below is done —
> `run_car.sh` loads these files via `--extra-mappings`, and the match is
> now `diagnostic_profile` (compatibility, proven by a nominated probe)
> rather than the retired `sgbd_variant`; the exact SGBD stays `unknown`
> until identity evidence exists. See `docs/MAPPING_ARCHITECTURE.md`.

## What is NOT yet done

- **Runtime integration.** These are verified but not yet polled by
  `live.py`. That needs a capability provider that confirms the DDE
  variant on connect and satisfies the `sgbd_variant` match; until then
  the candidates resolve to zero requests in the vehicle runtime by
  design. This is the obvious next build.
- **The other candidate families.** `dde7_kwp_local_id.yaml` (E-series
  KWP) and `f10_static_58xx.yaml` (N55 static) were not run — the F303
  path covers this car; those remain for their own chassis/engine.
- **DPF under load / a regeneration event**, transmission values via the
  DDE (0x0604–0x0608 exist in the D73 table; not yet built for d72), and
  a cold-start temperature ramp — all future sessions.
