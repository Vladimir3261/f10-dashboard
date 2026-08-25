# Mapping research and provenance

This document describes how a diagnostic mapping gets from "something we
noticed" to "something the car is polled for", and what has to be
recorded along the way.

**The runtime does not participate in any of this.** `live.py` loads
validated mapping files and nothing else. No importer, extractor,
decompiler or vendor tool is a runtime dependency, and none ever will be.

---

## Lifecycle

```
    external source
        |
        v
    importer / extractor              (offline tooling, not runtime)
        |
        v
    candidate YAML                    verification.status: candidate
        |
        v
    validation against known output or a vehicle
        |
        v
    verified YAML                     verification.status: verified
        |
        v
    production registry               mappings/<family>/
```

### `discovered`

Something exists. A job name in an SGBD, an identifier that answers, a
byte in a trace that moves with the throttle. Enough to write down, not
enough to decode.

A discovered mapping may name a request with no signals, or signals whose
`decode` is a placeholder `bytes` read. It is a research note in the
project's format, and it must never be `production: true`.

### `candidate`

A decode has been proposed: offset, primitive, scale, unit. It is
plausible and internally consistent, and it has not been confirmed
against anything.

Most importer output lands here. **A candidate must never reach the
vehicle runtime.** Mark the file `production: false`, or keep it outside
the directory `--mappings` points at.

### `verified`

The decode has been confirmed against an independent source of truth, and
`verification.method` says which:

* the same value read by Tool32 / ISTA on the same vehicle at the same
  moment;
* a captured request/response pair whose value matches a known physical
  state (engine off, coolant at ambient, tank just filled);
* a channel that tracks a second, already-verified channel through a
  drive in the way physics requires.

"It looks about right" is not verification. Neither is "the formula came
from a forum post".

Record the vehicle. A mapping verified on one variant is `candidate` for
another until someone checks.

**Record it by label, not by VIN.** `verification.vehicle` carries a
stable identifier such as `F10-520d-dev` together with the model and
engine, which is everything a reader needs in order to judge whether a
mapping applies to their car. A VIN identifies a specific vehicle and its
owner, and does not belong in a hosted repository. The label-to-VIN table
lives in `local/VEHICLES.md`, which is gitignored.

### `rejected`

Tried, wrong, and kept so nobody proposes it again. Say why in
`verification.notes`. The synthetic example fixture is also marked
`rejected`, so no importer, report or registry query can mistake it for
knowledge.

---

## Recording provenance

Every mapping file, request and signal can carry:

```yaml
source:
  type: obd_standard | prg | ediabas | tool32 | ista | trace | manual | synthetic
  file:    ...      # the artefact this came from
  sgbd:    ...      # SGBD / ECU description name
  job:     ...      # diagnostic job name
  result:  ...      # result name within that job
  notes:   ...

verification:
  status:  discovered | candidate | verified | rejected
  method:  ...      # how it was confirmed
  vehicle: ...      # which car, which variant
  notes:   ...
```

File-level values are inherited by every request and signal in the file
and can be overridden at either level, so a file of mostly-verified
channels can carry one candidate without lying about the rest.

None of this affects decoding. All of it survives loading and is
queryable — `python3 -m bmwdiag.mapping show <file>` prints it, and
`mapping.signals[i].verification.status` is available to any tool.

### What each source type must record

| `type` | minimum |
| --- | --- |
| `obd_standard` | the standard and service (`SAE J1979 ... service 01`) |
| `prg` | PRG/GRP filename, SGBD name, job, result |
| `ediabas` | SGBD name, job, result, and how the job was invoked |
| `tool32` | SGBD, job, arguments, and the output that was read |
| `ista` | which procedure or measurement, and the ISTA version |
| `trace` | capture file, the tool that produced it, and the vehicle state |
| `manual` | who worked it out and from what |
| `synthetic` | that it is invented, and that it is a fixture |

A mapping whose provenance cannot be written down is not ready to be a
mapping.

---

## Rules for proprietary mappings

