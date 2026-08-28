# f10-dashboard — project brief

Orientation for anyone (human or AI) picking this up. Purpose and
architecture first; day-to-day conventions at the end.

## What this is

A **personal** telemetry, logging, diagnostics, and long-term analytics
system for **one car** — the owner's BMW F10 520d (N47 diesel), read over
ENET/HSFZ. It is deliberately **not** a product: no distribution, no
SaaS, no multi-tenant, no onboarding, no hardware sales, no general-market
compatibility. Optimise for technical depth, observability, data quality,
and experimentation over polish.

The real long-term goal is not the live dashboard — it is a **historical
behavioural model of this specific vehicle**: baselines conditioned on
operating state (ambient, load, RPM, speed, trip phase), and detection of
gradual change ("DPF ΔP has crept from 24–27 to 35 mbar at comparable
exhaust flow over 3 months") rather than raw thresholds.

## Vehicle

BMW F10 520d, engine family **N47**, engine ECU at diagnostic address
`0x12` (observed, confirmed by capability — never assumed). DDE variant
is **d72n47a0-family** (F-series UDS), confirmed *behaviourally* on-car
(the F303 dynamic-read sequence is accepted); the exact SGBD revision is
not yet pinned from an ident DID (F191/F194/F197/F18A return NRC 0x31).

**No VIN in this repository.** The car is referenced by the stable label
**`F10-520d-dev`**; the label→VIN table lives in `local/VEHICLES.md`
(gitignored). Keep it that way — never commit the VIN, in code, tests,
docs, or artifacts.

## The central architectural idea

Vehicle knowledge is **data, not code**. `live.py` speaks HSFZ, discovers
the gateway/ECUs, records to SQLite and serves the dashboard; it does not
know what a PID means. What to read, how bytes decode, how often, and
where it came from all live in versioned mapping files under `mappings/`,
loaded through the dependency-free `bmwdiag` package.

External BMW knowledge earns its way into the runtime through a pipeline:

```
external source (SGBD table, capture, community data)
      ↓  research/importers/  (deterministic, provenance-preserving)
normalized research records (partial knowledge stays partial)
      ↓  gate + conflict analysis
candidate mappings (mappings/candidates/, production: false)
      ↓  supervised read-only on-car validation (tools/validate_candidate.py)
locally verified mappings  →  runtime telemetry (--extra-mappings)
```

### Load-bearing principles (do not erode these)

- **Read-only runtime.** Only observational reads are ever sent
  (OBD 0x01/0x09, UDS 0x22, 0x2C define/clear/read subfns, 0x19, 0x3E).
  The validation tool enforces a service allowlist at a single choke
  point; write/control services abort. No state-changing job enters
  automatic polling.
- **No proprietary tool is a runtime dependency.** ISTA / Tool32 /
  EDIABAS / EdiabasLib / EdiabasX / PRG-GRP are research & validation
  **sources only**. The runtime decodes through our own mappings.
- **No invented BMW data.** Every fact is labelled by origin
  (`wire_observation` / `sgbd_derived` / `source_claim` / `inference` /
  `speculation`). Unknown stays `"unknown"`. Tier-D (untraceable) claims
  never produce anything executable. See `docs/MAPPING_RESEARCH.md`.
- **Capability by probe, never by address.** The engine ECU is whichever
  answers PID 0x0C; an SGBD variant is confirmed by replaying its own
  read (`bmwdiag/variant.py`), not by an address or an ident string.
- **Variants never merge.** d71 / d72 / d73 N47 are different diagnostic
  variants; the same identifier can mean different things or use a
  different request on each. A mapping resolves against the actual ECU.
- **The production set stays clean.** `mappings/obd/engine.yaml`
  (standard SAE J1979) is the only mapping the default `live.py` loads.
  Proprietary-derived channels (BMW SGBD scales) load **only** via
  `--extra-mappings` — an explicit per-run opt-in — so the repo's "no
  proprietary data in the production set" property holds. There are
  open license questions on the SGBD-derived data; see
  `research/reports/legal-and-license-notes.md`.
- **Float-exactness is a contract.** Decode steps (`scale`, `divide`,
  `add`) are separate and skipped at identity so migrated formulas stay
  bit-identical; a frozen regression decodes every input byte.
- **Verification states** (research): `discovered` → `candidate` →
  `externally_verified` → `cross_source_confirmed` → `locally_verified`
  → `rejected`. Only **`locally_verified`** means confirmed for THIS car.
  (Mapping *files* use the loader's 4-state vocabulary — `verified`
  there means locally verified on `F10-520d-dev`, detailed in the
  `verification.method` field.)
- **Portability for a future embedded runtime.** Mappings are plain data
  (no expression language, no eval) so they can later compile to C
  structures. Do not add anything to the format that executes.

## Current state (what works)

- ENET/UDP discovery, HSFZ transport with reconnect, ECU discovery by
  capability, OBD Mode 01 live polling, SQLite time-series logging,
  HTTP/SSE dashboard with historical run viewing.
- The declarative mapping engine: model, dependency-free YAML parser,
  loader/validator, registry, decoder, derived channels, polling plan,
  executor, OBD capability, variant capability.
- The N47 research pipeline: pinned source manifest, importers,
  normalized records, gate, conflict detection, generated reports.
- **~23 proprietary channels verified on the car**, in five files under
  `mappings/candidates/` (all `verification.status: verified`, still in
  candidates/ — not moved to `mappings/verified/`, deliberately left in
  place): d72 dynamic (oil/soot×2/engine temp), d72 flow (coolant, boost
  act+set, rail act+set, MAF, charge-air, ambient, pedal), d72 DPF/EGR
  (ΔP, exhaust temps, dist-since-regen, regen count, op-mode, EGR dev),
  d72 gearbox (gearbox oil temp, turbine speed, converter temp), and the
  **engaged gear** (EGS `0x18`, `22 DA2E` byte 1 — steps 1..8 with speed).
  Validated by on-car cross-checks / drives. Loaded via `--extra-mappings`.
- **`./run_car.sh`** launches `live.py` with every verified channel — use
  it instead of a bare `live.py` (a bare launch has no gear/DPF/etc and
  the dashboard falls back to "N").
- **Dashboard has 3 modes**: Drive (M-Performance cluster — shift-light
  rev bar, hero tach+speedo, big centre GEAR, M-tricolor, tiles), Detail
  (per-channel history graphs), All-data (dense table w/ min/max/age).
- **ClickHouse lake + Grafana are DEPLOYED on a VPS** (`infra/`, docker
  compose): the sync agent ships every drive up over mobile (~1.4
  bytes/sample), server-side normalization, and a provisioned
  `f10-health` Grafana dashboard (DPF ΔP-vs-flow, soot rate, boost
  tracking, decode cross-check). Secrets live in the VPS `.env`
  (gitignored); the VPS IP + Grafana password are in the owner's notes,
  not git. `analysis/clickhouse/insights.sql` is the query battery.
- 294 tests, no car / no network / no BMW data required.

## Repo map

```
live.py                     transport, discovery, recorder, HTTP/SSE, dashboard
bmwdiag/                    the mapping engine (stdlib only, opens no sockets)
  mapping/                  model, loader, decoder, derive, polling, execute, registry
  protocol/ obd/ variant.py transport seam; OBD & SGBD-variant capability
mappings/
  obd/engine.yaml           production: standard SAE Mode 01 (the only default load)
  candidates/bmw/dde/n47/   source-backed candidates; d72 dynamic+flow are verified
  verified/                 lifecycle target dir (see its README)
research/                   offline import/evidence pipeline + reports
  reports/                  source audit, coverage, conflicts, legal, on-car results,
                            n47-next-session (the running to-do)
analysis/                   read-only session analytics (Stage 3): warm-up, cross-
                            checks, load, DPF, quality -> report.md/json + curves.html
infra/                      the ClickHouse lake + telemetry sync (deploy on a VPS)
  clickhouse/init/          schema: narrow samples (VIN=vehicle_id), sessions, vehicles
  ingest/                   the only writer into CH; auth + server-side normalization
  sync/                     fault-tolerant local agent (reads SQLite RO, ships batches)
  common/wire.py            columnar + LZMA batch format (~4 bytes/sample)
tools/                      read-only research + validation (egs.py, export_json.py,
                            validate_candidate.py)
validation-runs/            on-car artifacts, VIN-redacted, one dir per run
drive-sessions/             analysis output (VIN-redacted), one dir per analysed run
docs/                       MAPPING_ARCHITECTURE.md, MAPPING_RESEARCH.md, ROADMAP.md
local/                      gitignored: VEHICLES.md (VIN), captures, raw run copies,
                            research source cache, telemetry.db, sessions/, sync-state
```

Start with `docs/MAPPING_ARCHITECTURE.md` for the runtime model and
`research/reports/n47-next-session.md` for what's planned next.

## Working conventions

- **Run the car read-only, one client at a time.** The ZGW serves one
  HSFZ client; stop `live.py` before any validation tool.
- **Every on-car run produces a tracked artifact** under
  `validation-runs/` (VIN-redacted) plus a raw copy under gitignored
  `local/`. Never commit the raw copy or a VIN.
- **Tests must pass with no car, no network, no BMW data.**
  `python3 -m unittest discover`. The production mapping is byte-pinned —
  a test fails if it changes.
- **Report honestly.** Distinguish observed fact / inference / hypothesis
  / unknown. A single warm reading is not a validated scale; say what was
  cross-checked and against what.
- Commit messages end with the `Co-Authored-By` trailer; branch off
  master before committing if asked to commit.

## Running it in the car

- Launch with **`./run_car.sh`** (loads every verified channel; a bare
  `live.py` has no gear/DPF and the dashboard shows "N"). Dashboard on
  `:8080`; open the Drive view for the M-cluster.
- The link is **ENET/HSFZ over Ethernet** — the host needs a
  `169.254.x.x` link-local address on the cable, and discovery UDP-
  broadcasts to find the gateway. On a laptop this is automatic; on a
  **Raspberry Pi** (the near-term in-car host) it works natively; on
  Android/Termux the link-local IP on a USB-Ethernet adapter is the
  fiddly part (may need root / a static `--ip`/`--local-ip`).
  `find_link_local_ip()` currently shells out to `ifconfig` — fine on a
  Pi, but replace with pure-Python/`ip addr` for non-laptop hosts.
- Keep the **sync agent** (`infra/sync/agent.py --config
  infra/sync/config.json`) running alongside to ship to the VPS over the
  host's mobile/data connection. Config (server URL + token) is
  gitignored; recreate it from `infra/sync/config.json` on the owner's
  laptop or the token in the VPS `.env`.

## Explicitly NOT doing yet

A mapping→C compiler; the AI interpretation layer; multi-ECU polling
beyond the DDE + the gear/EGS reads already added. The in-car host is
moving to a **Raspberry Pi** (Android phone considered as an interim —
stdlib-only runtime makes it feasible, but the ENET link is the catch).
The biggest unbuilt piece and highest-value next domain remains the
**analytics layer** (condition-normalized baselines, drift/anomaly
detection) — the ClickHouse lake + Grafana are deployed and accumulating
the data it needs; `docs/ROADMAP.md` has the staging.
