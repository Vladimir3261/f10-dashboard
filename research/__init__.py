"""
research - the N47 mapping research and import pipeline.

Everything in this package is OFFLINE tooling: it reads pinned public
sources (cached under the gitignored local/research-cache/), normalizes
what they claim into research records, detects conflicts between them,
gates what may become an executable candidate mapping, and writes the
reports under research/reports/.

Nothing here runs at vehicle time, nothing here opens a socket, and
nothing here is imported by live.py. The runtime consumes only validated
mapping files under mappings/; this package is how candidate mappings
earn their way there.

The one rule that overrides convenience: no invented BMW data. A fact
either carries a source citation or it is stored as `unknown`. See
docs/MAPPING_RESEARCH.md and research/README.md.
"""

__all__ = ["model", "manifest", "gate", "conflicts", "importers"]
