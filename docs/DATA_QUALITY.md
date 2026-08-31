# Data quality (Stage 1)

Why storage has to record *why* a value is not trustworthy, what the lake
says is actually wrong today, and how the layer is being built.

Status: built. The decoder carries quality, storage records it end to
end, and the production mapping declares the two cases below that it can
prove. Nothing has driven with it yet, so no flagged row exists in the
lake — the first drive on this code is what will show it working.
Numbers below are from the lake on 2026-08-31, 2,862,258 samples across
115 sessions and 7 drives, all of them recorded before this layer.

## The defect

`samples(run_id, ts, param_id, value)` stores a number and nothing about
whether it means anything. Three different situations collapse into the
same storage:

| what happened | what storage held |
|---|---|
| the ECU returned a real reading | a row |
| the ECU returned its "no value" sentinel | **no row** |
| the channel was never polled | **no row** |
| the sensor was pinned at the top of its range | a row, indistinguishable from a real one |

The second and third being identical is the sharp end. `decoder.py`
returned `None` for anything listed in `invalid` or outside
`valid_min`/`valid_max`, and the caller skipped it — so a channel the car
actively reports as unavailable looks exactly like one nobody asked for.
`/api/diagnostics` closed the neighbouring hole (a request that is sent
and never answered) but cannot see this one: the request *succeeded*.

The lake has carried a `quality` enum since its first day —
`Enum8('ok','saturated','sentinel','stale','clipped','decode_fail')` —
and **nothing has ever written it. All 2,862,258 rows are `ok`.**

## What is actually wrong in the data

Four findings, in descending order of how much they distort analysis.

### 1. `engine.lambda` — 114,138 sentinel rows stored as readings

Raw `0xFFFF` on OBD PID 0x24 decodes, through `divide: 32768.0`, to
exactly **2.0** — the top of the channel's own declared display range. It
is the ECU saying "no value", and it is stored as if the mixture were 2.0.

**57.4% of all lambda samples are this sentinel** (114,138 of 198,721),
and it is not a legacy artifact: 60.7% under the current mapping
generation. Every mean, baseline or trend computed over lambda today is
wrong, and wrong in a direction that looks plausible.

*Confidence: observed fact.* The bit pattern, the arithmetic and the
prevalence are all directly measured.

### 2. `engine.manifold_pressure.absolute` — a hard byte ceiling

`map` decodes as `{type: uint8}`, so the channel physically cannot report
above 255 kPa. In the lake:

| value | samples |
|---|---|
| 250 | 172 |
| 251 | 191 |
| 252 | 178 |
| 253 | 174 |
| 254 | 190 |
| **255** | **6,756** |

A 36× spike at exactly the byte boundary. This is not a sensor that likes
255 kPa; it is every value at or above 255 kPa landing on the same row.
That the car genuinely goes past it is independently confirmed: the
proprietary flow channel recorded boost actual at 2786 hPa = 278.6 kPa.

So 255 means "255 or more", and the true value is **not recoverable** —
which is exactly why it has to be labelled rather than repaired.

*Confidence: observed fact.*

### 3. `engine.maf` — 222.22 g/s at zero rpm, cause unknown

13,622 samples sit at exactly 222.22 g/s (raw 22222), and **13,601 of
them are at rpm = 0**. 222 g/s with the engine stopped is impossible. At
rpm = 0 the channel alternates between ~1.1 g/s and this single value;
above idle it essentially never appears (73 of 176,942 samples).

It is live, not historical — 9.9% of MAF samples under the current
mapping generation.

**This is deliberately not encoded as a sentinel.** 22222 is not a
natural bit pattern, nothing in any source documents it, and declaring
`invalid: [22222]` on a guess would be invented BMW data. It needs the
raw PID 0x10 response captured on the car with ignition on and engine
off; it is on the car-side to-do in
`research/reports/n47-next-session.md` alongside the EGR outlier.

**Until then: MAF at rpm = 0 is unusable, and any MAF analytics must gate
on the engine running.** Those rows are 9.9% of the channel, so ignoring
the gate is not a rounding error.

*Confidence: the artifact is observed fact; its cause is unknown.*

### 4. `ambient.pressure` — two units under one name

Not a sentinel problem; a normalization one, recorded here because it
corrupts the same analyses.

| `channel_raw` | unit | range | samples |
|---|---|---|---|
| `baro` | kPa | 99–100 | 6,730 |
| `n47d_ambient_press` | hPa | 991–1007 | 1,807 |

Both normalize to `ambient.pressure`, so any query grouping by `channel`
averages kPa with hPa — a 10× scale collision inside one channel.
Ambient pressure is a *conditioning* variable for Stage 3 baselines, so
this would quietly poison the comparisons the analytics layer exists to
make.

Fixed by unmerging the names in the ingest's channel map, not by
converting values: the ingest normalizes names, and giving it value
arithmetic would be new semantics with float-exactness consequences.
`channel_raw` is the source of truth and `channel` re-derives from it.
Tracked separately from Stage 1 — different defect, different rollback.

*Confidence: observed fact.*

### Not a defect: MAP above 255 in the oldest data

