"""
Offline, read-only analysis of recorded telemetry.

Reads a run out of a telemetry.db and produces human-readable reports.
It opens the database read-only, never writes to it, and never emits the
VIN. This is the first analytics layer (roadmap Stage 3): descriptive
per-session summaries and the cold-start + drive report.
"""
