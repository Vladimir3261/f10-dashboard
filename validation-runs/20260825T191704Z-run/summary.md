# Validation run 20260825T191704Z-run

- **Command:** `run`  (`run mappings/candidates/bmw/dde/n47/d72n47a0_dynamic.yaml n47.d72.dyn.4517`)
- **When (UTC):** 20260825T191704Z
- **Read-only:** yes — every frame passed the service allowlist `0x1, 0x19, 0x22, 0x2c, 0x3e, 0x9`
- **Gateway:** 169.254.65.67  **Engine ECU:** 0x12 (ECM-EngineControl) (30 PIDs)

## n47.d72.dyn.4517

- **ECU:** 0x12
- **Outcome:** `decoded`

| # | dir | bytes | nrc | ms |
|---|---|---|---|---|
| 0 | tx→0x12 | `2c 03 f3 03` | - | 29.7 |
| 0 | rx | `6c 03 f3 03` | - | - |
| 1 | tx→0x12 | `2c 01 f3 03 45 17 01 02` | - | 30.2 |
| 1 | rx | `6c 01 f3 03` | - | - |
| 2 | tx→0x12 | `22 f3 03` | - | 29.8 |
| 2 | rx | `62 f3 03 45 24` | - | - |

**Decoded:**

- `n47d_oil_temp` = **77.0**

- [ ] FILL IN: does this value match the physical state? (e.g. oil ~ coolant when cold, soot measured ~ modelled, pressure rises with rpm)

## Promotion decision

For each decoded request, once the plausibility box above is
checked and true, edit the candidate mapping's
`verification.status` to `locally_verified` and record the
vehicle label `F10-520d-dev`. If a request returned a
negative response or an implausible value, set it `rejected`
with the NRC/reason. Nothing here is promoted automatically.

