# Validation run 20260825T192513Z-identify

- **Command:** `identify`  (`identify`)
- **When (UTC):** 20260825T192513Z
- **Read-only:** yes — every frame passed the service allowlist `0x1, 0x19, 0x22, 0x2c, 0x3e, 0x9`
- **Gateway:** 169.254.65.67  **Engine ECU:** 0x12 (ECM-EngineControl) (30 PIDs)

## identify

Read-only identification:

- `vin` = <redacted: recorded in local/ only>
- `sysname_f197` = <redacted: recorded in local/ only>
- `hw_f191` = (NRC/negative response to 0x22: NRC 0x31)
- `sw_f194` = (NRC/negative response to 0x22: NRC 0x31)
- `supplier_f18a` = <redacted: recorded in local/ only>
- `ecu_name_0900` = ECM-EngineControl

> compare hw/sw/supplier against the d_motor IDENT results in an offline EDIABAS/ediabasx oracle to resolve the exact DDE SGBD variant

## Promotion decision

For each decoded request, once the plausibility box above is
checked and true, edit the candidate mapping's
`verification.status` to `locally_verified` and record the
vehicle label `F10-520d-dev`. If a request returned a
negative response or an implausible value, set it `rejected`
with the NRC/reason. Nothing here is promoted automatically.

