"""
Source manifest loading and validation.

`research/manifests/sources.yaml` is the single registry of every
external source the pipeline is allowed to reference. A research record
naming a source absent from the manifest is a validation failure - that
is how "where did this byte come from" stays answerable forever.

The manifest is parsed with the same dependency-free YAML subset the
mapping runtime uses, so the research tooling adds no third-party
dependency either.
"""

import os
from typing import Any, Dict, List

from bmwdiag.mapping import yamlsubset

from .model import EVIDENCE_TIERS, ResearchError

__all__ = [
    "MANIFEST_PATH",
    "load_manifest",
    "load_relationships",
    "validate_manifest",
    "check_source_ids",
]

MANIFEST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "manifests", "sources.yaml"
)

#: Fields every source entry must carry. `license.id` may be "unknown",
#: but it may not be absent - not knowing is data, not an omission.
REQUIRED_FIELDS = ("name", "type", "url", "retrieved_at")


def load_manifest(path: str = MANIFEST_PATH) -> Dict[str, Dict[str, Any]]:
    """Load and validate the manifest; returns {source_id: entry}."""
    with open(path, "r", encoding="utf-8") as handle:
        document = yamlsubset.loads(handle.read(), source=path)

    if not isinstance(document, dict) or "sources" not in document:
        raise ResearchError(f"{path}: manifest must have a top-level 'sources' map")

    sources = document["sources"]

    if not isinstance(sources, dict):
        raise ResearchError(f"{path}: 'sources' must be a mapping of id -> entry")

    problems = validate_manifest(sources)

    if problems:
        raise ResearchError(f"{path}: " + "; ".join(problems))

    return sources


def load_relationships(path: str = MANIFEST_PATH) -> List[Dict[str, Any]]:
    """The evidence-ancestry rows, for independence analysis."""
    with open(path, "r", encoding="utf-8") as handle:
        document = yamlsubset.loads(handle.read(), source=path)

    rows = document.get("relationships", []) if isinstance(document, dict) else []

    if not isinstance(rows, list):
        raise ResearchError(f"{path}: 'relationships' must be a list")

    return rows


def validate_manifest(sources: Dict[str, Any]) -> List[str]:
    problems: List[str] = []

    for source_id, entry in sources.items():
        where = f"sources.{source_id}"

        if not isinstance(entry, dict):
            problems.append(f"{where} is not a mapping")
            continue

        for fld in REQUIRED_FIELDS:
            if fld not in entry:
                problems.append(f"{where}.{fld} is missing")

        license_block = entry.get("license")

        if not isinstance(license_block, dict) or "id" not in license_block:
            problems.append(
                f"{where}.license.id is missing (use 'unknown' when unverified)"
            )

        trust = entry.get("trust")

        if isinstance(trust, dict):
            tier = trust.get("tier")

            if tier is not None and tier not in EVIDENCE_TIERS:
                problems.append(f"{where}.trust.tier {tier!r} is not a valid tier")

        pin = entry.get("commit") or entry.get("revision")

        if entry.get("type") in ("git_repository", "gist") and not pin:
            problems.append(f"{where} has no pinned commit/revision")

    return problems


def check_source_ids(records, sources: Dict[str, Any]) -> List[str]:
    """Every record must reference a source the manifest knows."""
    problems: List[str] = []

    for record in records:
        if record.source_id not in sources:
            problems.append(
                f"record {record.record_id!r} references source "
                f"{record.source_id!r}, which is not in the manifest"
            )

    return problems
