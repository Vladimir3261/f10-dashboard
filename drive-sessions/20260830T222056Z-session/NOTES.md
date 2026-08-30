# Session 9 (2026-08-30) — stationary verification run + the first real cold start

Not a drive. The car sat parked with the engine idling from cold for 41
minutes, to verify every feature shipped over the last two days and to
capture a cold start. **129,567 samples, one unbroken run, zero
per-request faults, 100% request success.**

The session doubles as the **first genuine cold-start capture** in the
project. Every previous report's "Cold-start warm-up" section was an
artifact of a run that opened at 88–89 °C; this one opens at 21.0 °C
against 17–19 °C ambient.

## The warm-up curve (idle only, no load)

| | start | end | →40 °C | →60 °C | →80 °C | →85 °C |
|---|---|---|---|---|---|---|
| coolant (OBD 0x05) | 21.0 | 88.0 | 7.2 min | 17.3 min | 33.6 min | 36.6 min |
| oil (DDE 0x4517) | 21.3 | 88.0 | 7.3 min | 17.3 min | 33.7 min | 36.7 min |
| engine temp (0x4BC3) | 21.3 | 88.0 | — | — | 33.8 min | — |
| gearbox oil | 21.0 | 45.0 | — | — | — | — |
| charge air | 20.7 | 38.5 | — | — | — | — |
| exh pre-DPF | 14.2 | 95.8 (max 104.2) | — | — | — | — |
| exh pre-cat | 15.3 | 143.8 (max 145.3) | — | — | — | — |

**Coolant and oil track each other to within 0.1 min at every threshold**
over a 67-degree climb. That is the strongest validation the temperature
scales have had: two independently-sourced channels (standard OBD vs a
proprietary SGBD scale) agreeing across the full range from ambient.

### Read the "oil lags coolant" line in the generated report carefully

`report.md` says *"When coolant reached 80 °C, oil was 79.8 °C (oil lags
coolant — the expected warm-up signature)"*. The 0.2 °C is real but it is
**not** the lag the plan predicted, and it should not be cited as such.

Across matched samples oil ran a mean **+0.27 °C ABOVE** coolant, not
below. At idle there is no load, so oil takes its heat from the block
rather than from work done and the two rise together. **The load-driven
oil lag remains uncaptured** — it needs a cold start followed by driving.
This session validates the scales, not the lag.

Gearbox oil reached only **45 °C** while engine oil hit 88. The
transmission barely warms at idle in park; useful as a baseline for what
"cold gearbox" looks like.

## New: the operating-mode word has a cold-start state

`CoEOM_stOpModeAct` (0x467E) took **two** values, and one of them is new:

| value | bits | when | n |
|---|---|---|---|
| `0x80870001` | 0x1, 0x10000, 0x20000, 0x40000, 0x800000 | first **1.2 min** only | 6 |
| `0x100001` | 0x1, 0x100000 | the remaining 39 min | 197 |

The regen bit (0x02) stayed clear throughout, as expected with no regen.

**This corrects an earlier inference.** After drive 7 we noted that
`0x40000` "appears only in the regen state and looks regen-correlated".
That is now falsified: `0x40000` is set here during a cold start with the
regen bit clear. It is more likely a warm-up/enrichment or after-start
flag than anything to do with regeneration. Recorded as a correction
rather than quietly dropped — the earlier note was a guess from a single
observation and this is what testing it looks like.

Known states of 0x467E so far:

- `0x100001` — warm idle, steady
- `0x140002` — regeneration active (drive 7)
- `0x80870001` — first ~1.2 min after a cold start (new)

## New: EGR deviation is not a dead channel

`n47d_egr_deviation` (0x487A) read a flat 0.0 % across all 103,701
samples of drive 7, which put it on the plan's "looks wrong or
unexercised" list. It is fine: here it spans **0.00–5.54 % across 26
distinct values**, all of them during the warm-up transient, settling
back to 0.0 once warm.

So the channel works and the loop is healthy. It simply reads zero when
the engine is warm and steady, which is most of a drive. **The right
place to trend EGR health is the warm-up transient, not cruise.**

