# Trips, identity, and vehicle events

Three problems with one root: the storage model described how data was
**acquired**, not what the car **did**.

## 1. Identity was a function of the filename

The lake's `session_id` was `CRC32(basename) << 20 | run_id`. Deterministic
— which is what made it look sufficient — but it identified a *storage
location*, not a drive:

- renaming `telemetry.db` changed the identity of every run inside it, so
  a re-sync duplicated the whole history rather than de-duplicating;
- two drive files sharing a basename collided outright, and the lake
  silently merged two different drives into one session;
- CRC32 is 32 bits, so collisions were possible anyway;
- copying a file to a new name minted new identities for old data.

**Identity is now minted once, when the run is created, and travels with
it.** `runs.session_uid` holds a ULID — sortable by creation time, 80 bits
of randomness, no dependencies (see `bmwdiag/identity.py`).

The lake keeps a numeric `session_id` as its join key, because rekeying
`samples` to a string would rewrite every row and every query for no
analytical gain — but it is now derived from the **ULID**, not the
filename, via `blake2b(..., digest_size=8)`.

The hash choice matters: an earlier draft composed CRC32 and Adler32 into
64 bits, which answered the filename-CRC32 problem with more CRC32. Both
are error-detecting codes with no uniformity guarantee, and `samples`
carry **only** the numeric id — so a collision there could not be
repaired afterwards from the uid on `sessions`. BLAKE2b is stdlib and
uniform; there was never a dependency trade-off to justify the cheaper
option.

`sessions.session_uid` carries the full identity for anything that needs
certainty.

**Runs recorded before this keep the filename derivation.** Their sessions
are already in the lake under the old ids; re-deriving would not correct
those rows, it would duplicate them.

## 2. A run is not a trip

A run is one HSFZ connection. It ends on a mode change (deliberately), on
a clock step (deliberately), and on a dropped link (accidentally). **Drive
11 recorded as four runs.** Every longitudinal question — "how did this
drive compare with that one" — was asking about the wrong unit.

`analysis/trips.py` groups runs into physical trips as a **pure function
over recorded evidence**, not something stamped at record time. Two
reasons:

- the evidence improves. Adding ignition state or odometer later should
  re-group the whole history, not just new drives;
- a boundary someone disagrees with must be arguable. Every trip carries
  the reason it started, so "why are these two separate drives?" is a
  field, not archaeology.

Boundaries, strongest first:

| reason | why it is decisive |
|---|---|
| different host boot | the recorder was power-cycled; stronger than any gap, and independent of the clock |
| vehicle configuration changed | whatever happened, it is not one drive |
| clock not disciplined | a gap is a timestamp difference; on a stepped clock it is not evidence — **split rather than guess** |
| gap > 300 s | a reconnect takes seconds; a real stop takes minutes |
| *(no boundary)* | a run whose process was **killed** has no `ended_at`; the last recorded sample supplies the effective end, so a crash-and-restart rejoins the drive instead of splitting it |
| overlapping runs | timestamps disagree with each other, so neither is trusted |

Trip identity is **derived from the first run**, never minted, so
re-grouping the same data yields the same trip ids rather than looking
like new trips on every analysis.

Inspect it:

```
python3 -m analysis.trips --db local/telemetry.db
```

## 3. Nothing recorded what was done to the car

A longitudinal baseline is a claim about **one configuration of one
vehicle**. An oil change, a replaced sensor or a remap does not make
earlier data wrong — it makes it a *different population*, and comparing
across the boundary silently pools two cars.

`local/vehicle-events.yaml` (gitignored, no VIN) records them; copy
`config/vehicle-events.example.yaml`. Analytics asks:

```python
baseline_is_valid_across(events, earlier, now)   # nothing happened between?
events_between(events, earlier, now)             # what happened?
```

The range is half-open, `(start, end]`: an event exactly at `start`
already applied to the baseline, one at `end` has not. Without a rule an
event on the boundary counts twice or not at all.

An event with no date is **skipped, not placed at zero** — at zero it
would sit before all history and silently invalidate every baseline.

An unfamiliar `kind` still segments. The safe reading of "something was
done to the car" is that it mattered.

## What this does not do

It does not build the baselines themselves — that is #14. This is the
identity, grouping and event context those baselines will need in order
to be trustworthy, and none of it could be retrofitted afterwards:
identity has to be minted at record time, and the event history has to be
written down while someone still remembers.
