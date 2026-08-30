"""
Regression: the research pipeline changed NOTHING in production.

The production OBD mapping is byte-pinned, the vehicle runtime still
loads exactly the same requests, and the candidate files are excluded
from it wholesale.

The pin is a tripwire, not a freeze. It exists so that no research
importer, candidate promotion or refactor can edit the production
mapping as a side effect - reaching this file has to be a deliberate act.
Updating it is legitimate ONLY together with a `mapping.version` bump and
a note here saying what changed and why.
"""

import hashlib
import os
import unittest

from tests import support  # noqa: F401
from tests.support import hexb

from bmwdiag.mapping import MappingRegistry, decode_signal, load_file

#: sha256 of mappings/obd/engine.yaml.
#:
#: v1 -> v2 (2026-08-30): polling rates only. The twelve PIDs on the old
#: `fast` class were split into four wall-clock tiers (motion / context /
#: slow / rare) after the channel census showed the fast tier was 83% of
#: stored rows at 0.1-3.8% distinct values. No request, decode step,
#: signal or unit changed, which the decode tests below and the
#: exhaustive sweeps in tests/test_existing_obd_mappings.py still prove
#: byte for byte.
ENGINE_YAML_SHA256 = (
    "2c03669a8c9d32205f1c9fde67a36d65dfb9f9f1fb8cf8fb8af3d0e294aa5318"
)


class ProductionUnchanged(unittest.TestCase):
    def test_engine_yaml_is_byte_identical(self):
        with open(support.OBD_MAPPING, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()

        self.assertEqual(digest, ENGINE_YAML_SHA256)

    def test_runtime_registry_shape_is_unchanged(self):
        registry = MappingRegistry.from_tree(
            support.MAPPINGS, production_only=True
        )
        self.assertEqual(len(registry.mappings), 1)
        self.assertEqual(registry.mappings[0].id, "sae-obd-engine")
        self.assertEqual(len(registry.requests), 24)
        self.assertEqual(len(registry.signals), 24)
        self.assertEqual(len(registry.derived), 2)

    def test_candidates_never_reach_the_vehicle_runtime(self):
        registry = MappingRegistry.from_tree(
            support.MAPPINGS, production_only=True
        )
        ids = {m.id for m in registry.mappings}
        self.assertNotIn("candidate-n47-d72-dynamic", ids)
        self.assertNotIn("candidate-n47-dde7-kwp", ids)
        self.assertNotIn("candidate-f10-static-58xx", ids)

    def test_candidate_signal_keys_do_not_collide_with_production(self):
        """The full tree - production plus candidates - loads cleanly."""
        registry = MappingRegistry.from_tree(
            support.MAPPINGS, production_only=False
        )
        self.assertEqual(len(registry.mappings), 10)

    def test_a_production_decode_spot_check(self):
        mapping = load_file(support.OBD_MAPPING)
        request = next(r for r in mapping.requests if r.id == "obd.mode01.0C")
        value = decode_signal(
            request.signals[0], request, hexb("41 0C 0C 3C")
        )
        self.assertEqual(value, 783.0)


if __name__ == "__main__":
    unittest.main()
