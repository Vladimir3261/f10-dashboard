"""
Regression coverage for issue #1's refreshed F10/N55 evidence.

The source car is not the target N47, so this test does not promote any
claim to local verification. It pins the narrower contract the report
actually supports: source provenance is current, 0x586F is represented as
absolute pressure, and the newly reported namespace/oil-temperature facts
stay available as explicit N55 leads rather than folklore.
"""

import os
import unittest

from tests import support  # noqa: F401
from tests.support import hexb

from bmwdiag.mapping import decode_signal, load_file, yamlsubset
from research.manifest import load_manifest

PIN = "742f8a44e6c9a78f4dd43ac1bb6a9c2376c74caa"
EVIDENCE = os.path.join(
    support.ROOT, "research", "evidence", "n47", "f10_field",
    "oil_pressure_586F.yaml",
)
CANDIDATE = os.path.join(
    support.ROOT, "mappings", "candidates", "bmw", "dde", "n47",
    "f10_static_58xx.yaml",
)


def load_evidence():
    with open(EVIDENCE, encoding="utf-8") as handle:
        return yamlsubset.loads(handle.read(), source=EVIDENCE)


class RefreshedSource(unittest.TestCase):
    def test_manifest_and_evidence_pin_the_report_that_contains_the_fix(self):
        source = load_manifest()["obd-gauge-cluster"]
        evidence = load_evidence()

        self.assertEqual(source["commit"], PIN)
        self.assertEqual(evidence["citation"]["commit"], PIN)
        self.assertIn(PIN, evidence["citation"]["url"])

    def test_the_wider_namespace_is_recorded_without_claiming_n47_support(self):
        evidence = load_evidence()

        self.assertEqual(
            evidence["namespace"]["answering_blocks"],
            ["42xx", "43xx", "44xx", "45xx", "4Axx", "58xx"],
        )
        self.assertEqual(evidence["namespace"]["no_answers"], ["DAxx"])
        self.assertEqual(evidence["n47_applicability"], "unverified")

    def test_the_confirmed_source_car_oil_temperature_lead_is_preserved(self):
        oil = load_evidence()["oil_temperature"]

        self.assertEqual(oil["request"], "22 44 02")
        self.assertEqual(oil["raw_type"], "uint16")
        self.assertEqual(oil["byte_order"], "big")
        self.assertEqual(oil["mul"], "0.75")
        self.assertEqual(oil["add"], "-48")
        self.assertEqual(oil["verification"], "externally_verified")
        self.assertEqual(oil["n47_applicability"], "unverified")

    def test_the_old_5817_and_58eb_interpretation_is_not_left_ambiguous(self):
        leads = {
            row["request"]: row["note"]
            for row in load_evidence()["unpinned_leads"]
        }

        self.assertIn("not oil temperature", leads["22 58 17"])
        self.assertIn("not oil temperature", leads["22 58 EB"])


class AbsolutePressureCandidate(unittest.TestCase):
    def setUp(self):
        self.mapping = load_file(CANDIDATE)
        self.request = next(
            r for r in self.mapping.requests if r.id == "f10.static.586F"
        )
        self.signal = self.request.signals[0]

    def test_the_mapping_names_the_datum_explicitly(self):
        self.assertEqual(self.mapping.version, 2)
        self.assertEqual(self.signal.key, "f10_oil_press_absolute")
        self.assertEqual(self.signal.label, "Oil pressure (absolute)")
        self.assertEqual(self.signal.unit, "mbar")

    def test_the_decoder_keeps_the_measured_absolute_value(self):
        # 0x03E8 is one atmosphere in millibar. The mapping must not hide
        # a fixed sea-level subtraction; gauge conversion needs the
        # contemporaneous barometric channel and belongs downstream.
        self.assertEqual(
            decode_signal(
                self.signal,
                self.request,
                hexb("62 58 6F 03 E8"),
            ),
            1000.0,
        )


if __name__ == "__main__":
    unittest.main()
