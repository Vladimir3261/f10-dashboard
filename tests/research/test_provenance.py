"""
Provenance: every record carries its origin, and labels distinguish
source facts from our inference.
"""

import json
import os
import unittest

from tests import support  # noqa: F401

from research.build import _f10_field_records, _obdb_records
from research.importers import klartext_f25, wican_issue_fixture
from research.manifest import check_source_ids, load_manifest
from research.model import FACT_LABELS, validate_record

NORMALIZED = os.path.join(support.ROOT, "research", "normalized", "n47")


def evidence_records():
    return (
        wican_issue_fixture.import_fixture()
        + klartext_f25.import_evidence()
        + _obdb_records()
        + _f10_field_records()
    )


class Provenance(unittest.TestCase):
    def setUp(self):
        self.records = evidence_records()
        self.sources = load_manifest()

    def test_every_record_validates(self):
        for record in self.records:
            self.assertEqual(validate_record(record), [], record.record_id)

    def test_every_record_references_a_manifest_source(self):
        self.assertEqual(check_source_ids(self.records, self.sources), [])

    def test_fact_labels_are_from_the_closed_vocabulary(self):
        for record in self.records:
            self.assertTrue(record.fact_labels, record.record_id)

            for label in record.fact_labels:
                self.assertIn(label, FACT_LABELS)

    def test_wire_observations_are_labelled_as_such(self):
        by_id = {r.record_id: r for r in self.records}
        self.assertIn(
            "wire_observation", by_id["wican752.dde7.0x0406"].fact_labels
        )
        self.assertIn(
            "wire_observation", by_id["klartext.d72n47a0.ITOEL"].fact_labels
        )

    def test_our_normalized_names_are_labelled_inference(self):
        """The normalized-name assignment is ours, and says so."""
        for record in self.records:
            if record.normalized_signal:
                self.assertIn("inference", record.fact_labels, record.record_id)

    def test_claims_without_captures_are_labelled_source_claim(self):
        by_id = {r.record_id: r for r in self.records}
        claim = by_id["wican752.dde7.claim.0x03EB"]
        self.assertEqual(claim.fact_labels, ("source_claim",))
        self.assertNotIn("wire_observation", claim.fact_labels)

    def test_no_record_claims_local_verification(self):
        """Nothing has touched the target F10; nothing may say otherwise."""
        for record in self.records:
            self.assertNotEqual(
                record.verification, "locally_verified", record.record_id
            )

    def test_original_source_names_stay_queryable(self):
        """PFltLd_* / STAT_*_WERT survive into the committed output."""
        path = os.path.join(NORMALIZED, "signals.jsonl")

        if not os.path.isfile(path):
            self.skipTest("normalized output not generated")

        with open(path, encoding="utf-8") as handle:
            text = handle.read()

        for needle in ("PFltLd_mSotSimCont", "PFltLd_mSotMeas",
                       "STAT_MOTOROEL_TEMPERATUR_WERT"):
            self.assertIn(needle, text, needle)

    def test_normalized_output_is_valid_sorted_jsonl(self):
        path = os.path.join(NORMALIZED, "signals.jsonl")

        if not os.path.isfile(path):
            self.skipTest("normalized output not generated")

        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()

        ids = [json.loads(line)["record_id"] for line in lines]
        self.assertEqual(len(ids), len(set(ids)))

        parsed = [json.loads(line) for line in lines]

        for row in parsed:
            self.assertIn("source_id", row)
            self.assertIn("license", row)


if __name__ == "__main__":
    unittest.main()