## The soot channels are not distance-based

The car did not move: `n47d_dist_since_regen` held at exactly 45.24 km
and `n47d_regen_count` at 93 for the whole session. Yet:

    n47d_soot_meas   9.35 -> 9.52 g   (+0.17 g in 41 min, 0 km)

Across drives 7–8 soot rose ~0.028 g/km, which was consistent with a
per-distance reading. This kills that: **soot accumulates while idling
stationary.** It tracks engine running time and fuel burnt, not distance.

Combined with the drive-8 finding that soot did *not* fall across a
confirmed regeneration, the picture is now:

- **Observed:** soot rises with engine running time, including at zero
  speed, and did not reset at a regeneration.
- **Inference:** these channels are a cumulative soot-produced estimate,
  not current filter load. Two independent behaviours now point the same
  way.
- **Still open:** what resets it, and what the absolute number means
  (0.09 g on 2026-08-25 to 9.52 g now). The next regeneration remains the
  decisive test.

## Feature verification — what was checked

| feature | result |
|---|---|
| `/api/diagnostics` | 25,740 sent / 25,740 ok / **0 failed**, 100% |
| mapping badges | 6 loaded, versions + `extra` flags correct |
| resolution drops | 5, each with a reason: 4 `capability`, 1 `inputs` (`fuel_l`) |
| channel tracing | 46 channels traced to request + mapping version |
| clock handling | survived a live 35-min NTP step at boot; no run straddled it |
| admin panel | LAN-only bind, 401 unauthenticated, independent of `live.py` |
| drive modes | all five in `config/modes.yaml`, seconds, run records `normal` |
| share links | mint / gate / VIN masked / history 404 / revoke all correct |
| `errors` table | correct shape, **0 rows — untested, not proven** |
| sync | 129,567 rows to the lake, `pending 0` |

The `errors` table was never exercised because nothing failed. Treat it
as unverified until a session actually produces a fault.

## A regression found and fixed during this session

`runs.mapping_set` was empty and `run_mappings` had no rows — the link
`docs/DATA_VERSIONING.md` relies on to tie a dataset to the mapping
revision that produced it.

Cause: in the car path `start_run()` was called ~49 lines before
`set_metadata()`, so the recorder thread inserted the run while
`meta_source` was still `None`. The demo path had them the right way
round, which is why it was never noticed.

Every session on disk shows the same signature — run 1 empty, every later
run fine:

    drive-20260829T152727Z   4 runs   X...
    drive-20260829T155205Z   6 runs   X.....
    drive-20260829T164441Z   6 runs   X.....
    drive-20260830T213912Z   1 run    X

**Worth remembering as a category:** while sessions fragmented into 4–6
runs this cost ~18% of runs and the provenance survived in the rest. Once
the fragmentation fix landed and a session became ONE run, it became
100%. Fixing one defect removed the thing that was masking another.
*When you stop work being split into units, anything that was only wrong
on the first unit becomes wrong on all of it.*

Fixed in `67adee5` (metadata snapshot now travels in the payload rather
than being read from shared state at pop time; a run that cannot say what
it is recording no longer opens silently). Verified live immediately
after: the next session's run 1 carries a full `mapping_set` and 6
`run_mappings` rows.

**This session keeps the gap.** `params.mapping_ver` is intact (2/3/5, now
genuinely per-file), so every channel is still attributable to a mapping
version — but the run-level record for today's three sessions is not
recoverable and must not be back-filled.

## Caveats

- **Idle only.** No load, no boost, no speed. Nothing here says anything
  about behaviour under load, and the load-dependent baselines are
  untouched.
- **DPF ΔP read −13.0 to −4.0 hPa** the whole session — the expected
  near-zero-flow sensor offset at idle, not a fault, and not usable for
  restriction trending without exhaust flow.
- **Lambda** sat at the 2.0 sentinel for 193 of 245 samples.
- Ambient/baro cross-check 8.94 hPa mean — the baro PID's 1 kPa
  quantisation, agreement within resolution.
