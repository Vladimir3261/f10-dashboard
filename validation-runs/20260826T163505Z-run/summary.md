# Validation run 20260826T163505Z-run

- **Command:** `run`  (`run mappings/candidates/bmw/kombi/f10_gear.yaml --all`)
- **When (UTC):** 20260826T163505Z
- **Read-only:** yes — every frame passed the service allowlist `0x1, 0x19, 0x22, 0x2c, 0x3e, 0x9`
- **Gateway:** 169.254.65.67  **Engine ECU:** 0x12 (ECM-EngineControl) (30 PIDs)

## kombi.gear.D031

- **ECU:** 0x63
- **Outcome:** `decoded`

| # | dir | bytes | nrc | ms |
|---|---|---|---|---|
| 0 | tx→0x63 | `22 d0 31` | - | 17.0 |
| 0 | rx | `62 d0 31 02 00` | - | - |

**Decoded:**

- `park_state` = **2.0**
- `gear_b1` = **0.0**

- [ ] FILL IN: does this value match the physical state? (e.g. oil ~ coolant when cold, soot measured ~ modelled, pressure rises with rpm)

## Promotion decision

For each decoded request, once the plausibility box above is
checked and true, edit the candidate mapping's
`verification.status` to `locally_verified` and record the
vehicle label `F10-520d-dev`. If a request returned a
negative response or an implausible value, set it `rejected`
with the NRC/reason. Nothing here is promoted automatically.

