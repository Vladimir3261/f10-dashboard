# vision/ — reserved (not implemented)

Placeholder for future **computer-vision** work (e.g. a camera on the Pi).
**Nothing here yet, and nothing to be built yet** — explicitly out of
scope for now.

## Possible future scope (none committed to)

- A camera feeding road/context data alongside the telemetry stream.
- On-device inference on the Pi 4 (or an accelerator).
- Correlating visual context with the diagnostic time-series.

## Ground rules when this starts

- **Privacy is the hard part.** Imagery can capture people, plates,
  locations, and routes. **No captured imagery, frames, or route/GPS data
  goes in git** — same rule that keeps the VIN out. Anything committed is
  code and synthetic/placeholder assets only.
- Keep it separate from the core BMW application; it augments, it does not
  redesign the runtime.
- Public-repo hygiene as everywhere in `hardware/`.

See [`../README.md`](../README.md).
