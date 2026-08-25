# Validation run 20260825T194956Z-run

- **Command:** `run`  (`run mappings/candidates/bmw/dde/n47/d72n47a0_flow.yaml --all`)
- **When (UTC):** 20260825T194956Z
- **Read-only:** yes — every frame passed the service allowlist `0x1, 0x19, 0x22, 0x2c, 0x3e, 0x9`
- **Gateway:** 169.254.65.67  **Engine ECU:** 0x12 (ECM-EngineControl) (30 PIDs)

## n47.d72.dyn.461B

- **ECU:** 0x12
- **Outcome:** `decoded`

| # | dir | bytes | nrc | ms |
|---|---|---|---|---|
| 0 | tx→0x12 | `2c 03 f3 03` | - | 29.1 |
| 0 | rx | `6c 03 f3 03` | - | - |
| 1 | tx→0x12 | `2c 01 f3 03 46 1b 01 02` | - | 29.9 |
| 1 | rx | `6c 01 f3 03` | - | - |
| 2 | tx→0x12 | `22 f3 03` | - | 29.7 |
| 2 | rx | `62 f3 03 4c dc` | - | - |

**Decoded:**

- `n47d_coolant` = **96.76**

- [ ] FILL IN: does this value match the physical state? (e.g. oil ~ coolant when cold, soot measured ~ modelled, pressure rises with rpm)

## n47.d72.dyn.4841

- **ECU:** 0x12
- **Outcome:** `decoded`

| # | dir | bytes | nrc | ms |
|---|---|---|---|---|
| 0 | tx→0x12 | `2c 03 f3 03` | - | 29.9 |
| 0 | rx | `6c 03 f3 03` | - | - |
| 1 | tx→0x12 | `2c 01 f3 03 48 41 01 02` | - | 29.9 |
| 1 | rx | `6c 01 f3 03` | - | - |
| 2 | tx→0x12 | `22 f3 03` | - | 30.1 |
| 2 | rx | `62 f3 03 2c 75` | - | - |

**Decoded:**

- `n47d_boost_act` = **1042.0**

- [ ] FILL IN: does this value match the physical state? (e.g. oil ~ coolant when cold, soot measured ~ modelled, pressure rises with rpm)

## n47.d72.dyn.42C8

- **ECU:** 0x12
- **Outcome:** `decoded`

| # | dir | bytes | nrc | ms |
|---|---|---|---|---|
| 0 | tx→0x12 | `2c 03 f3 03` | - | 29.7 |
| 0 | rx | `6c 03 f3 03` | - | - |
| 1 | tx→0x12 | `2c 01 f3 03 42 c8 01 02` | - | 30.2 |
| 1 | rx | `6c 01 f3 03` | - | - |
| 2 | tx→0x12 | `22 f3 03` | - | 30.0 |
| 2 | rx | `62 f3 03 2d 70` | - | - |

**Decoded:**

- `n47d_boost_set` = **1065.0**

- [ ] FILL IN: does this value match the physical state? (e.g. oil ~ coolant when cold, soot measured ~ modelled, pressure rises with rpm)

## n47.d72.dyn.4746

- **ECU:** 0x12
- **Outcome:** `decoded`

| # | dir | bytes | nrc | ms |
|---|---|---|---|---|
| 0 | tx→0x12 | `2c 03 f3 03` | - | 29.8 |
| 0 | rx | `6c 03 f3 03` | - | - |
| 1 | tx→0x12 | `2c 01 f3 03 47 46 01 02` | - | 29.7 |
| 1 | rx | `6c 01 f3 03` | - | - |
| 2 | tx→0x12 | `22 f3 03` | - | 30.3 |
| 2 | rx | `62 f3 03 19 99` | - | - |

**Decoded:**

- `n47d_rail_act` = **300.0**

- [ ] FILL IN: does this value match the physical state? (e.g. oil ~ coolant when cold, soot measured ~ modelled, pressure rises with rpm)

## n47.d72.dyn.4715

- **ECU:** 0x12
- **Outcome:** `decoded`

