# esp32/ — reserved (not implemented)

Placeholder for future ESP32-based microcontroller work, kept **separate**
from the Raspberry Pi runtime host on purpose.

Nothing here yet. This directory exists so future ESP32 work has an
obvious, isolated home and does not get entangled with the Pi
provisioning.

## Possible future scope (none committed to)

- A low-power always-on sensor/wake node.
- A CAN or auxiliary-sensor bridge feeding the Pi.
- Standalone logging when the Pi is not present.

## Ground rules when this starts

- **Keep it separate** from `raspberry-pi/` — its own toolchain, its own
  build, its own docs.
- Same public-repo hygiene as the rest of `hardware/`: no secrets, no
  Wi-Fi creds, no keys, no VIN in git. Real config injected outside git.
- Read-only on the car, consistent with the project's core principle.

See [`../README.md`](../README.md) for the hygiene rules that apply to
everything under `hardware/`.
