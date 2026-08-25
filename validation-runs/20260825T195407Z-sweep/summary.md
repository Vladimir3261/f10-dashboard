# Validation run 20260825T195407Z-sweep

- **Command:** `sweep`  (`sweep mappings/candidates/bmw/dde/n47/d72n47a0_flow.yaml n47.d72.dyn.4746 n47.d72.dyn.4841 n47.d72.dyn.47DD n47.d72.dyn.4232 --seconds 25`)
- **When (UTC):** 20260825T195407Z
- **Read-only:** yes — every frame passed the service allowlist `0x1, 0x19, 0x22, 0x2c, 0x3e, 0x9`
- **Gateway:** 169.254.65.67  **Engine ECU:** 0x12 (ECM-EngineControl) (30 PIDs)

## n47d_boost_act

- range **1020.9 → 1249.0** hPa (span 228.1, last 1048.9, 70 samples)
- [ ] moved as expected under throttle? (boost/rail/MAF rise, pedal tracks foot)

## n47d_rail_act

- range **264.1 → 648.5** bar (span 384.4, last 582.6, 70 samples)
- [ ] moved as expected under throttle? (boost/rail/MAF rise, pedal tracks foot)

## n47d_maf_per_cyl

- range **246.68 → 625.88** mg/hub (span 379.2, last 291.48, 70 samples)
- [ ] moved as expected under throttle? (boost/rail/MAF rise, pedal tracks foot)

## n47d_pedal

- range **0.0 → 18.93** % (span 18.93, last 13.17, 70 samples)
- [ ] moved as expected under throttle? (boost/rail/MAF rise, pedal tracks foot)

## Promotion decision

For each decoded request, once the plausibility box above is
checked and true, edit the candidate mapping's
`verification.status` to `locally_verified` and record the
vehicle label `F10-520d-dev`. If a request returned a
negative response or an implausible value, set it `rejected`
with the NRC/reason. Nothing here is promoted automatically.

