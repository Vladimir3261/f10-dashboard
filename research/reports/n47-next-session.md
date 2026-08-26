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

## Ready to validate now — GEAR + gearbox candidates (built 2026-08-26)

The user wants current gear on the dashboard. Gear lives on OTHER ECUs
than the DDE, reached by static UDS 0x22 (the mapping engine already
polls a second ECU via fixed-address targets — proven offline). Three
new candidate files, all source-backed, all pending on-car validation:

  - mappings/candidates/bmw/kombi/f10_gear.yaml — CURRENT GEAR (number)
    from KOMBI 0x63, `22 D031` (OBDb 5SERIES_GEAR_V2). THE one the user
    wants on the dashboard.
  - mappings/candidates/bmw/egs/f10_transmission.yaml — gear SELECTOR
    (P/R/N/D) + drive mode from EGS 0x18, `22 DA2E` (OBDb).
  - mappings/candidates/bmw/dde/n47/d72n47a0_gearbox.yaml — gearbox oil
    temp / turbine speed / converter temp the DDE receives (F303 on 0x12,
    d72 SG_FUNKTIONEN scales) — health signals, not the gear number.

Validate each (one file at a time; the gear/EGS ones target 0x63 / 0x18):

    python3 tools/validate_candidate.py run mappings/candidates/bmw/kombi/f10_gear.yaml --all
    python3 tools/validate_candidate.py run mappings/candidates/bmw/egs/f10_transmission.yaml --all
    python3 tools/validate_candidate.py run mappings/candidates/bmw/dde/n47/d72n47a0_gearbox.yaml --all

Plausibility: gear steps 1..8 in Drive (0/N distinct); selector tracks the
lever; gearbox oil temp warms like engine oil; turbine speed tracks rpm.
UNKNOWNS to confirm on-car: does 0x63 answer 22 D031, and 0x18 answer
22 DA2E, on THIS car? (OBDb is a 5-series community claim.) If 0x63 is
silent, try the EGS DA2E, or scan with tools/egs.py. Once gear decodes,
add its file to the live --extra-mappings set and it shows on the
dashboard Drive view (already wired in).

## TANK LEVEL + WARNINGS (washer fluid etc) — needs a discovery session

Not a table lookup. Fuel tank level and NBT warning states (washer fluid
low, brake wear, ...) live on body/cluster modules and are not in the DDE
SG_FUNKTIONEN table. Approach: an on-car discovery session — scan the
KOMBI (0x60/0x63), JBE/body (0x00), and fuel modules for UDS 0x22 DIDs
with tools/egs.py, correlate a value against a known state (tank level
vs the gauge; washer low vs the actual warning), then build candidates.
local/captures/kombi_dids.json (a real 0x60 scan) is a starting point but
uses the 0x1xxx DID space; the OBDb DAxx space (e.g. 0x60 D111 fuel range)
is the other lead. This is a research/discovery task for a dedicated
session, not an offline build.

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
