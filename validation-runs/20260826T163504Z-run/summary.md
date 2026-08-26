# Validation run 20260826T163504Z-run

- **Command:** `run`  (`run mappings/candidates/bmw/egs/f10_transmission.yaml --all`)
- **When (UTC):** 20260826T163504Z
- **Read-only:** yes — every frame passed the service allowlist `0x1, 0x19, 0x22, 0x2c, 0x3e, 0x9`
- **Gateway:** 169.254.65.67  **Engine ECU:** 0x12 (ECM-EngineControl) (30 PIDs)

## egs.selector.DA2E

- **ECU:** 0x18
- **Outcome:** `decoded`

| # | dir | bytes | nrc | ms |
|---|---|---|---|---|
| 0 | tx→0x18 | `22 da 2e` | - | 9.1 |
| 0 | rx | `62 da 2e 00 01` | - | - |

**Decoded:**

- `gear_selector` = **0.0**
- `drive_mode` = **1.0**

- [ ] FILL IN: does this value match the physical state? (e.g. oil ~ coolant when cold, soot measured ~ modelled, pressure rises with rpm)

## Promotion decision

For each decoded request, once the plausibility box above is
checked and true, edit the candidate mapping's
`verification.status` to `locally_verified` and record the
vehicle label `F10-520d-dev`. If a request returned a
negative response or an implausible value, set it `rejected`
with the NRC/reason. Nothing here is promoted automatically.

