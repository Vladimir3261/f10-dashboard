# N47 source audit

Every source inspected for the N47 research pipeline, what it actually
contains, and how far it can be trusted. Pins and licenses are in
`research/manifests/sources.yaml`; most entries were retrieved on
2026-08-25, with `obd-gauge-cluster` refreshed on 2026-09-02 after issue
#1 identified newer on-car evidence. "F10 applicability" is `unverified`
for every source — nothing in this audit has been validated against the
target car.

## Primary evidence sources

### klartext (`HadiCherkaoui/klartext` @ `04c9ee52`)

- **License:** AGPL-3.0-or-later. Reference/oracle only; no code copied.
- **Vehicle / ECU:** BMW F25 X3 (session 1) and an F20 (project car),
  N47 diesel, DDE `d72n47a0` at `0x12`, ENET/HSFZ.
- **Raw captures:** yes — a 41-minute, 5912-packet pcap-verified on-car
  session (the pcap itself is not published; the doc is a byte-level
  verification addendum against it).
- **What it establishes (Tier A):**
  - the dynamic-measurement wire sequence on `d72n47a0`:
    `2C 03 F3 03` → `2C 01 F3 03 45 17 01 02` → `22 F3 03` →
    `62 F3 03 39 08` → 46.0 °C oil temperature (source id `0x4517`);
  - DPF soot reads via the same sequence, sources `0x44BE` (measured,
    15.49 g) and `0x44C1` (modelled, 15.5 g);
  - the *static* form `22 45 17` is **rejected** (`7F 22 31`) on this
    variant — the source id is a define-source, not a readable DID;
  - no session/security precondition for these reads.
- **What it establishes (Tier B, `sgbd_derived`):** the `SG_FUNKTIONEN`
  table structure (1787 rows × 16 columns in `d72n47a0`; 272 jobs, 89
  tables), the `raw × MUL / DIV + ADD` scaling convention, and the
  ITMOT worked example (`0x4BC3`, 0.1·raw − 273.14, `0x0E2F` → 89.96 °C,
  disassembly-derived, explicitly "pending on-car confirmation").
- **Variant facts verified:** the `d72n47a0` ECU comment reads "SGBD für
  N47TÜ/N57TÜ (DDE7.21/7.01/7.41) verwendet in F0x, F1x, F2x, F3x (UDS,
  MV, FlexRay)"; `d73n47a0` is identified as the E84/X1 KWP2000 variant.
  The doc itself warns the exact variant must be resolved on-car.
- **Reliability:** highest of all sources — it separates
  wire-observation from disassembly-derivation explicitly and marks its
  own unconfirmed claims. Copied from nowhere; it derives from BMW's
  `.prg` files the user supplied (same primary source as the ediabasx
  docs site).
- **Clean-room note:** facts extracted with `file:line` citations only.

### wican-issue-752 (`meatpiHQ/wican-fw` issue #752)

- **License:** repository GPL-3.0; issue text is a user report —
  `unknown`. Raw byte observations treated as uncopyrightable facts.
- **Vehicle / ECU:** BMW E90 320d, N47D20C, `DDE7N47`, ISO 15765-4
  11-bit 500k, `7DF`/`7E8`.
