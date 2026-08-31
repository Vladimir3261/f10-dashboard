# f10-dashboard

Real-time engine telemetry for a BMW F10 over ENET, with a live web
dashboard and a SQLite time-series log.

The application speaks HSFZ — BMW's diagnostic-over-IP framing — to the
central gateway on TCP 6801, routes requests to the ECU that answers for
them, and serves a dashboard over server-sent events.

**Observational only.** The services that can be sent are a closed set:
OBD `0x01`/`0x09`, UDS `0x22` (read by identifier), the `0x2C`
define/clear/read subfunctions, `0x19` and `0x3E`. No write, no actuator,
no coding — enforced at a single choke point.

One honest caveat rather than a comfortable claim: `0x2C` is *not* purely
a read. "Dynamically define data identifier" reconfigures what `F303`
points at, which is ECU state. It is session-scoped and re-armed on every
read, so it should not persist — but "strictly read-only" would be
imprecise. See [docs/POLLING_AND_SAFETY.md](docs/POLLING_AND_SAFETY.md).

```
./run_car.sh                    # the normal launch: every verified channel
./run_car.sh --mode long        # start in a quieter drive mode

python3 live.py                 # bare: standard OBD only, no gear/temps
python3 live.py --ip 169.254.x.x
python3 live.py --demo          # no car needed, simulated data
```

Use `./run_car.sh` in the car. A bare `live.py` loads only the standard
SAE mapping, so the dashboard has no gear and shows "N" — the proprietary
channels are an explicit per-run opt-in via `--extra-mappings`, which is
what keeps the repository's production mapping set free of BMW-derived
data.

The dashboard has three views, switchable in the header:
**Drive** (big gauges + live strips for the fun realtime metrics),
**Detail** (per-channel history graphs), and **All data** (a dense table
of every channel with running min/max and age).

On the in-car Raspberry Pi there is a second, phone-sized page — the
**admin panel** on `:8088` — for what would otherwise be SSH from the
driver's seat: health, recording truth, service control, logs, `git pull`,
clean shutdown, and a **Car link** tab showing the full
car-communication picture for the session. See
[hardware/raspberry-pi/admin/](hardware/raspberry-pi/admin/README.md).

Requires **Python 3.9+** and nothing else. There are no third-party
dependencies, deliberately: this runs on a laptop in a car, where
`pip install` is not always an option.

---

## Setting it up

Standing the whole system up from nothing — server, Raspberry Pi, first
drive — is documented step by step in **[docs/SETUP.md](docs/SETUP.md)**.

## Layout

```
live.py              the application - transport, discovery, recorder,
                     HTTP/SSE server, dashboard
bmwdiag/             the diagnostic mapping subsystem
  mapping/           model, loader, decoder, registry, polling, execution
  protocol/          DiagnosticTransport - the seam to the vehicle link
  obd/               OBD Mode 01 capability discovery, isolated
mappings/            versioned mapping data
  obd/engine.yaml    standard SAE Mode 01 channels (production)
  candidates/        source-backed candidates, production: false,
                     capability-gated - ~23 now verified on the car and
                     loaded via --extra-mappings; still here rather than
                     moved, deliberately
  verified/          the lifecycle target dir; unused so far
  examples/          synthetic fixtures, excluded from the runtime
  VERSIONS.lock      version ledger, test-enforced
config/modes.yaml    drive modes - how hard to poll, scaled per class
hardware/            Raspberry Pi provisioning + the on-Pi admin panel
infra/               ClickHouse lake, ingest, sync agent (deploy on a VPS)
analysis/            read-only session analytics
research/            the N47 evidence/import pipeline (offline only):
                     pinned source manifest, importers, normalized
                     records, conflict detection, reports
tests/               619 tests; no car and no network required
tools/               read-only research and analysis tools
docs/                architecture and research-process documentation
local/               gitignored; scratch, captures, notes, exports,
                     and the research source cache
```

## Diagnostic mappings

Which channels exist, how their bytes decode and how often they are read
is **not** in the application. It comes from versioned mapping files:

```yaml
requests:
  obd.mode01.0C:
    protocol: obd
    pid: 0x0C
    polling: {class: motion}      # 10 Hz - declared in seconds
    response: {data_length: 2}
    signals:
      rpm:
        label: Engine speed
        unit: rpm
        display: {digits: 0, min: 0, max: 5000}
        decode: {type: uint16_be, divide: 4.0}
```

