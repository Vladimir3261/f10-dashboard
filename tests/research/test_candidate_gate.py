"""
The executable-candidate gate: incomplete evidence stays research data.

Missing data must remain missing - the gate returns what is absent and
never fills a gap, infers a service from an identifier's shape, or lets
a Tier D claim or an unsafe operation through.
"""

import unittest

from tests import support  # noqa: F401

from research.gate import candidate_gate
from research.model import ResearchRecord, validate_record


def complete_record(**overrides) -> ResearchRecord:
    """A record with every gate requirement satisfied (the WiCAN shape)."""
    fields = dict(
        record_id="fixture.complete",
        record_type="signal_definition",
        source_id="wican-issue-752",
        evidence_tier="A",
        verification="externally_verified",
        safety="read_only_telemetry_candidate",
        normalized_signal="dpf.soot_mass.modelled",
        fact_labels=("wire_observation",),
        source={"source_identifier": "0x0406"},
        applicability={"sgbd": "unknown", "source_ecu": "DDE7N47"},
        data={
            "raw_type": "uint16", "width_bytes": 2, "byte_order": "big",
            "mul": "1", "div": "100", "add": "0", "unit": "g",
        },
        request={
            "completeness": "complete", "target": "engine",
            "sequence": ["2C 10 04 06"], "session": "none",
            "matcher": "service_sub_only", "prefix": "6C 10",
            "payload_location": "after prefix", "response_length": 2,
        },
        license={"source_license": "unknown"},
    )
    fields.update(overrides)
    return ResearchRecord(**fields)


class Gate(unittest.TestCase):
    def test_complete_record_is_eligible(self):
        record = complete_record()
        self.assertEqual(validate_record(record), [])
        self.assertEqual(candidate_gate(record), [])

    def test_incomplete_evidence_is_valid_research_but_not_executable(self):
        """A partial record is well-formed AND gated out - both must hold."""
        record = complete_record()
        record.request = {
            "completeness": "unknown", "target": "unknown",
            "sequence": "unknown", "session": "unknown", "matcher": None,
            "payload_location": "unknown", "response_length": "unknown",
        }
        self.assertEqual(validate_record(record), [])
        missing = candidate_gate(record)
        self.assertIn("request", missing)
        self.assertIn("target", missing)
        self.assertIn("matcher", missing)

    def test_each_missing_requirement_is_reported(self):
        cases = {
            "target": ({"request": dict(complete_record().request, target="unknown")}),
            "session": ({"request": dict(complete_record().request, session=None)}),
            "response_length": (
                {"request": dict(complete_record().request, response_length=None)}
            ),
            "decoder": ({"data": dict(complete_record().data, raw_type="unknown")}),
            "unit": ({"data": dict(complete_record().data, unit="unknown", enum=None)}),
        }

        for expected, overrides in cases.items():
            record = complete_record(**overrides)
            self.assertIn(expected, candidate_gate(record), expected)

    def test_byte_order_never_inferred_from_data_type(self):
        record = complete_record(
            data=dict(complete_record().data, byte_order="unknown")
        )
        self.assertIn("byte_order", candidate_gate(record))

    def test_single_byte_needs_no_byte_order(self):
        record = complete_record(
            data=dict(
                complete_record().data,
                raw_type="uint8", width_bytes=1, byte_order="unknown",
            )
        )
        self.assertEqual(candidate_gate(record), [])

    def test_tier_d_never_executes(self):
        record = complete_record(evidence_tier="D")
        self.assertIn("tier", candidate_gate(record))

    def test_rejected_never_executes(self):
        record = complete_record(verification="rejected")
        self.assertIn("verification", candidate_gate(record))

    def test_unsafe_and_unknown_operations_are_excluded(self):
        for safety in ("write_or_control", "service_operation",
                       "diagnostic_read", "unknown"):
            record = complete_record(safety=safety)
            self.assertIn("safety", candidate_gate(record), safety)

    def test_no_provenance_no_mapping(self):
        record = complete_record(source={})
        self.assertIn("provenance", candidate_gate(record))

    def test_no_response_validation_no_mapping(self):
        record = complete_record(
            request=dict(complete_record().request, matcher=None)
        )
        self.assertIn("matcher", candidate_gate(record))

    def test_license_metadata_is_mandatory(self):
        record = complete_record(license={})
        self.assertNotEqual(validate_record(record), [])
        self.assertIn("license", candidate_gate(record))
        # 'unknown' is a determination, not an omission - it passes.
        record = complete_record(license={"source_license": "unknown"})
        self.assertEqual(candidate_gate(record), [])


if __name__ == "__main__":
    unittest.main()
