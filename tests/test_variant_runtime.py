"""
Runtime integration of variant-gated proprietary channels.

The verified F-series dynamic channels reach the poll loop only when the
ECU is confirmed to be their SGBD variant by PROBE, and several channels
that share one dynamic DID must multiplex without their bytes bleeding
into each other. Both are tested here against a fake transport - no car.
"""

import unittest

from tests import support  # noqa: F401
from tests.support import hexb

from bmwdiag.mapping import MappingExecutor, MappingRegistry, load_file
from bmwdiag.mapping.model import Capability
from bmwdiag.obd import ObdCapabilitySet
from bmwdiag.variant import (
    CombinedCapabilitySet,
    VariantCapabilitySet,
    VariantProbe,
    variant_probes,
)

DYNAMIC = support.os.path.join(
    support.ROOT, "mappings", "candidates", "bmw", "dde", "n47",
    "d72n47a0_dynamic.yaml",
)
FLOW = support.os.path.join(
    support.ROOT, "mappings", "candidates", "bmw", "dde", "n47",
    "d72n47a0_flow.yaml",
)


class FakeDde:
    """Answers the F303 define/clear and returns per-source raw words."""

    RAW = {
        "4517": "39 08", "44be": "03 f7", "44c1": "03 f6", "4bc3": "0e 2f",
        "461b": "46 a6", "4841": "2c 33", "42c8": "2d 7b",
    }

    def __init__(self):
        self.sent = []
        self.last = None

    def request(self, payload, *, dst, timeout=None):
        h = bytes(payload).hex()
        self.sent.append(h)

        if h.startswith("2c01f303"):
            self.last = h[8:12]

        if h == "22f303":
            return hexb("62 f3 03 " + self.RAW.get(self.last, "00 00"))

        return hexb("6c 03 f3 03")


class DeadEcu:
    """Answers nothing usefully - an ECU that is not this variant."""

    def request(self, payload, *, dst, timeout=None):
        raise support_hsfz_error()


def support_hsfz_error():
    import live
    return live.HsfzError("negative response to 0x22: NRC 0x31")


class VariantProbing(unittest.TestCase):
    def test_probe_confirms_variant_by_reading_it(self):
        registry = MappingRegistry([load_file(DYNAMIC)])
        probes = variant_probes(registry.mappings)
        self.assertEqual([(v, r.id) for v, r in probes],
                         [("d72n47a0", "n47.d72.dyn.4517")])

        fake = FakeDde()
        confirmed = VariantProbe(
            lambda p, dst, timeout=None: fake.request(p, dst=dst)
        ).confirm(probes, 0x12)
        self.assertEqual(confirmed, {"d72n47a0"})

    def test_a_non_variant_ecu_confirms_nothing(self):
        registry = MappingRegistry([load_file(DYNAMIC)])
        probes = variant_probes(registry.mappings)

        confirmed = VariantProbe(
            lambda p, dst, timeout=None: DeadEcu().request(p, dst=dst)
        ).confirm(probes, 0x12)
        self.assertEqual(confirmed, set())

    def test_combined_caps_answer_both_kinds(self):
        caps = CombinedCapabilitySet(
            ObdCapabilitySet({0x0C, 0x05}),
            VariantCapabilitySet({"d72n47a0"}),
        )
        self.assertTrue(caps.satisfies(Capability("obd_mode01_pid", 0x0C)))
        self.assertTrue(caps.satisfies(Capability("sgbd_variant", "d72n47a0")))
        self.assertFalse(caps.satisfies(Capability("sgbd_variant", "d73n47a0")))

    def test_resolution_gates_the_channels_on_the_variant(self):
        registry = MappingRegistry([load_file(DYNAMIC)])

        # unknown variant -> the mapping's ecu.match fails -> no requests
        without = registry.resolve(
            ObdCapabilitySet({0x0C}), targets={"discovered_engine": 0x12}
        )
        self.assertEqual(without.requests, [])

        # variant confirmed -> the channels resolve
        with_variant = registry.resolve(
            CombinedCapabilitySet(
                ObdCapabilitySet({0x0C}), VariantCapabilitySet({"d72n47a0"})
            ),
            targets={"discovered_engine": 0x12},
        )
        self.assertEqual(len(with_variant.requests), 4)


class F303Multiplexing(unittest.TestCase):
    def test_shared_dynamic_did_channels_decode_independently(self):
        registry = MappingRegistry([load_file(FLOW)])
        profile = registry.resolve(
            VariantCapabilitySet({"d72n47a0"}),
            targets={"discovered_engine": 0x12},
        )
        fake = FakeDde()
        executor = MappingExecutor(profile, transport=fake)

        requests = [profile.request(i) for i in (
            "n47.d72.dyn.461B", "n47.d72.dyn.4841", "n47.d72.dyn.42C8"
        )]
        values = executor.execute(requests)

        # each channel decoded ITS OWN source, no bleed across the shared DID
        self.assertEqual(values["n47d_coolant"], 80.86)
        self.assertEqual(round(values["n47d_boost_act"], 1), 1035.9)
        self.assertEqual(round(values["n47d_boost_set"], 1), 1066.0)

        # a define was re-armed before each different poll, in order
        defines = [f[8:12] for f in fake.sent if f.startswith("2c01")]
        self.assertEqual(defines, ["461b", "4841", "42c8"])

    def test_a_single_channel_arms_its_define_once(self):
        registry = MappingRegistry([load_file(FLOW)])
        profile = registry.resolve(
            VariantCapabilitySet({"d72n47a0"}),
            targets={"discovered_engine": 0x12},
        )
        fake = FakeDde()
        executor = MappingExecutor(profile, transport=fake)
        req = profile.request("n47.d72.dyn.461B")

        executor.execute([req])
        executor.execute([req])          # armed already -> reuse

        defines = [f for f in fake.sent if f.startswith("2c01")]
        self.assertEqual(len(defines), 1)


class RuntimeLoad(unittest.TestCase):
    def test_extra_mappings_load_but_base_stays_obd_only(self):
        import live

        base = live.load_registry(support.MAPPINGS)
        self.assertEqual({m.id for m in base.mappings}, {"sae-obd-engine"})

        extra_dir = support.os.path.join(
            support.ROOT, "mappings", "candidates", "bmw", "dde", "n47"
        )
        live.load_extra(base, [extra_dir])
        ids = {m.id for m in base.mappings}
        self.assertIn("sae-obd-engine", ids)
        self.assertIn("candidate-n47-d72-dynamic", ids)
        self.assertIn("candidate-n47-d72-flow", ids)


if __name__ == "__main__":
    unittest.main()
