# power/ — reserved (not implemented)

Placeholder for future automotive **power electronics** — cleanly and
safely powering the in-car host from the vehicle. **Nothing here yet, and
nothing to be built yet** (explicitly out of scope for now).

## The problem this will eventually solve

Running a Pi off the car needs more than a USB cable:

- **Ignition-aware power** — power up with the car, and shut the Pi down
  *gracefully* after the ignition goes off (not a hard cut mid-write).
- **Clean 12 V → 5 V** conversion rated for automotive transients
  (load-dump, cranking dips, spikes).
- **Brown-out / deep-discharge protection** so the host never flattens the
  car battery when parked.
- Safe, fused, correctly-gauged wiring.

## Ground rules when this starts

- **Safety first** — fusing, transient protection, and a graceful
  shutdown path are requirements, not extras. Get the electrical design
  reviewed.
- Public-repo hygiene as everywhere in `hardware/`: no personal/vehicle-
  identifying data in git.
- Until then, the host is bench/USB-powered.

See [`../README.md`](../README.md).
