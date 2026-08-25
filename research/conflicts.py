"""
Cross-source conflict detection.

Two sources rarely disagree politely: the same normalized signal turns
up under different identifiers, the same numeric identifier means
different things on different variants, and the same result name comes
with different scaling. None of that is resolved here - a conflict is a
FINDING, written to the conflict report with both sides cited, never a
decision made silently.

Cross-source confirmation is the mirror image: agreement counts only
when the sources' ancestry actually differs. Two exports of the same
BMW table agreeing is parser validation, not independent evidence.
"""

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .model import EVIDENCE_RELATIONSHIPS, ResearchRecord

__all__ = ["Conflict", "detect_conflicts", "independent_sources", "confirmations"]


class Conflict:
    """One detected disagreement, with both sides attached."""

    def __init__(
        self,
        kind: str,
        key: str,
        a: ResearchRecord,
        b: ResearchRecord,
        detail: str,
    ):
        self.kind = kind
        self.key = key
        self.a = a
        self.b = b
        self.detail = detail

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "a": self.a.record_id,
            "b": self.b.record_id,
            "a_variant": self.a.applicability.get("sgbd", "unknown"),
            "b_variant": self.b.applicability.get("sgbd", "unknown"),
            "detail": self.detail,
        }

    def __repr__(self) -> str:
        return f"Conflict({self.kind}, {self.a.record_id} vs {self.b.record_id})"


def _same_variant(a: ResearchRecord, b: ResearchRecord) -> bool:
    va = a.applicability.get("sgbd")
    vb = b.applicability.get("sgbd")
    return va is not None and va == vb


def _pairs(records: Sequence[ResearchRecord]):
    ordered = sorted(records, key=lambda r: r.record_id)

    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            yield a, b


def detect_conflicts(records: Sequence[ResearchRecord]) -> List[Conflict]:
    """
    Detect disagreements across signal-definition records.

    The same numeric identifier on DIFFERENT variants is deliberately
    NOT a conflict - `d71`, `d72` and `d73` records are expected to
    coexist, and treating cross-variant reuse of an id as disagreement
    would manufacture noise. A conflict needs a shared claim: the same
    variant, or the same normalized signal.
    """
    out: List[Conflict] = []
    signals = [r for r in records if r.record_type == "signal_definition"]

    for a, b in _pairs(signals):
        ident_a = a.source.get("source_identifier")
        ident_b = b.source.get("source_identifier")

        # -- same variant, same identifier, different meaning ---------
        if _same_variant(a, b) and ident_a and ident_a == ident_b:
            name_a = a.source.get("source_result_name")
            name_b = b.source.get("source_result_name")

            if name_a and name_b and name_a != name_b:
                out.append(Conflict(
                    "same_id_different_meaning", str(ident_a), a, b,
                    f"{name_a!r} vs {name_b!r} for identifier {ident_a} "
                    f"on {a.applicability.get('sgbd')}",
                ))

            for fld in ("mul", "add", "div", "raw_type", "unit", "byte_order"):
                va, vb = a.data.get(fld), b.data.get(fld)

                if va not in (None, "unknown") and vb not in (None, "unknown") \
                        and va != vb:
                    out.append(Conflict(
                        f"same_id_different_{fld}", str(ident_a), a, b,
                        f"{fld}: {va!r} vs {vb!r}",
                    ))

        # -- same normalized signal, different wire facts -------------
        if (
            a.normalized_signal
            and a.normalized_signal == b.normalized_signal
        ):
            if ident_a and ident_b and ident_a != ident_b:
                out.append(Conflict(
                    "same_signal_different_id", a.normalized_signal, a, b,
                    f"{ident_a} ({a.applicability.get('sgbd', '?')}) vs "
                    f"{ident_b} ({b.applicability.get('sgbd', '?')})",
                ))

            seq_a = a.request.get("pattern")
            seq_b = b.request.get("pattern")

            if seq_a and seq_b and seq_a != seq_b:
                out.append(Conflict(
                    "same_signal_different_request_pattern",
                    a.normalized_signal, a, b,
                    f"{seq_a!r} vs {seq_b!r}",
                ))

        # -- same result name, different raw type (any variant) -------
        name_a = a.source.get("source_result_name")
        name_b = b.source.get("source_result_name")

        if name_a and name_a == name_b:
            ta, tb = a.data.get("raw_type"), b.data.get("raw_type")

            if ta not in (None, "unknown") and tb not in (None, "unknown") \
                    and ta != tb:
                out.append(Conflict(
                    "same_result_name_different_raw_type", name_a, a, b,
                    f"{ta!r} vs {tb!r}",
                ))

    return out


def independent_sources(
    source_a: str,
    source_b: str,
    relationships: Sequence[Dict[str, str]],
) -> bool:
    """
    True when two sources have genuinely different ancestry.

    `relationships` rows: {"a": id, "b": id, "type": <relationship>}.
    Any recorded non-independent link, in either direction, breaks
    independence. Unrelated sources default to independent.
    """
    if source_a == source_b:
        return False

    for rel in relationships:
        kind = rel.get("type")

        if kind not in EVIDENCE_RELATIONSHIPS:
            continue

        if kind == "independent":
            continue

        pair = {rel.get("a"), rel.get("b")}

        if pair == {source_a, source_b}:
            return False

    return True


def confirmations(
    records: Sequence[ResearchRecord],
    relationships: Sequence[Dict[str, str]],
) -> List[Tuple[str, List[ResearchRecord]]]:
    """
    Normalized signals that at least two INDEPENDENT sources agree on.

    Agreement means: same normalized signal, same identifier, and no
    contradictory scaling. This is what may justify upgrading a record
    to `cross_source_confirmed` - by a human, in the report; never
    automatically here.
    """
    by_signal: Dict[str, List[ResearchRecord]] = {}

    for record in records:
        if record.record_type != "signal_definition":
            continue

        if record.normalized_signal:
            by_signal.setdefault(record.normalized_signal, []).append(record)

    out: List[Tuple[str, List[ResearchRecord]]] = []

    for signal, group in sorted(by_signal.items()):
        if len(group) < 2:
            continue

        idents = {r.source.get("source_identifier") for r in group}
        muls = {r.data.get("mul") for r in group if r.data.get("mul")}

        if len(idents) != 1 or len(muls) > 1:
            continue

        for a, b in _pairs(group):
            if independent_sources(a.source_id, b.source_id, relationships):
                out.append((signal, sorted(group, key=lambda r: r.record_id)))
                break

    return out
