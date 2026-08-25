# Validation run 20260825T192203Z-run

- **Command:** `run`  (`run mappings/candidates/bmw/dde/n47/d72n47a0_dynamic.yaml --all --step`)
- **When (UTC):** 20260825T192203Z
- **Read-only:** yes — every frame passed the service allowlist `0x1, 0x19, 0x22, 0x2c, 0x3e, 0x9`
- **Gateway:** 169.254.65.67  **Engine ECU:** 0x12 (ECM-EngineControl) (30 PIDs)

## n47.d72.dyn.4517

- **ECU:** 0x12
- **Outcome:** `decoded`

| # | dir | bytes | nrc | ms |
|---|---|---|---|---|
| 0 | tx→0x12 | `2c 03 f3 03` | - | 29.8 |
| 0 | rx | `6c 03 f3 03` | - | - |
| 1 | tx→0x12 | `2c 01 f3 03 45 17 01 02` | - | 28.8 |
| 1 | rx | `6c 01 f3 03` | - | - |
| 2 | tx→0x12 | `22 f3 03` | - | 30.5 |
| 2 | rx | `62 f3 03 44 a2` | - | - |

**Decoded:**

- `n47d_oil_temp` = **75.7**

- [ ] FILL IN: does this value match the physical state? (e.g. oil ~ coolant when cold, soot measured ~ modelled, pressure rises with rpm)

## n47.d72.dyn.44BE

- **ECU:** 0x12
- **Outcome:** `decoded`

| # | dir | bytes | nrc | ms |
|---|---|---|---|---|
| 0 | tx→0x12 | `2c 03 f3 03` | - | 26.5 |
| 0 | rx | `6c 03 f3 03` | - | - |
| 1 | tx→0x12 | `2c 01 f3 03 44 be 01 02` | - | 30.0 |
| 1 | rx | `6c 01 f3 03` | - | - |
| 2 | tx→0x12 | `22 f3 03` | - | 30.1 |
| 2 | rx | `62 f3 03 00 06` | - | - |

**Decoded:**

- `n47d_soot_meas` = **0.09**

- [ ] FILL IN: does this value match the physical state? (e.g. oil ~ coolant when cold, soot measured ~ modelled, pressure rises with rpm)

## n47.d72.dyn.44C1

- **ECU:** 0x12
- **Outcome:** `decoded`

| # | dir | bytes | nrc | ms |
|---|---|---|---|---|
| 0 | tx→0x12 | `2c 03 f3 03` | - | 27.5 |
| 0 | rx | `6c 03 f3 03` | - | - |
| 1 | tx→0x12 | `2c 01 f3 03 44 c1 01 02` | - | 28.8 |
| 1 | rx | `6c 01 f3 03` | - | - |
| 2 | tx→0x12 | `22 f3 03` | - | 30.0 |
| 2 | rx | `62 f3 03 00 0a` | - | - |

**Decoded:**

- `n47d_soot_model` = **0.1**

- [ ] FILL IN: does this value match the physical state? (e.g. oil ~ coolant when cold, soot measured ~ modelled, pressure rises with rpm)

## n47.d72.dyn.4BC3

- **ECU:** 0x12
- **Outcome:** `decoded`

| # | dir | bytes | nrc | ms |
|---|---|---|---|---|
| 0 | tx→0x12 | `2c 03 f3 03` | - | 26.7 |
| 0 | rx | `6c 03 f3 03` | - | - |
| 1 | tx→0x12 | `2c 01 f3 03 4b c3 01 02` | - | 29.8 |
| 1 | rx | `6c 01 f3 03` | - | - |
| 2 | tx→0x12 | `22 f3 03` | - | 30.0 |
| 2 | rx | `62 f3 03 0d 90` | - | - |

**Decoded:**

- `n47d_engine_temp` = **74.06**

- [ ] FILL IN: does this value match the physical state? (e.g. oil ~ coolant when cold, soot measured ~ modelled, pressure rises with rpm)

## Promotion decision

For each decoded request, once the plausibility box above is
checked and true, edit the candidate mapping's
`verification.status` to `locally_verified` and record the
vehicle label `F10-520d-dev`. If a request returned a
negative response or an implausible value, set it `rejected`
with the NRC/reason. Nothing here is promoted automatically.

