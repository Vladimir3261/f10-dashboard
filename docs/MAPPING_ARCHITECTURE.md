# Mapping architecture

Vehicle-specific diagnostic knowledge lives in versioned mapping files
under `mappings/`, not in application code. `live.py` knows how to talk
HSFZ, discover a gateway, serve a dashboard and write SQLite; it does not
know what a PID is, what `rpm` means, or how to turn two bytes into an
engine speed.

```
    HSFZ transport
         |
         v
    mapping engine          (bmwdiag.mapping)
         |
         v
    normalised telemetry signals      {"rpm": 783.0, "coolant": 91.0}
         |
         +--> Telemetry state  -> SSE -> dashboard
         +--> SQLite Recorder
```

---

## The pieces

| Module | Responsibility |
| --- | --- |
| `bmwdiag/mapping/model.py` | frozen dataclasses; the whole runtime vocabulary |
| `bmwdiag/mapping/yamlsubset.py` | dependency-free parser for the mapping YAML subset |
| `bmwdiag/mapping/loader.py` | parse + validate one file into a `MappingFile` |
| `bmwdiag/mapping/versioning.py` | the `VERSIONS.lock` ledger (build/check) |
| `bmwdiag/mapping/registry.py` | hold every file; resolve against one vehicle |
| `bmwdiag/mapping/decoder.py` | primitives, transformations, response matching |
| `bmwdiag/mapping/derive.py` | computed channels, closed set of named operations |
| `bmwdiag/mapping/polling.py` | which requests are due this iteration |
| `bmwdiag/mapping/execute.py` | request -> transport -> decode -> signal values |
| `bmwdiag/protocol/request.py` | `DiagnosticTransport`, `ObdPidReader`, payload building |
| `bmwdiag/obd/capability.py` | Mode 01 support bitmasks — OBD only, nothing generic |

`bmwdiag` imports nothing outside the standard library, and nothing in it
opens a socket.

Every mapping file also carries an integer `mapping.version` that is
stamped onto the data it decodes, so a recorded dataset ties back to the
exact mapping revision that produced it. That mechanism — the versioning
rule, the `VERSIONS.lock` ledger, and where the version is stored through
to the lake — is documented separately in
[`DATA_VERSIONING.md`](DATA_VERSIONING.md).

---

## Requests versus signals

A **request** is one thing that goes on the wire. A **signal** is one
normalised telemetry channel decoded out of the reply. A request owns one
or more signals.

This is the distinction the whole design turns on:

* the polling plan schedules **requests**, so three signals decoded from
  one 6-byte reply cost one exchange, not three;
* adding a signal to an existing request adds **no traffic at all** —
  it is a mapping edit with zero runtime cost;
* the recorder and the dashboard consume **signals** and never learn
  which request, protocol or ECU produced them.

```yaml
requests:
  obd.mode01.0C:
    protocol: obd
    pid: 0x0C
    polling: {class: motion}
    response: {data_length: 2}
    signals:
      rpm:
        label: Engine speed
        unit: rpm
        display: {digits: 0, min: 0, max: 5000}
        decode: {type: uint16_be, divide: 4.0}
```

### Protocols

`protocol` selects how a payload is built and what a valid reply looks
like. Nothing assumes UDS, and nothing assumes service `0x22`.

| protocol | payload | default expected prefix |
| --- | --- | --- |
| `obd` | `service, pid` | `service+0x40, pid` |
| `uds` | `service, did_hi, did_lo` | `service+0x40, did_hi, did_lo` |
| `raw` | the literal `payload:` bytes | whatever `response.prefix` says |

`payload:` overrides everything and is the escape hatch for a
proprietary job that fits no service/identifier convention:

```yaml
  vendor.job.example:
    protocol: raw
    payload: "31 01 AB CD"
    response:
      prefix: "71 01 AB CD"
      data_length: 4
```

### Setup sequences

Some reads are a *sequence*, not a single exchange. The BMW F-series
dynamic measurement is the motivating case: clear a dynamic identifier,
define it from a source id, then poll it. `setup:` holds the frames
that go out **once per session**, in declared order, before the request
is first polled:

```yaml
  n47.d72.dyn.4517:
    protocol: uds
    service: 0x22
    did: 0xF303
    setup:
      - "2C 03 F3 03"                # clear dynamic define
      - "2C 01 F3 03 45 17 01 02"    # define F303 := source 0x4517
    response: {data_length: 2}
```

