# f10-dashboard

Real-time engine telemetry for a BMW F10 over ENET, with a live web
dashboard and a SQLite time-series log.

The application speaks HSFZ — BMW's diagnostic-over-IP framing — to the
central gateway on TCP 6801, routes standard OBD-2 service 01 requests to
the engine ECU, and serves a dashboard over server-sent events.

**Read-only.** Only service `0x01` (current data) requests are sent, plus
HSFZ alive-check replies. Nothing is ever written to the vehicle.

```
python3 live.py                 # discover the car, serve on :8080
python3 live.py --ip 169.254.x.x
python3 live.py --demo          # no car needed, simulated data

# also poll the verified F-series N47 proprietary channels (oil/coolant/
# engine temp, DPF soot, rail/boost pressure, MAF, ...). They activate
# only on an ECU that answers their variant probe:
python3 live.py --extra-mappings mappings/candidates/bmw/dde/n47
```

The dashboard has three views, switchable in the header:
**Drive** (big gauges + live strips for the fun realtime metrics),
**Detail** (per-channel history graphs), and **All data** (a dense table
of every channel with running min/max and age).

Requires **Python 3.9+** and nothing else. There are no third-party
dependencies, deliberately: this runs on a laptop in a car, where
`pip install` is not always an option.

---

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
                     capability-gated - never polled until verified
  verified/          empty until something is verified on the car
  examples/          synthetic fixtures, excluded from the runtime
research/            the N47 evidence/import pipeline (offline only):
                     pinned source manifest, importers, normalized
                     records, conflict detection, reports
tests/               259 tests; no car and no network required
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
    polling: {class: fast}
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

A development CLI works without a vehicle:

```
python3 -m bmwdiag.mapping validate mappings/
python3 -m bmwdiag.mapping list     mappings/
python3 -m bmwdiag.mapping show     mappings/obd/engine.yaml
python3 -m bmwdiag.mapping plan     mappings/obd --slow-every 10
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