- **Raw captures:** yes — the `0x0406` exchange is quoted frame by frame:
  `2C 10 04 06` → `6C 10 0E D7` → 3799/100 = 37.99 g, **no identifier
  echo**. Firmware versions affected are named; the fix is NOT merged
  (maintainer comment 2026-04-26 defers to #751, disabling validation).
- **Also claimed, no frames shown (Tier C):** `0x03EB` distance-since-
  regen (`/1000` km, 4 data bytes) and `0x0AF1` engine temperature
  (`×0.01969` °C) — the latter **collides with the D73 table's ITMOT
  = 0x0AF1 at 0.1·raw − 273.14**; recorded in the conflict report.
- **Reliability:** high for the one captured exchange; the issue author
  distinguishes captured evidence from configuration claims. Independent
  of the SGBD-export family as far as ancestry is traceable.

### obd-gauge-cluster (`cheeseprince/obd-gauge-cluster` @ `742f8a44`)

- **License:** MIT.
- **Vehicle:** BMW F10 535i — right chassis, **N55 petrol** engine.
- **Raw captures:** yes — census, 1,797-probe sweep and cold/warm drive
  data summarized in `docs/BMW-STATUS.md`.
- **What it establishes (Tier A, for its car):**
  - the F10 DME answers enhanced Mode-22 on the plain `7DF` functional
    broadcast; 462 DIDs answered across six blocks: `42xx`, `43xx`,
    `44xx`, `45xx`, `4Axx` and `58xx`; the swept `DAxx` block returned
    no answers;
  - `22 586F` returns u16 big-endian **absolute millibar** oil pressure.
    KOEO values of 1057–1058 mbar against roughly 1000 mbar barometric
    pressure, plus the return to atmospheric as the engine stops, prove
    the datum. Gauge pressure requires a contemporaneous barometric
    subtraction; treating the raw value as gauge overstates it by about
    one atmosphere (~14.5 psi);
  - `22 4402` is u16 big-endian oil temperature at
    `raw × 0.75 − 48 °C`, confirmed by a 24.8 → 74.2 °C cold-start ramp
    and independent `22 4408` corroboration (`r = 0.99978`);
  - the former `22 5817` / `22 58EB` oil-temperature candidates are not
    oil temperature on the source car: they duplicate each other and
    track ambient;
  - the community `DAxx`/`6F1` EGS path was unreachable **through an
    ELM327** (`612`/`618` silent, `22 DA25` → `7F 22 22`). That finding
    is adapter-specific and does not transfer to our ENET/HSFZ transport,
    which demonstrably routes to `0x18`.
- **Methodology (adopted):** pre-scan assumptions kept separate from
  on-car results; failed assumptions documented; "a shape check cannot
  validate a scale" (their byte-0 oil-pressure decode was wrong by ~4×
  and passed its own plausibility test). The absolute-pressure correction
  adds the matching lesson that a scale does not establish its datum.
- **Reliability:** high; it documents its own mistakes with evidence.
  None of the N55 DIDs is treated as an N47 fact without target-car
  validation.

## Structured-table sources (Tier B)

### morguux-d73n47a0 (gist `832054bc…` rev `074bac9c`)

- **License:** none attached → `unknown`. Underlying data is an
  SG_FUNKTIONEN-shaped export of BMW's `D73N47A0` SGBD (E84/X1, KWP
  family) — database-right status unresolved; treat bulk derivatives as
  non-redistributable until reviewed (see legal notes).
- **Content verified:** 1645 data rows, 9 columns
  (`Title,ID,Result Name,Data Type,Unit,MUL Factor,ADD Factor,Label,
  Description`), zero duplicate IDs; data types are `unsigned char/int/
  long` + 7 × `motorola float`. All identifier claims from the task
  brief (0x0405–0x040F, 0x0604–0x0608, 0x0EA6/0x0EA7) are present with
  the exact scales recorded in `research/normalized/n47/signals.jsonl`.
- **What it does NOT establish:** wire service, request sequence,
  response framing/echo, session, integer byte order, or any F10
  compatibility. All 1645 records import as partial/crossref/alias —
  zero executable candidates, by design.
- **Ancestry:** same primary source family as klartext's and the
  ediabasx-docs tables (BMW SGBD catalogue). Not independent of them.

### ediabasx-docs-sgbd (`emdzej/ediabasx-docs-sgbd` @ `b644de8f`)

- **License:** none stated → `unknown`; machine-generated from BMW
  `.prg` files, so redistribution status is doubtful. Used strictly as a
  **lookup oracle** for individual row cross-checks; never bulk-scraped.
