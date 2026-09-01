# Vehicle profile — configuration as an analytics input

A health system must know whether the component it is evaluating exists.

## Why this exists

This car's particulate filter was removed. The analysis layer did not
know that, and went on reporting DPF restriction baselines, soot
accumulation and differential-pressure health as though a filter were
fitted. Those conclusions were not merely uncertain — they were
**impossible**: `n47d_dpf_dp` measures the pressure drop across an empty
pipe.

It had already cost real time. The soot decode was chased as a scaling
bug across several sessions because nobody had written the hardware
configuration down; see [`DPF_SOOT.md`](DPF_SOOT.md). The fix is not a
special case for DPF — it is to make vehicle configuration a first-class
input that any analysis can interrogate.

## The file

Copy `config/vehicle-profile.example.yaml` to
`local/vehicle-profile.yaml` (gitignored) and edit:

```yaml
vehicle:
  label: F10-520d-dev
  model: BMW F10 520d
  engine: N47

hardware:
  dpf: false        # removed
  egr: true

modifications:
  - type: dpf_removed
    at: unknown
```

**No VIN.** The car is identified by its stable label; the label→VIN
table lives in `local/VEHICLES.md` and is never committed. A test asserts
the committed example contains no VIN-shaped token.

## Three states, and the third is the point

| state | meaning | effect on conclusions |
|---|---|---|
| `present` | the hardware is fitted | conclusions may be drawn |
| `absent` | it is not fitted | physical conclusions are **VOID** |
| *not listed* → `unknown` | nobody has said | **NOT EVALUATED** |

`unknown` behaves like `absent` when deciding whether to conclude, and
unlike it when reporting why. An unconfigured checkout must not start
asserting DPF health — and must not claim the filter was removed either.
That is why `has()` returns False for unknown and `is_absent()` also
returns False.

## Three kinds of channel, only one of which needs the hardware

The DPF case shows why a blanket "hide everything" would be wrong:

| kind | example | with no filter |
|---|---|---|
| **physical sensing** | `n47d_dpf_dp` | **void** — measures an empty pipe |
| **ECU model output** | the two soot channels | still reported, explicitly *as the model* |
| **commanded action** | regen count, distance-since-regen | **fully meaningful** |

The regeneration count is the one that got *more* interesting when the
filter left: the ECU still commands regens against its internal model, at
a real cost in fuel and oil dilution, and they now clean nothing. That
cost is a finding, so it is deliberately never suppressed.

## Configuration is provenance, not a setting

The profile file describes the car **today**. Using it to interpret an
old drive would relabel history:

```text
run A recorded while the DPF was fitted
        ↓  filter removed, profile updated to dpf: false
analyse run A again
        ↓
run A's differential-pressure readings declared VOID
        - a statement about hardware that DID exist at the time
```

The reverse is as bad after a part is restored or replaced. This is the
same defect as `params.mapping_ver` in #5, one layer over, and it is
fixed the same way.

**The configuration is snapshotted onto the run when it is recorded.**
`runs.vehicle_label` and `runs.vehicle_hardware` hold the stable label
and a deterministic `subsystem=state,…` fingerprint, frozen on the
calling thread at `start_run()` exactly as mapping provenance is. The
analysis resolves through the run:

| `vehicle_provenance` | meaning |
|---|---|
| `run` | snapshotted when recorded — **authoritative for this drive** |
| `current` | today's profile standing in for a run that predates the field — labelled as such in the report, never presented as historical fact |
| `none` | nothing configured; every subsystem unknown |

The fingerprint is a flat string on purpose: what is stored on a run has
to be readable back without carrying a schema along with it, and two runs
under the same configuration must produce byte-identical fingerprints or
a change of nothing would look like a change of something.

The same pair of fields rides to the lake on `sessions`, so ClickHouse
analytics can condition on what was true for that session rather than on
a present-day toggle that would reinterpret every historical drive.

## Where it is enforced

- **`analysis/vehicle_profile.py`** — the profile, the three states, the
  loader. A missing file is not an error.
- **`analysis/session_report.py`** — `dpf()` is capability-aware and
  `findings()` will not state that differential-pressure sensing is
  healthy on a car with no filter. `--vehicle-profile` overrides the path.
- **`analysis/clickhouse/insights.sql`** — sections 2 and 4 are gated on
  `{dpf_present:UInt8}` and return an explanatory row instead of a
  baseline. Section 8 (regenerations) is deliberately **not** gated. The
  parameter is required rather than defaulted, so nobody reads section 2
  without having answered the question.
- **`infra/grafana/dashboards/f10-health.json`** — a `dpf_present`
  dashboard variable (default `0`) gates the two DPF panels, which are
  titled VOID.

The `{dpf_present:UInt8}` parameter and the `dpf_present` Grafana
variable are named for the *query*, while the profile key is the generic
`hardware.dpf` capability. They are the same fact in two vocabularies:

    profile   hardware: {dpf: false}      -> capability `dpf` is absent
    query     --param_dpf_present=0       -> section 2/4 skipped
    Grafana   $dpf_present = 0            -> DPF panels void

The generic key is deliberate, so the mechanism extends to any subsystem
without inventing a new `*_present` flag per part. Do not let the two
drift into independently maintained concepts: the query parameter is a
*projection* of the capability, not a second source of truth.

Once `sessions.vehicle_hardware` is populated in the lake (migration
`2026-09-01_vehicle_configuration_provenance.sql`), lake-side gating
should read the session's own snapshot instead of the global parameter,
which removes the last place a present-day toggle can reinterpret an old
drive.

## Adding a subsystem

Nothing in the profile knows what a DPF is. Add a key under `hardware:`
and ask for it:

```python
if not run["vehicle"].has("egr"):
    ...        # withhold EGR-health conclusions
```

The same mechanism covers a remap, an EGR delete, replaced sensors, a
different battery — anything where a conclusion depends on the part being
there.
