"""
Conflict detection and cross-source independence.

Variants must coexist: the same numeric identifier on two different
SGBDs is NOT a conflict, while a real disagreement - same signal,
different wire facts - must surface and never be silently resolved.
"""

import unittest

from tests import support  # noqa: F401

from research.conflicts import (
    confirmations,
    detect_conflicts,
    independent_sources,
)
from research.model import ResearchRecord


def signal(record_id, source_id, sgbd, ident, *, name=None, result=None,
           mul=None, raw_type=None, pattern=None):
    return ResearchRecord(
        record_id=record_id,
        record_type="signal_definition",
        source_id=source_id,
        evidence_tier="B",
        verification="discovered",
        safety="read_only_telemetry_candidate",
        normalized_signal=name,
        source={
            "source_identifier": ident,
            "source_result_name": result,
        },
        applicability={"sgbd": sgbd},
        data={"mul": mul, "raw_type": raw_type},
        request={"completeness": "unknown", "pattern": pattern},
        license={"source_license": "unknown"},
    )


class Detection(unittest.TestCase):
    def test_same_id_on_different_variants_is_coexistence_not_conflict(self):
        """d71/d72/d73 records sharing 0x0406 must NOT collide."""
        a = signal("d73.x", "s1", "D73N47A0", "0x0406", result="STAT_A")
        b = signal("d71.x", "s2", "D71N47A0", "0x0406", result="STAT_B")
        self.assertEqual(detect_conflicts([a, b]), [])

    def test_same_variant_same_id_different_meaning(self):
        a = signal("v.a", "s1", "D73N47A0", "0x0406", result="STAT_SOOT")
        b = signal("v.b", "s2", "D73N47A0", "0x0406", result="STAT_TEMP")
        kinds = [c.kind for c in detect_conflicts([a, b])]
        self.assertIn("same_id_different_meaning", kinds)

    def test_same_variant_same_id_different_scaling(self):
        a = signal("v.a", "s1", "D73N47A0", "0x0406", result="STAT_X", mul="0.01")
        b = signal("v.b", "s2", "D73N47A0", "0x0406", result="STAT_X", mul="0.015259")
        kinds = [c.kind for c in detect_conflicts([a, b])]
        self.assertIn("same_id_different_mul", kinds)

    def test_same_signal_different_id_across_variants(self):
        a = signal("d73.oil", "s1", "D73N47A0", "0x0458",
                   name="engine.oil_temperature")
        b = signal("d72.oil", "s2", "d72n47a0", "0x4517",
                   name="engine.oil_temperature")
        kinds = [c.kind for c in detect_conflicts([a, b])]
        self.assertIn("same_signal_different_id", kinds)

    def test_same_result_name_different_raw_type(self):
        a = signal("x.a", "s1", "D73N47A0", "0x0001",
                   result="STAT_SAME", raw_type="uint16")
        b = signal("x.b", "s2", "D71N47A0", "0x0002",
                   result="STAT_SAME", raw_type="uint8")
        kinds = [c.kind for c in detect_conflicts([a, b])]
        self.assertIn("same_result_name_different_raw_type", kinds)

    def test_same_signal_different_request_pattern(self):
        a = signal("p.a", "s1", "d72n47a0", "0x44C1",
                   name="dpf.soot_mass.modelled", pattern="uds_dynamic_f303")
        b = signal("p.b", "s2", None, "0x0406",
                   name="dpf.soot_mass.modelled", pattern="kwp_local_id")
        kinds = [c.kind for c in detect_conflicts([a, b])]
        self.assertIn("same_signal_different_request_pattern", kinds)

    def test_conflicts_carry_both_sides(self):
        a = signal("c.a", "s1", "D73N47A0", "0x1", name="x", mul="1")
        b = signal("c.b", "s2", "d72n47a0", "0x2", name="x", mul="1")
        conflict = detect_conflicts([a, b])[0]
        d = conflict.as_dict()
        self.assertEqual({d["a"], d["b"]}, {"c.a", "c.b"})
        self.assertIn("0x1", d["detail"])
        self.assertIn("0x2", d["detail"])


RELATIONSHIPS = [
    {"a": "klartext", "b": "ediabasx-docs-sgbd", "type": "same_primary_source"},
    {"a": "morguux-d73n47a0", "b": "klartext", "type": "same_primary_source"},
    {"a": "deepobd-configs", "b": "ediabaslib", "type": "derived_from"},
    {"a": "wican-issue-752", "b": "klartext", "type": "independent"},
]


class Ancestry(unittest.TestCase):
    def test_same_primary_source_is_not_independent(self):
        self.assertFalse(
            independent_sources("klartext", "ediabasx-docs-sgbd", RELATIONSHIPS)
        )

    def test_copied_or_derived_is_not_independent(self):
        self.assertFalse(
            independent_sources("deepobd-configs", "ediabaslib", RELATIONSHIPS)
        )

    def test_unrelated_sources_default_to_independent(self):
        self.assertTrue(
            independent_sources("wican-issue-752", "morguux-d73n47a0",
                                RELATIONSHIPS)
        )

    def test_a_source_is_never_independent_of_itself(self):
        self.assertFalse(independent_sources("klartext", "klartext", []))

    def test_confirmation_requires_independent_agreement(self):
        agree_a = signal("cf.a", "klartext", "d72n47a0", "0x4517",
                         name="engine.oil_temperature", mul="0.01")
        agree_b = signal("cf.b", "ediabasx-docs-sgbd", "d72n47a0", "0x4517",
                         name="engine.oil_temperature", mul="0.01")
        # same primary source: agreement is parser validation, not
        # cross-source confirmation
        self.assertEqual(confirmations([agree_a, agree_b], RELATIONSHIPS), [])

        agree_c = signal("cf.c", "wican-issue-752", "d72n47a0", "0x4517",
                         name="engine.oil_temperature", mul="0.01")
        confirmed = confirmations([agree_a, agree_c], RELATIONSHIPS)
        self.assertEqual([s for s, _ in confirmed], ["engine.oil_temperature"])


if __name__ == "__main__":
    unittest.main()