- **Used for:** confirming the `d72n47a0` rows behind the klartext
  fixtures (ITOEL 0x4517 = 0.01·raw − 100; IMRUP 0x44BE = 0.015259;
  IMPAS 0x44C1 = 0.01; ITMOT 0x4BC3 = 0.1, −273.14) and the D71 family
  picture: `D71N47A0` ("DDE 7.1 für N47 — E87, E9x") is **KWP2000**,
  its `STATUS_MESSWERTBLOCK_LESEN` job comment naming
  "$2C DefineDataByLocalIdentifier $10 RecordLocalIdentifier" — which is
  exactly the WiCAN E90 wire pattern — and its `MESSWERTETAB` (502 × 12)
  carries **ITOEL = 0x00C1 at 0.016787·raw − 50.138**, a third,
  incompatible oil-temperature definition.
- **Also on the site:** `d70bx7a0` ("N47O1, N57O1, B47U0/O0 (DDE7.01,
  DDE7.41) … F15, F20, F30, F56, F45 (UDS, MV)") — a second F-series
  N47-capable SGBD family, relevant to variant resolution on our car.

## Implementation / configuration sources (Tier C)

### ediabaslib (`uholeschak/ediabaslib` @ `a7cef804`, GPL-3.0)

Deep OBD's `BmwDeepObd/Xml/E90/Motor.ccpage` names the `d_motor` group,
the job `STATUS_MESSWERTBLOCK_LESEN`, a 24-ARG measurement block
(IUBAT2, ITMOT, ITOEL, ITKRS, ILMMG, SLMMG, ITUMG, IPLAD, SPLAD, ITLAL,
IPUMG, IPRDR, SPRDR, ITAVO, ITAVP1, IPDIP, IDSLRE, PFltRgn_numRgn,
CoEOM_stOpModeAct, ISRBF, ISOED, PCBS_lDistanceOut + two OBD_PID args)
and the `STAT_…_WERT` result names the brief predicted. Result names
only — no identifiers, no scaling, no framing. Imported as
`job_definition` + partial signal records. GPL-3.0: behaviour oracle,
never a runtime dependency, no code copied.

### bmw-xdfs-testo (`MotorMouth93/BMW-XDFs` @ `54a7ce42`, no license)

The originally-referenced `zarboz/BMW-XDFs` returns **404**; this
repository (not marked as a fork) carries the same
`Me7.2/Datalogger/TestO Datalogger/config/customjobs.xml`. Verified
entries: `D71N47A0` `STATUS_MESSWERTBLOCK_LESEN` args
`3;0x13A6;0x0080`; `D71N47C0`/`D71N47D0` with a 13-identifier set
(`0x1881,0x0500,0x0709,0x076D,0x01F4,0x0502,0x0772,0x0672,0x0641,
0x07D1,0x07D0,0x041E,0x0A29`). Establishes variant names, the job, and
identifier sets someone logged with — nothing about meaning or scaling.
Cross-check: `0x13A6`/`0x0080` are INLKG/ILMKG (air-mass rows) in
D71N47A0's MESSWERTETAB, which corroborates the set being real, not
invented.

### obdb-bmw-5-series (`OBDb/BMW-5-Series` @ `d6345247`, CC-BY-SA-4.0)

30 UDS `22` commands with `6F1` extended addressing. Relevant claims:
EGS `0x18` DIDs `DA12` (ATF temp, 8-bit scalar — decode incomplete),
`DA25` (oil temp, s16 −48 offset), `DA2A` (converter + output-shaft
speeds, 2 × s16 rpm), `DA2E` (gear enum P/R/N/D); DDE `0x12` `586F`
(oil pressure, declared 8-bit scalar — **contradicted** by the
obd-gauge-cluster on-car u16 absolute-millibar decode); KOMBI `0x63`
`D031` (current gear). No per-signal provenance; year filters suggest
F/G-era applicability. Tier C throughout; two rows pass
decode-completeness but none pass verification beyond `discovered`.

### bmw-dash-display (`anejckl/bmw-dash-display` @ `b25db4ac`, MIT)

