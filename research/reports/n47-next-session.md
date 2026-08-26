# N47 — next on-car session (planned)

Picking up after 2026-08-25. The core telemetry is done: 13 d72n47a0
dynamic channels are `verified` on F10-520d-dev (idle OBD cross-check +
throttle sweep), wired into the runtime behind `--extra-mappings`, and
shown on the 3-mode dashboard. See `n47-oncar-results.md` for what was
proven. This file is the to-do for the next session.

## Setup reminders (every session)

- Stop `live.py` before running any validation tool — the ZGW serves one
  HSFZ client at a time.
- Laptop needs a `169.254.x.x` link-local address on the ENET cable.
- Ignition on; engine running for anything load- or flow-dependent.
- Artifacts land in `validation-runs/` (tracked, VIN-redacted) and
  `local/validation-runs-raw/` (gitignored, VIN).

## TOMORROW — find the real gear (needs a ROAD drive)

Status: D031 (KOMBI 0x63) and DA2E (EGS 0x18) are REJECTED as the gear
source (parking-lot manual-shift test 2026-08-26 — none of their bytes
step 1..8; a ZF 8HP won't engage high gears at standstill anyway). The
engaged 1..8 gear is on a DID we haven't found. Plan:

1. Parked, engine on: scan the EGS for every DID it answers -
       python3 tools/egs.py scan --ecu 0x18
   (holds the gateway a few minutes; nothing else connected). Repeat for
   the KOMBI if needed (--ecu 0x63). Gives a DID inventory.
2. Pick DIDs whose response is a small 1-byte value (gear-shaped).
3. Road drive reaching gears 3..5, logging those DIDs (a throwaway
   candidate file, one signal per DID), then correlate which value steps
   1..8 with speed. That DID+byte is the gear.
4. Rename to `gear`, verify, add to --extra-mappings -> dashboard (the
   Drive view already lists `gear`).

Fallback if no clean gear DID exists: derive gear from the ratio of
road speed to engine rpm (ZF 8HP fixed ratios) - approximate, discrete-
ised. Only if a direct DID can't be found.

VERIFIED this session (keep): DDE gearbox oil temp / turbine speed /
converter temp (d72n47a0_gearbox.yaml) - turbine speed ~= engine speed
at cruise confirmed the scale.

## Ready to validate now — the new DPF/EGR candidate channels

`mappings/candidates/bmw/dde/n47/d72n47a0_dpf_egr.yaml` (7 channels,
built 2026-08-26, Stage 2) is source-backed but **not yet validated on
the car**: DPF differential pressure (0x44F8), exhaust temp before DPF
(0x44EF) and before catalyst (0x44F2), distance-since-regen (0x44BF,
u32), successful-regen count (0x44B8), operating-mode status word
(0x467E), EGR control deviation (0x487A).

    # one at a time, or --all
    python3 tools/validate_candidate.py run \
        mappings/candidates/bmw/dde/n47/d72n47a0_dpf_egr.yaml --all --step

Plausibility to confirm: DPF ΔP a few to tens of hPa warm idle, rising
with exhaust flow under load; exhaust temps climb under load (and spike
during a regen); distance-since-regen increases monotonically; regen
count a stable integer; EGR deviation near 0 % when the loop is happy.
Promote each to `verified` on success, then add the file to the live
`--extra-mappings` set. (The polling stagger fix, also Stage 2, is done.)

## Planned experiments, in priority order

### 1. Boost under real load (a gentle drive)

Neutral revving builds little boost (no air demand), so today's sweep
only nudged boost ~0.25 bar. A gentle pull in gear would build proper
boost and stretch rail/MAF/boost across their full range.

    python3 tools/validate_candidate.py sweep \
        mappings/candidates/bmw/dde/n47/d72n47a0_flow.yaml \
        n47.d72.dyn.4746 n47.d72.dyn.4841 n47.d72.dyn.47DD n47.d72.dyn.4232 \
        --seconds 60

Only with a safe way to log (passenger, or a stationary dyno-style pull).
Expect: boost well above ambient, rail toward 1400+ bar, MAF climbing
with load. Confirms the scales at the top of their range.

### 2. Transmission channels received by the DDE

The D73 table (research records) shows the DDE *receives* gearbox values.
Derive the d72n47a0 equivalents from the cached SG_FUNKTIONEN table
(`local/research-cache/misc/d72n47a0.md`) — candidates to look up:
current gear, turbine speed, gearbox oil temp, converter temp. Build a
`d72n47a0_trans.yaml` candidate the same way `d72n47a0_flow.yaml` was
built, then validate. Cross-check gear against actual selected gear;
gearbox oil temp against a plausible warm value. (Also compare with
direct EGS `0x18` DIDs from `tools/egs.py scan --ecu 0x18` and the OBDb
`DA2A/DA2E` claims — see `n47-conflicts.md`.)

### 3. Cold-start temperature ramp (gold-standard temp check)

The car has always been warm, so the temperature *scales* were confirmed
against OBD but never watched ramp from ambient. On a cold start:

    python3 tools/validate_candidate.py sweep \
        mappings/candidates/bmw/dde/n47/d72n47a0_flow.yaml \
        n47.d72.dyn.461B --seconds 300
    # (and 4517 oil, 4BC3 engine temp, 4843 charge-air — one at a time or --all)

Expect coolant/oil to rise from ambient toward ~90 °C; oil should lag
coolant. A clean ramp that matches OBD PID 0x05 the whole way is the
strongest possible temperature validation.

### 4. DPF under a regeneration event (opportunistic)

Today soot read 0.09/0.10 g (clean). If a regen ever happens during a
session, sweeping `44BE`/`44C1` (measured/modelled soot) plus the
differential-pressure and exhaust-temp channels would show the DPF
working — measured soot dropping as it burns off.

## Not blocked on the car

- Runtime polish: the F303 channels are all `slow` class; a full slow
  cycle now sends ~13 x (2 setup + 1 poll) frames. If that stutters the
  fast OBD channels, stagger the F303 reads across cycles or add a
  slower polling class for them. Watch the dashboard Hz with
  `--extra-mappings` enabled and tune if needed.
- Consider a dedicated `dpf`/`trans` polling class in the mapping so the
  heavy proprietary reads don't compete with fast OBD.