`engine.manifold_pressure.absolute` holds non-integer values up to 259 —
impossible for a `uint8`. They occur only under `mapping_ver`
`d72n47a0-2026-08` (2026-08-24 to 08-27), a decode generation that no
longer runs. Recorded so it is not rediscovered as a live bug. Not
back-filled: unknown stays unknown.

## The change

Decoding returns a value **and** a label, instead of collapsing the
untrustworthy cases to nothing:

```python
Reading(value=2.0, quality='sentinel')     # lambda's 0xFFFF
Reading(value=255.0, quality='saturated')  # MAP on its rail
Reading(value=42.0, quality='ok')          # a measurement
```

Four rules hold this together:

1. **The decoded number is always kept**, including for a flagged
   reading. A sentinel row whose value is the real 2.0 is honest; a
   placeholder would not be. `samples.value` is `Float64 NOT NULL` in the
   lake, so there is no "value withheld" representation to use anyway.
2. **Display keeps suppressing.** The dashboard must not start showing a
   255 kPa MAP just because storage now keeps it. Suppression moves from
   "the decoder returned None" to "presentation respects quality"; the
   user-visible behaviour does not change.
3. **`invalid:` and `saturated:` are raw-domain** — they list bit
   patterns, not decoded values. A sentinel is `0xFFFF`, not `2.0`, so
   the test survives a scale correction.
4. **Quality describes a value that arrived.** An exchange that produced
   no response is `telemetry.channel_errors`, not a quality row. The two
   layers do not overlap, and `decode_fail` is consequently unused: a
   decode that raises has no value to attach a label to.

`sentinel` takes precedence over `clipped`, because a sentinel usually
violates the declared range too and "the ECU declared it unavailable" is
the more informative of the two facts.

### The labels are a schema contract

`QUALITIES` in `bmwdiag/mapping/decoder.py` must equal the lake's
`Enum8` exactly. ClickHouse drops an unknown *column* silently but
**fails an entire insert batch on an unknown enum value** — so inventing
a label without an `ALTER MODIFY COLUMN` migration breaks sync for every
sample in the batch. A test asserts the tuple against the enum, with that
failure mode written into it.

### Order of work, and why

1. **Decoder** — `Reading`, the labels, the `saturated:` field. No
   behaviour change: the old `decode_value` / `decode_response` remain
   byte-identical wrappers that map any non-ok reading to `None`.
2. **Storage** — carry quality through the executor, the recorder's
   SQLite schema, the sync wire and the ingest; teach the display to
   suppress on quality.
3. **Mapping declarations** — `invalid: [0xFFFF]` on lambda,
   `saturated: [255]` on MAP, with the `mapping.version` bump,
   `VERSIONS.lock` and the re-pinned production hash.

The declarations come **last on purpose.** The wrapper maps a non-ok
reading to `None`, so the moment a mapping declares `saturated: [255]`
while the executor still calls the wrapper, MAP = 255 stops being stored
at all — the fix would introduce a regression, and take boost
(`map - baro`) down with it. Nothing declares the new fields until
storage can carry the label.

## Who acts on it

A label nothing consumes is decoration. Two consumers act on it, and they
answer different questions.

**The diagnostics view** (`/api/diagnostics`, Car link tab) reports quality
per channel, separately from the per-request counters. Those counters
answer "did the exchange work"; they cannot answer "did anything usable
come back", because a positive response still decodes to a sentinel. A
channel at 100% request success and 100% sentinel is broken in a way no
request counter can show, and it now reads as `sentinel 76` in the
channel table. `flagged_pct` is `None` — not `0.0` — until a channel has
decoded at least once, because "0% flagged" on a channel that never
answered would read as a clean bill of health.

**The analytics exclude flagged samples by default.**
`analysis/session_report.py` drops them from every series and statistic,
counts them per channel, and says in the report how many it left out.
`--include-flagged` puts them back for anyone deliberately studying them.
A channel flagged into total silence still appears in the quality table
with `samples: 0`, because omitting it would put "answered, but nothing
usable" back in the same bucket as "never polled".

The lake battery (`analysis/clickhouse/insights.sql`) reads the recorded
label rather than re-deriving it. It used to hard-code `value >= 255` and
`value >= 2.0` to rediscover MAP saturation and the lambda sentinel by
hand, in every report; a newly declared sentinel now appears there
without anyone editing the file. One caveat for longitudinal work: rows
recorded before this layer are all `ok` because nothing was labelling
them, not because they were clean, so any comparison of flagged rates
across that boundary is comparing two different questions.

`pinned_at_max` survives in the session report, doing a different job.
It runs over what is left *after* the declared flags are removed, so it
now surfaces saturation nobody has declared **yet** — which is how the
MAF 222.22 g/s artifact was found. It is a lead to investigate, never a
finding.

### The derived-channel corner

`boost = map - baro` is computed from MAP. When MAP is saturated, boost
is currently derived from a wrong 255 and stored as clean. A derived
value computed from any non-`ok` input **must not** carry `quality='ok'`,
or Stage 1 recreates one layer up the exact defect it exists to remove.