E81 N47 DDE7.0. Standard Mode-01 table (cross-checks our production
mapping), an explicitly "Needs Testing" Mode-22 section (not imported),
and community E-series broadcast CAN IDs (`0x0AA`, `0x1D0`, `0x130`,
`0x0C0` — different bus concept entirely, not imported as mappings).

## Architecture / tooling references (no records imported)

- **ediabasx** (`emdzej/ediabasx` @ `78a3f5f4`, PolyForm-NC-1.0.0):
  `.prg` parser, table exporter, BEST/2 decompiler. Offline oracle only;
  its license bars commercial runtime use. The designed import path is:
  user exports JSON/CSV from a legally-supplied `.prg` with it, and our
  importer consumes the export.
- **bimmerdis / bimmerjson** (`radelbro/*`, no license): PRG→`.b1v`→JSON
  path for a future importer; unusable for redistribution until
  licensed; not yet exercised.
- **bimmerdaten** (`zer02dev/BimmerDaten`, GPL-3.0 per its LICENSE
  file despite GitHub's NOASSERTION): human-facing PRG browser oracle.
- **pydiabas** (MIT): future Tool32/EDIABAS oracle automation.
- **bmw-best2-disassembler** (GPL-3.0): secondary opcode cross-check.
- **beemuu**, **bimmerz-box**, **bmw-enet-tool** (kaiwen-z): design
  references (transport separation, simulator, F10 ENET behaviour).
- **awesome-automotive-can-id** (CC0): discovery index only.

## Independently discovered sources (§10 searches)

- **kmalinich/dieslg8** (@ `a90d1641`, MIT) — carries
  `ref/D73N57C0-MESSWERT.csv`, the N57 sibling of the MorGuux export
  family: confirms the export format exists beyond one gist and gives an
  ancestry comparator. Not yet imported.
- **kmalinich/deepobd-configs** (@ `2f045567`, MIT) — E90 DDE ccpages,
  derived from ediabaslib's samples (relationship recorded).
- **emdzej/ediabasx-docs-sgbd** — found via the `d72n47a0` code search;
  audited above.
- **zoxknez/freecarly** — decompiled Carly APK whose smali contains the
  `2C 01 F3 03` sequence. **Rejected**: decompiled proprietary
  application; noted purely as existence-evidence that commercial tools
  use the same dynamic-define path. Nothing extracted.

## Rejected sources

- **govmateai-bmw-pro-diagnostic** (@ `e1f3ea9e`, MIT): README claims
  E84 N47 UDS/EDIABAS/DPF support; inspection shows env-overridable
  routine-ID placeholders ("RID found from ISTA or log" defaults),
  Turkish AI-styled scaffolding, and RoutineControl/
  WriteDataByIdentifier service operations (forced regen, adaptation
  reset) in the tree. Tier D; no constant from it enters any record.
  Its write/service operations are the canonical `write_or_control`
  exclusion test case.
- **zoxknez/freecarly**: see above.
- **zarboz/BMW-XDFs**: repository no longer exists (404) — superseded by
  the MotorMouth93 copy, with the caveat that ancestry between the two
  cannot be established from GitHub metadata.

## Negative results / dead ends

- GitHub code search for `"DDE7N47"` returns zero code hits — the ECU
  ident string is effectively absent from public code.
- No public repository was found carrying a raw F10/F11 **N47** DDE
  capture of either the `F303` dynamic sequence or a static read from the
  N55-observed `42xx/43xx/44xx/45xx/4Axx/58xx` blocks. The F25
  (klartext) and F10-N55 (obd-gauge-cluster) captures are the nearest
  evidence on each side.
- OBDb has no BMW-520d/F10-diesel-specific repository; BMW-5-Series is
  the closest and is hybrid/petrol-slanted (HV-battery heavy).
- The `0x03EB`/`0x0AF1` WiCAN claims could not be corroborated by any
  second independent source at the wire level.