| # | dir | bytes | nrc | ms |
|---|---|---|---|---|
| 0 | tx→0x12 | `2c 03 f3 03` | - | 29.5 |
| 0 | rx | `6c 03 f3 03` | - | - |
| 1 | tx→0x12 | `2c 01 f3 03 47 15 01 02` | - | 30.0 |
| 1 | rx | `6c 01 f3 03` | - | - |
| 2 | tx→0x12 | `22 f3 03` | - | 30.1 |
| 2 | rx | `62 f3 03 19 99` | - | - |

**Decoded:**

- `n47d_rail_set` = **300.0**

- [ ] FILL IN: does this value match the physical state? (e.g. oil ~ coolant when cold, soot measured ~ modelled, pressure rises with rpm)

## n47.d72.dyn.47DD

- **ECU:** 0x12
- **Outcome:** `decoded`

| # | dir | bytes | nrc | ms |
|---|---|---|---|---|
| 0 | tx→0x12 | `2c 03 f3 03` | - | 30.2 |
| 0 | rx | `6c 03 f3 03` | - | - |
| 1 | tx→0x12 | `2c 01 f3 03 47 dd 01 02` | - | 29.7 |
| 1 | rx | `6c 01 f3 03` | - | - |
| 2 | tx→0x12 | `22 f3 03` | - | 30.0 |
| 2 | rx | `62 f3 03 4e 59` | - | - |

**Decoded:**

- `n47d_maf_per_cyl` = **489.67**

- [ ] FILL IN: does this value match the physical state? (e.g. oil ~ coolant when cold, soot measured ~ modelled, pressure rises with rpm)

## n47.d72.dyn.4843

- **ECU:** 0x12
- **Outcome:** `decoded`

| # | dir | bytes | nrc | ms |
|---|---|---|---|---|
| 0 | tx→0x12 | `2c 03 f3 03` | - | 29.7 |
| 0 | rx | `6c 03 f3 03` | - | - |
| 1 | tx→0x12 | `2c 01 f3 03 48 43 01 02` | - | 29.8 |
| 1 | rx | `6c 01 f3 03` | - | - |
| 2 | tx→0x12 | `22 f3 03` | - | 30.0 |
| 2 | rx | `62 f3 03 3a 26` | - | - |

**Decoded:**

- `n47d_charge_air_temp` = **48.86**

- [ ] FILL IN: does this value match the physical state? (e.g. oil ~ coolant when cold, soot measured ~ modelled, pressure rises with rpm)

## n47.d72.dyn.4CF0

- **ECU:** 0x12
- **Outcome:** `decoded`

| # | dir | bytes | nrc | ms |
|---|---|---|---|---|
| 0 | tx→0x12 | `2c 03 f3 03` | - | 30.1 |
| 0 | rx | `6c 03 f3 03` | - | - |
| 1 | tx→0x12 | `2c 01 f3 03 4c f0 01 02` | - | 29.8 |
| 1 | rx | `6c 01 f3 03` | - | - |
| 2 | tx→0x12 | `22 f3 03` | - | 29.9 |
| 2 | rx | `62 f3 03 7f de` | - | - |

**Decoded:**

- `n47d_ambient_press` = **999.0**

- [ ] FILL IN: does this value match the physical state? (e.g. oil ~ coolant when cold, soot measured ~ modelled, pressure rises with rpm)

## n47.d72.dyn.4232

- **ECU:** 0x12
- **Outcome:** `decoded`

| # | dir | bytes | nrc | ms |
|---|---|---|---|---|
| 0 | tx→0x12 | `2c 03 f3 03` | - | 30.0 |
| 0 | rx | `6c 03 f3 03` | - | - |
| 1 | tx→0x12 | `2c 01 f3 03 42 32 01 02` | - | 30.2 |
| 1 | rx | `6c 01 f3 03` | - | - |
| 2 | tx→0x12 | `22 f3 03` | - | 29.9 |
| 2 | rx | `62 f3 03 00 00` | - | - |

**Decoded:**

- `n47d_pedal` = **0.0**

- [ ] FILL IN: does this value match the physical state? (e.g. oil ~ coolant when cold, soot measured ~ modelled, pressure rises with rpm)

## Promotion decision

For each decoded request, once the plausibility box above is
checked and true, edit the candidate mapping's
`verification.status` to `locally_verified` and record the
vehicle label `F10-520d-dev`. If a request returned a
negative response or an implausible value, set it `rejected`
with the NRC/reason. Nothing here is promoted automatically.

