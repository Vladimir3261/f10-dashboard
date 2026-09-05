"""
The WiCAN issue #752 fixture: a non-echoing DDE7 response, end to end.

The raw bytes come from the issue's CAN capture; the candidate mapping
must decode them to exactly 37.99 g through the SAME decoder the
runtime uses - and must do it without weakening echo-based matching
anywhere else (test_response_matchers covers that side).
"""

import os
import unittest

from tests import support  # noqa: F401

from bmwdiag.mapping import load_file, decode_signal
from research.gate import candidate_gate
from research.importers import wican_issue_fixture
from research.model import validate_record

CANDIDATE = os.path.join(
    support.ROOT, "mappings", "candidates", "bmw", "dde", "n47",
    "dde7_kwp_local_id.yaml",
)


class Records(unittest.TestCase):
    def setUp(self):
        self.records = wican_issue_fixture.import_fixture()
        self.by_id = {r.record_id: r for r in self.records}

    def test_records_validate(self):
        for record in self.records:
            self.assertEqual(validate_record(record), [], record.record_id)

    def test_metadata_is_the_source_vehicle_not_ours(self):
        signal = self.by_id["wican752.dde7.0x0406"]
        self.assertEqual(signal.verification, "externally_verified")
        self.assertEqual(signal.applicability["source_engine"], "N47D20C")
        self.assertEqual(signal.applicability["source_ecu"], "DDE7N47")
        self.assertEqual(
            signal.applicability["target_chassis_status"], "unverified"
        )

    def test_captured_signal_passes_the_gate(self):
        self.assertEqual(candidate_gate(self.by_id["wican752.dde7.0x0406"]), [])

    def test_claimed_identifiers_stay_incomplete(self):
        """0x03EB / 0x0AF1 have no raw frames - recorded, not promoted."""
        for ident in ("0x03EB", "0x0AF1"):
            record = self.by_id[f"wican752.dde7.claim.{ident}"]
            self.assertEqual(record.evidence_tier, "C")
            self.assertEqual(record.verification, "discovered")
            self.assertEqual(record.request["completeness"], "incomplete")
            self.assertNotEqual(candidate_gate(record), [])

    def test_matcher_is_service_sub_only(self):
        signal = self.by_id["wican752.dde7.0x0406"]
        self.assertEqual(signal.request["matcher"], "service_sub_only")
        self.assertIs(False, self.by_id["wican752.dde7.0x0406.exchange"]
                      .request["identifier_echo"])


class CandidateMapping(unittest.TestCase):
    def setUp(self):
        self.mapping = load_file(CANDIDATE)
        self.request = self.mapping.requests[0]

    def test_decodes_the_captured_response_to_37_99_g(self):
        request_bytes, response, expected = wican_issue_fixture.fixture_bytes()
        self.assertEqual(bytes(self.request.payload), request_bytes)

        signal = self.request.signals[0]
        self.assertEqual(decode_signal(signal, self.request, response), expected)

    def test_response_prefix_is_service_sub_only(self):
        """`6C 10` and nothing more - the identifier is deliberately absent."""
        self.assertEqual(self.request.response.prefix, (0x6C, 0x10))
        self.assertEqual(self.request.response.payload_offset, 2)

    def test_never_production(self):
        self.assertFalse(self.mapping.production)
        self.assertEqual(self.mapping.verification.status, "candidate")

    def test_capability_gate_disables_it_against_an_obd_ecu(self):
        """
        Even force-loaded, the file requires a `diagnostic_profile` the
        OBD provider cannot answer - so resolution against a real
        (OBD-discovered) ECU yields zero requests until a probe of its
        nominated read has actually answered.
        """
        from bmwdiag.mapping import MappingRegistry
        from bmwdiag.obd import ObdCapabilitySet

        registry = MappingRegistry([self.mapping])
        profile = registry.resolve(ObdCapabilitySet({0x0C, 0x05}))
        self.assertEqual(profile.requests, [])


if __name__ == "__main__":
    unittest.main()
