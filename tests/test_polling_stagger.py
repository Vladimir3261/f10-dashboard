"""
Staggered polling classes.

An expensive group (the multi-frame F303 dynamic reads) must not fire
all at once and stall the fast channels. A staggered class fires exactly
one member per due-cycle, round-robin, while non-staggered classes are
untouched (the byte-pinned OBD behaviour lives in test_polling_plan).
"""

import unittest

from tests import support  # noqa: F401

from bmwdiag.mapping import PollingPlan, load_text
from bmwdiag.mapping.model import PollingClassDef
from bmwdiag.mapping.polling import resolve_classes

STAGGERED = """
schema_version: 1
mapping: {id: stagger-fixture, production: false}
ecu: {target: 0x12}
polling_classes:
  fast: {every: 1, priority: 0}
  grp:  {every: 2, priority: 1, stagger: true}
defaults:
  request: {protocol: uds, service: 0x22, target: 0x12}
requests:
  fastone:
    did: 0x0001
    polling: {class: fast}
    response: {data_length: 2}
    signals:
      f: {label: F, unit: x, decode: {type: uint16_be}}
  a:
    did: 0x000A
    polling: {class: grp}
    response: {data_length: 2}
    signals:
      sa: {label: A, unit: x, decode: {type: uint16_be}}
  b:
    did: 0x000B
    polling: {class: grp}
    response: {data_length: 2}
    signals:
      sb: {label: B, unit: x, decode: {type: uint16_be}}
  c:
    did: 0x000C
    polling: {class: grp}
    response: {data_length: 2}
    signals:
      sc: {label: C, unit: x, decode: {type: uint16_be}}
"""


class Stagger(unittest.TestCase):
    def setUp(self):
        self.mapping = load_text(STAGGERED, source="<stagger>")
        classes = resolve_classes(self.mapping.polling_classes)
        self.plan = PollingPlan(self.mapping.requests, classes)

    def test_stagger_flag_loads(self):
        classes = {c.name: c for c in self.mapping.polling_classes}
        self.assertTrue(classes["grp"].stagger)
        self.assertFalse(classes["fast"].stagger)

    def test_at_most_one_group_member_per_cycle(self):
        for cycle in range(20):
            due = self.plan.due(cycle)
            grp = [r.id for r in due if r.id in ("a", "b", "c")]
            self.assertLessEqual(len(grp), 1, f"cycle {cycle}: {grp}")

    def test_fast_channel_is_never_starved(self):
        for cycle in range(20):
            due = {r.id for r in self.plan.due(cycle)}
            self.assertIn("fastone", due, f"cycle {cycle}")

    def test_members_round_robin_in_order(self):
        # grp is due on even cycles (every=2); members cycle a, b, c, a...
        fired = []

        for cycle in range(12):
            for r in self.plan.due(cycle):
                if r.id in ("a", "b", "c"):
                    fired.append((cycle, r.id))

        self.assertEqual(
            fired,
            [(0, "a"), (2, "b"), (4, "c"), (6, "a"), (8, "b"), (10, "c")],
        )

    def test_every_member_is_eventually_covered(self):
        covered = set()

        for cycle in range(12):
            covered.update(
                r.id for r in self.plan.due(cycle) if r.id in ("a", "b", "c")
            )

        self.assertEqual(covered, {"a", "b", "c"})

    def test_non_staggered_class_still_fires_all_members(self):
        """Sanity: flip stagger off and the group fires together again."""
        classes = resolve_classes(
            [PollingClassDef("fast", "cycles", 1.0, 0),
             PollingClassDef("grp", "cycles", 2.0, 1, stagger=False)]
        )
        plan = PollingPlan(self.mapping.requests, classes)
        due = {r.id for r in plan.due(0)}
        self.assertTrue({"a", "b", "c"} <= due)


class ProductionUnaffected(unittest.TestCase):
    def test_production_mapping_has_no_staggered_class(self):
        production = load_text(
            open(support.OBD_MAPPING, encoding="utf-8").read(),
            source=support.OBD_MAPPING,
        )

        for cls in production.polling_classes:
            self.assertFalse(cls.stagger, cls.name)


if __name__ == "__main__":
    unittest.main()