The polled request stays the honest `22 F3 03` - a sequence is data,
never one fabricated identifier. The executor tracks which setup is
currently armed on each ECU and re-arms only when it changes: a channel
polled repeatedly arms once, while several channels that share one
dynamic DID (all the F303 reads) re-arm as they take turns, so the
define in front of a `22 F3 03` always matches the poll that follows it.
A reconnect builds a fresh executor, which re-arms from scratch. This is
what lets many proprietary measurements multiplex safely over a single
dynamic identifier.

### Variant capability

A mapping that reads a proprietary measurement declares which SGBD
variant it applies to, and the ECU must *earn* that match by answering:

```yaml
ecu:
  match:
    capability:
      sgbd_variant: d72n47a0
```

`bmwdiag/variant.py` answers "is this ECU that variant" the same way the
OBD layer answers "does it support this PID" - by probe, never by
address or an ident string. `VariantProbe` replays a variant's own
dynamic read on connect and confirms the variant only if the ECU answers
in the expected shape; `CombinedCapabilitySet` then lets one ECU satisfy
both `obd_mode01_pid` and `sgbd_variant` questions from two independent
providers. On a base (OBD-only) load there are no variant-gated mappings
and the probe never runs.

Response matching is unchanged: the expected prefix is per-request
data, which is also how a protocol whose replies do not echo the
identifier (the DDE7 `6C 10` shape) is declared without loosening
matching anywhere else.

---

## Decoding

```
    raw = primitive(payload[offset : offset + width])
    raw in `invalid`             -> no value this cycle
    enum                         -> string
    lookup                       -> piecewise-linear interpolation
    otherwise                       ((raw + pre_add) * scale) / divide + add
    outside valid_min/valid_max  -> no value this cycle
    round to `round` digits         (default 3)
```

Offsets are measured from `response.payload_offset`, which defaults to
the length of the expected prefix. A signal therefore never has to know
how long the service echo in front of it was.

**The order of `scale` and `divide` is part of the contract.** The
standard OBD load formula is `A * 100 / 255`, and expressing it as a
single precomputed factor changes the last float digit. Each step is
skipped when it is at its identity value for the same reason:
`divide: 4.0` alone stays an exact `raw / 4.0`.

Primitives: `uint8` `int8` `uint16_be/le` `int16_be/le` `uint24_be/le`
`uint32_be/le` `int32_be/le` `float32_be/le` `bytes` `ascii` `bit`
`bitfield`.

`bitfield` auto-shifts by its mask's trailing zeros, so a mapping states
where a field is once rather than twice. `bit` indexes from the least
significant bit of the window.

### No executable YAML

There is no expression language and no `eval`, in the decoder or in
derived signals. A mapping file is data. This is not a stylistic
preference: mapping files will eventually be produced by importers from
third-party sources, and a format that can execute is a format that can
be attacked.

---

### Decoded but not stored — `log: false`

A signal or derived channel may declare `log: false`. It is still read,
still decoded and still shown on the dashboard; the recorder simply never
writes a row for it.

```yaml
      egs_da2e_b0:
        decode: {type: uint8, offset: 0}
        log: false
```

This is for a channel whose *finding is that it does not change*.
`egs_da2e_b0` shares one 2-byte response with `gear`, so reading it costs
nothing on the wire — but it wrote 124,485 rows carrying a single distinct
value over three days. Deleting the channel would discard a documented
result and stop us noticing if it ever became non-zero; storing it forever
buys nothing. `log: false` keeps the observation and drops the cost.

Two deliberate properties:

- **It never affects decoding.** Like everything else in the format it is
  data, not behaviour — the decoder does not consult it.
- **The safe direction is to log.** An unknown channel, or a recorder with no
  resolved profile, stores everything. Silently dropping data has to be
  something a mapping asks for explicitly.

## Derived signals

Channels computed from other channels are declared, not coded:

```yaml
derived:
  boost:
    label: Turbo boost
    unit: bar
    operation: subtract_scale
    inputs:
      value: map
      reference: baro
    fallback:
      reference: 100.0
    divide: 100.0
    round: 3
    trigger: [map]
```

Operations are a closed set — `linear`, `subtract_scale`,
`divide_scale`, `sum`, `product`, `ratio` — each with a documented
multiply/divide order for the same float-exactness reason as above.