Adding a channel is a mapping edit, not a code change. Mapping files are
data — there is no expression language and no `eval` anywhere in the
format or the code that reads it.

**How often** is declared the same way, in one unit — seconds of wall
clock — as a named class each request belongs to:

```yaml
polling_classes:
  motion:  {seconds: 0.1, priority: 0}   # rpm, speed, map, pedal
  context: {seconds: 10,  priority: 1}   # load, maf, rail, lambda
  rare:    {seconds: 60,  priority: 3}   # ambient, baro, counters
```

A **drive mode** (`config/modes.yaml`) then scales every class at
runtime — `off`, `sampling`, `long`, `normal`, `debug` — switchable from
the dashboard. A mode change starts a new run, so one run always has
exactly one sampling configuration. See
[docs/POLLING_AND_SAFETY.md](docs/POLLING_AND_SAFETY.md).

A development CLI works without a vehicle:

```
python3 -m bmwdiag.mapping validate mappings/
python3 -m bmwdiag.mapping list     mappings/
python3 -m bmwdiag.mapping show     mappings/obd/engine.yaml
python3 -m bmwdiag.mapping plan     mappings/obd
python3 -m bmwdiag.mapping decode   mappings/obd/engine.yaml rpm "41 0C 0C 3C"
```

See [docs/MAPPING_ARCHITECTURE.md](docs/MAPPING_ARCHITECTURE.md) for the
runtime model, and [docs/MAPPING_RESEARCH.md](docs/MAPPING_RESEARCH.md)
for how a mapping goes from *discovered* to *verified* and what
provenance it has to carry.

**There is no proprietary BMW diagnostic data in this repository.** The
only production mapping is standard, published SAE J1979 Mode 01.
Anything invented is marked `production: false`, uses obviously fake
identifiers, and is excluded from the vehicle runtime.

## N47 research pipeline

`research/` turns pinned public sources into normalized, provenance-
carrying research records and gates which of them may become candidate
mappings. Every fact is labelled (wire observation / SGBD-derived /
source claim / inference), every source is pinned and licensed in
`research/manifests/sources.yaml`, partial knowledge stays partial, and
Tier-D (untraceable) claims never generate anything executable.

```
python3 -m research.build       # re-import, regenerate normalized data + reports
```

The current candidates under `mappings/candidates/` encode three
source-backed request families - the F-series dynamic `0xF303` sequence
(wire-verified on an F25 X3), the E-series DDE7 non-echoing
local-identifier read (raw E90 capture), and an F10 static Mode-22 read
(on-car, N55) - all `production: false` and additionally disabled by an
unknown capability kind until the car's DDE variant is resolved. See
`research/reports/` for the source audit, coverage, conflicts, license
notes and the on-car validation plan.

## Tests

```
python3 -m unittest discover
```

No vehicle, no network, no BMW. The suite includes an exhaustive
regression that decodes every possible input byte through both the
mapping and a frozen copy of the original hardcoded formulas, so a
refactor cannot silently move a telemetry value.

## Tools

Read-only, and separate from the application.

```
python3 tools/egs.py find              # locate the gearbox ECU on the bus
python3 tools/egs.py scan --ecu 0x18   # which UDS 0x22 identifiers answer
python3 tools/export_json.py           # telemetry.db -> JSON for analysis
```

`egs.py` is empirical research tooling: it finds identifiers by asking
the vehicle, never by guessing. Its output is a candidate for the mapping
pipeline described in the research doc, not a mapping.

## Data and privacy

`telemetry.db` and everything under `local/` are gitignored. Recorded
telemetry is tied to a specific vehicle, so it stays out of version
control.

**No VIN appears in this repository.** A mapping records the vehicle it
was verified against by a stable label — `F10-520d-dev` — alongside the
model and engine, which is what a reader actually needs in order to judge
whether a mapping applies to their car. The label-to-VIN table lives in
`local/VEHICLES.md` and is not hosted. `--demo` uses an obviously
synthetic VIN.

If you record telemetry from your own car, note that `runs.vin` in
`telemetry.db` holds the real VIN read from the gateway — which is one
more reason the database is gitignored.