1. **No guessed identifiers.** No DID, ECU address, job or result goes
   into any file — even a candidate — unless it came from a named source
   or was observed answering on a real vehicle. A plausible-looking
   number is worse than no number: it survives, gets copied, and is
   eventually believed.

2. **No unsourced BMW knowledge.** Values recalled from a forum, a blog,
   another project without a licence, or a model's own memory are not
   sources. If it cannot be traced to an artefact or a capture, it does
   not go in.

3. **Addresses are never assumed.** Mapping files name capabilities and
   late-bound targets. If a mapping must name a fixed address, it needs a
   provenance entry saying which vehicle it was observed on.

4. **Fixtures are labelled and excluded.** Anything invented is
   `production: false`, `source.type: synthetic`,
   `verification.status: rejected`, and uses obviously fake values
   (`ECU 0x7E`, `DID 0xF001`). See `mappings/examples/uds_example.yaml`.

5. **Read-only.** Mappings describe reads. Nothing in the format can
   write to a vehicle, and nothing should be added that can.

---

## The importer contract

A future importer — PRG/GRP extractor, BEST bytecode decompiler, Tool32
scraper, ISTA reader, ENET trace analyser — is an offline tool that
**emits YAML and nothing else**. It must be able to produce a candidate
mapping without any runtime code changing.

That means an importer must not need:

* a new decoder primitive for a shape the format already covers;
* a poll-loop change to schedule its request;
* an application change to display or record its channel.

If an importer would need one of those, the *format* needs extending
first — deliberately, with a schema version bump — and then the importer
is written against it.

Importer output should be written to a staging directory, not to
`mappings/`, and promoted by a human after validation.

```
python3 -m bmwdiag.mapping validate staging/
python3 -m bmwdiag.mapping show   staging/candidate.yaml
python3 -m bmwdiag.mapping decode staging/candidate.yaml <signal> "<captured response>"
```

The `decode` command against a captured response is the cheapest possible
validation step and should be the first one taken.

---

## Extending the format

The mapping format is versioned (`schema_version: 1`). Adding an
optional field with a safe default does not need a version bump. Changing
what an existing field means, or making a new field required, does.

`bmwdiag.mapping.model.SCHEMA_VERSIONS` lists what a build understands;
anything else fails at load with a readable error rather than being
half-interpreted.

Before adding a decoder primitive or a derived operation, check whether
the existing set covers the shape. The set is deliberately small, and
`bitfield` + `lookup` + the `pre_add/scale/divide/add` pipeline cover
more than they look like they do. What must **never** be added is an
expression language or anything else that makes a mapping file
executable.

---

## Current state

| Mapping | Status | Notes |
| --- | --- | --- |
| `mappings/obd/engine.yaml` | `verified` | Standard SAE Mode 01. Polled live and logged on `F10-520d-dev`. |
| `mappings/candidates/bmw/dde/n47/d72n47a0_dynamic.yaml` | `candidate` | F-series dynamic `0xF303` reads (oil temp, DPF soot, engine temp). Wire-verified on an F25 X3 (source); NOT verified on `F10-520d-dev`. |
| `mappings/candidates/bmw/dde/n47/dde7_kwp_local_id.yaml` | `candidate` | E-series DDE7 non-echoing local-id read, from the raw E90 capture in WiCAN issue #752. |
| `mappings/candidates/bmw/dde/n47/f10_static_58xx.yaml` | `candidate` | F10 static `22 586F` oil pressure (u16 millibar), on-car verified on an F10 **N55** — engine family differs. |
| `mappings/examples/uds_example.yaml` | `rejected` | **TEST FIXTURE — NOT A REAL BMW MAPPING.** Every value invented. |

Every candidate is `production: false` and additionally gated behind a
capability kind no current provider satisfies, so none can reach a
vehicle until the DDE variant is resolved and a human promotes it. The
evidence, pins, licenses and conflict analysis behind them live under
`research/` — see `research/reports/n47-source-audit.md` for where every
byte came from, and `research/reports/n47-unresolved-questions.md` for
the supervised on-car validation sequence that would promote (or reject)
each one.