* `trigger` decides *when* a channel recomputes. `boost` recomputes when
  a fresh manifold pressure arrives, not when a fresh barometric one
  does; `baro` is a slow channel and its carried-forward value is used.
* `fallback` supplies a value for a role that has not been read yet, so
  `boost` works before the first barometric reading.
* Runtime configuration enters through `{config: <name>}`, which is how
  `--tank` reaches both `fuel_l`'s scale and its display maximum without
  the mapping hardcoding a tank size.

A derived channel whose non-fallback inputs are not being read is dropped
during resolution rather than published as a channel that can never
produce a value.

---

## Polling

Polling classes are named in mapping files and defined at runtime:

```yaml
polling_classes:
  motion: {seconds: 0.1, priority: 0}
  slow:   {seconds: 10,  priority: 2}
  survey: {seconds: 300, priority: 3}
```

**One unit: `seconds`, wall clock.** A request is due when that many
seconds have passed since it last went out. The poll loop's `--rate` sets
only the granularity — asking for a period shorter than one loop cycle
just means "every cycle".

`hz`, `every` and `cycles` were accepted until 2026-08-30 and are now
refused by name at load time (silently ignoring them would poll at the
wrong rate). `hz` and `seconds` were the same thing spelled differently;
`cycles` was worse than redundant, because it rescaled every class
whenever `--rate` changed, so one mapping file meant different sample
rates on different launches. Seconds won over Hz because the declared
rates are mostly slow: `{seconds: 60}` reads cleanly where
`{hz: 0.01666…}` does not.

There is no CLI rate override. A rate is a property of what the channel
measures, so it belongs in the mapping file; wanting everything faster or
slower for one drive is what a **drive mode** is for — and unlike a flag,
a mode is recorded with the data. Precedence is
**mapping file > built-in fallback**.

`PollingPlan.due()` returns requests ordered by class priority then
declaration order. That ordering is load-bearing: the OBD session batches
PIDs six at a time in list order, so a stable order keeps the traffic
byte-identical to the pre-mapping implementation.

### Staggered classes

A class marked `stagger: true` fires **one member per firing**,
round-robin, instead of all its due members at once:

```yaml
polling_classes:
  dde_dyn: {seconds: 0.5, priority: 5, stagger: true}
```

This exists for the F303 dynamic reads. Each is a three-frame exchange
(clear + define + read ≈ 90 ms), so firing thirteen of them on one cycle
stalls the fast OBD channels for over a second. Staggered, at most one
proprietary read lands per cycle, bounding the stall to ~90 ms while the
fast channels keep their cadence.

Note the arithmetic: **`seconds` is the gap between firings of the
CLASS**, and one member goes out per firing, so a member's own refresh
interval is `seconds × members` — 22 requests at `{seconds: 0.5}` means
each channel is read every ~11 s. The class is therefore timed as a
whole; timing its members individually would make it fire every cycle,
since the members not chosen never have their clock advanced. `stagger`
is opt-in, so the standard-OBD traffic is unaffected.

---

## ECU matching and capability

Mapping files never name a diagnostic address for a discovered ECU. They
declare what an ECU must be able to do:

```yaml
ecu:
  family: engine
  target: discovered_engine
  match:
    capability:
      obd_mode01_pid: 0x0C
```

`discovered_engine` is a late-bound target. The application scans the
bus, decides which ECU is the engine, and passes
`targets={"discovered_engine": addr}` into `registry.resolve()`.
**Address is never evidence** — the engine is whichever ECU advertises
engine speed.

Requests carry their own requirements. An OBD request derives its
capability from its PID automatically, so a mapped PID is enabled exactly
when the ECU advertises it in the Mode 01 support bitmask.

Answering "does this ECU satisfy this capability" is protocol-specific
and lives in `bmwdiag/obd/capability.py`. `01 00`, the bitmask layout and
the next-block bit appear nowhere in the generic mapping layer. A future
BMW-specific capability provider (identification job, ECU variant, SGBD)
implements the same tiny `CapabilitySet` interface and sits beside the
OBD one, neither knowing about the other.

An ECU that answers OBD but publishes no support bitmask has
`known == False` and satisfies everything, which preserves the original
behaviour of polling the whole table in that case.

---

## The registry

`MappingRegistry` holds loaded files and rejects collisions between them
(duplicate request ids, duplicate signal keys). `resolve()` produces a
`ResolvedProfile` — the mapping layer's answer to *what can we read from
this car*:

