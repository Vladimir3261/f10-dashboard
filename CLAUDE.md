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
gradual change ("boost is now tracking 0.18 bar under setpoint at
comparable load and RPM, up from 0.05 three months ago") rather than raw
thresholds.

**This car has no particulate filter — it was removed.** So the classic
DPF worked example is void here: `n47d_dpf_dp` and the two soot channels
report an empty pipe and an ECU model respectively, and no DPF health
conclusion can be drawn. Regeneration *count* is still meaningful — the
ECU commands regens against its model, at a real fuel and oil-dilution
cost. See `docs/DPF_SOOT.md`.

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
- **Mapping data is versioned.** Every mapping file carries an integer
  `mapping.version` (starts at 1); it is incremented by one on every
  change to that file's content — and only on mapping changes, never on
  code changes. That version is stamped onto every recorded sample
  (`params.mapping_ver` / `run_mappings` / `runs.mapping_set` locally,
  `samples.mapping_ver` / `sessions.mappings` in the lake), so a dataset
  ties back to the exact mapping revision that produced it.
  `mappings/VERSIONS.lock` (a test enforces it) and
  `tools/check_mapping_versions.py` (a git-diff guard) keep a change from
  landing without a bump. See `docs/DATA_VERSIONING.md`.
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
- **Dashboard has 3 views**: Drive (M-Performance cluster — shift-light
  rev bar, hero tach+speedo, big centre GEAR, M-tricolor, tiles), Detail
  (per-channel history graphs), All-data (dense table w/ min/max/age).
- **Poll rates follow the channel, not the loop** (OBD mapping v2,
  2026-08-30; v5 2026-09-01): wall-clock tiers — `motion` 10 Hz (rpm/
  speed/map/pedal), `control_ctx` 1 s (load/maf), `context` and `slow`
  1/10 s, `rare` 1/60 s — plus `dde_dyn` (round-robin, ~1/11 s each) and
  `egs` 2 Hz. 7,740 → 2,854 requests/min. Declared actual/setpoint pairs
  share one rotation slot so they are sampled in the same cycle, and each
  response carries its own acquisition timestamp (a cycle is executed
  sequentially, so one shared timestamp would erase the gap an alignment
  contract exists to measure). Phase spreading was measured and rejected:
  the burst is 7 wire exchanges, not 26 requests. See
  `docs/POLLING_AND_SAFETY.md`.
- **Drive modes** scale those classes at runtime: `off` (connected but
  silent), `sampling` (120 s on / 600 s off, slow tiers exempt), `long`,
  `normal` (= the declared rates), `debug` (the pre-v2 behaviour).
  Switch from the dashboard's `mode` chip or `--mode`. **A mode change
  starts a new run** — one run has exactly one sampling configuration,
  recorded in `sessions.mode`, so no analysis can silently mix rates.
  See `docs/POLLING_AND_SAFETY.md`.
- **ClickHouse lake + Grafana are DEPLOYED on a VPS** (`infra/`, docker
  compose): the sync agent ships every drive up over mobile (~1.4
  bytes/sample), server-side normalization, and a provisioned
  `f10-health` Grafana dashboard (boost tracking, decode cross-check,
  and DPF panels that are now decorative — see the no-filter note above).
  Per-request faults land in `telemetry.channel_errors`. Secrets live in
  the VPS `.env` (gitignored); the VPS IP + Grafana password are in the
  owner's notes, not git. `analysis/clickhouse/insights.sql` is the query
  battery.
- **The Pi admin panel** (`hardware/raspberry-pi/admin/`): a phone-sized
  page on `:8088`, three tabs. *System* — health
  (temp/throttle/disk/Wi-Fi/**clock**), recording truth (samples/min, not
  just "service active"), drive files with delete-if-synced, services,
  logs incl. previous boot, `git pull` (ff-only, pinned remote), reboot,
  clean shutdown. *Car link* — see below. *Claude* — the optional agent
  session: status, crash-loop detection, tmux pane, restart; no terminal
  and no prompt box, deliberately. Basic auth, LAN-only bind, sudoers
  allowlist. Its own systemd unit: it must survive `live.py` being broken.
- **`/api/diagnostics` — the verification view** (Car link tab). The full
  car-communication picture for a session: mappings loaded (with an
  `--extra` badge), what resolution **dropped and why**, per-request
  sent/ok/failed with success rates and last errors, and every channel
  traced to its request and mapping version. `resolve()` now returns a
  `ResolutionReport`, so *"why is this channel missing?"* is a lookup
  rather than an SSH-and-guess: the ECU does not advertise the PID, the
  file is for another variant, or a derived channel lost an input.
  **`sent` with no `ok` is a channel the car is not answering** — in the
  sample table that is indistinguishable from one nobody asked for.
- **The host clock is handled** (fixed 2026-08-30). The Pi has no RTC and
  once corrected itself 76.5 min mid-recording, corrupting a timeline.
  Now: services wait on `time-sync.target`, `--wait-for-clock` gives NTP
  a bounded chance, every run records `clock_synced`, and a mid-run step
  ends the run so none spans a discontinuity. **Anything time-derived
  must filter `sessions.clock_synced = 1`.**
- **Stage 1 data quality is live** (2026-08-31): the decoder returns
  `Reading(value, quality)`; sentinel/saturated/clipped values keep their
  bit-exact number plus a label through SQLite, the wire and the lake
  (`samples.quality`); the display suppresses them and derived channels
  drop with their flagged inputs. engine.yaml v4 declares lambda's
  0xFFFF sentinel and MAP's 255 saturation. See `docs/DATA_QUALITY.md`.
- 619 tests, no car / no network / no BMW data required.

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
  a test fails if it changes. The pin is a tripwire, not a freeze:
  re-base it only together with a `mapping.version` bump and a note in
  the test saying what changed and why.
- **Bump `mapping.version` for content, not comments.** The version is
  stamped on every sample; bumping it for a prose edit would falsely
  signal a data change and split a dataset that never changed.
- **Report honestly.** Distinguish observed fact / inference / hypothesis
  / unknown. A single warm reading is not a validated scale; say what was
  cross-checked and against what. **Vehicle configuration is part of a
  channel's provenance** — the soot channels cost weeks because nobody
  had written down that the filter was removed.
- Commit messages end with the `Co-Authored-By` trailer; branch off
  master before committing if asked to commit.

## Running it in the car

- Launch with **`./run_car.sh`** (loads every verified channel; a bare
  `live.py` has no gear and the dashboard shows "N"). Dashboard on
  `:8080`; open the Drive view for the M-cluster.
- **Pick a drive mode** for the trip: `normal` by default,
  `./run_car.sh --mode long` for a motorway run, `--mode sampling` for a
  multi-hour one, `--mode debug` when chasing a specific problem. Also
  switchable mid-drive from the `mode` chip — that ends the current run
  and starts a new one, which is intended.
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