```python
registry = MappingRegistry.from_tree("mappings", production_only=True)
profile  = registry.resolve(
    engine.capabilities(),
    config={"tank": args.tank},
    targets={"discovered_engine": engine.addr},
)
```

The profile is the single source of downstream metadata:

| consumer | call |
| --- | --- |
| dashboard channel list | `profile.meta()` |
| recorder `params` row | `profile.param_row(key)` → `(pid, label, unit)` |
| OBD multi-PID walking | `profile.obd_pid_lengths()` |
| polling | `PollingPlan(profile.requests, classes)` |
| derived channels | `profile.apply_derived(values, fresh)` |

`param_row` returns `pid=None` for anything that did not come from OBD,
which the existing `params.pid` column already allows. No schema change
was needed and none was made.

---

## Why the transport is separated

```python
class DiagnosticTransport(Protocol):
    def request(self, payload: bytes, *, dst: int,
                timeout: Optional[float] = None) -> bytes: ...
```

`HsfzTransport` in `live.py` adapts the existing `HsfzClient` to this in
four lines. HSFZ itself was not touched.

The separation buys three things:

1. **Testability without a car.** Every decoder, every formula and the
   whole execution path are exercised against byte literals. The test
   suite needs no vehicle, no network and no BMW.
2. **Replayability.** A captured ENET trace is a `DiagnosticTransport`.
   That is how a candidate mapping gets validated against recorded
   traffic before it is ever sent to a vehicle.
3. **Containment.** Reconnect policy, alive-checks, gateway NACK
   handling and the ZGW's habit of dropping the TCP session all stay in
   `live.py`, where they were already correct.

Standard OBD gets a second, narrower interface — `ObdPidReader` — because
it is the one protocol where the wire framing is not one exchange per
mapped request. An ECU may answer six PIDs at once and may stop doing so
mid-drive; that negotiation belongs to `ObdSession`, not to the mapping
engine. The executor rebuilds the logical `41 <pid> <data...>` response
from what the session returns, so prefix matching and offsets work
identically for OBD and for everything else.

---

## The YAML subset

Mapping files are parsed by a bundled parser rather than PyYAML. The
runtime deliberately has no third-party dependencies — this code runs on
a laptop in a car, where `pip install` is not always an option — and a
mapping file must not load on one machine and fail on another because the
two have different YAML libraries.

Supported: block mappings and sequences, indentation nesting (spaces
only), inline flow collections, `|` and `>` block scalars, comments,
`---` markers, and scalars including `0x`/`0o`/`0b` integers, floats,
booleans, `null` and quoted strings.

Rejected loudly: anchors, aliases, tags, merge keys, multi-line flow
collections, tab indentation. `yes`/`no`/`on`/`off` are **strings**, not
booleans, so an enum value named `off` means what it says.

---

## Validation

The loader is where a bad mapping dies, with the file and the path inside
it in the message. It catches unsupported schema versions, missing
required fields, unknown decoder types, negative or out-of-range offsets,
impossible lengths and bit indices, masks too wide for their window,
malformed enum and lookup tables, `divide: 0`, duplicate request ids,
duplicate signal keys (within a file and across the registry), unknown
derived operations, and derived inputs, triggers or fallbacks naming
signals nobody provides.

Everything raises a `MappingError` subclass, never a bare `KeyError`.

---

## Development CLI

Works without a vehicle:

```
python3 -m bmwdiag.mapping validate mappings/
python3 -m bmwdiag.mapping list mappings/
python3 -m bmwdiag.mapping show mappings/obd/engine.yaml
python3 -m bmwdiag.mapping plan mappings/obd
python3 -m bmwdiag.mapping request mappings/obd/engine.yaml obd.mode01.0C --target 0x12
python3 -m bmwdiag.mapping decode mappings/obd/engine.yaml rpm "41 0C 0C 3C"
```

It is deliberately separate from `live.py`'s runtime CLI: mapping work is
a research and authoring activity, and should never require the vehicle
stack.

---

## Production versus fixtures

`mappings/obd/engine.yaml` is the production mapping: standard SAE Mode
01 only, `source.type: obd_standard`, every target late-bound.

`mappings/examples/uds_example.yaml` marks itself `production: false`.
`live.py` loads with `production_only=True` and skips it; the CLI and the
test suite load it so a broken fixture cannot go unnoticed. Every
identifier in it is invented, and it says so on every second line. No
value in it is a claim about any vehicle.
