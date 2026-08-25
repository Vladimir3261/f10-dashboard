"""
Deep OBD ccpage and TestO customjobs importers: partial knowledge in,
partial records out - job names, ARG lists and result names, never an
invented request.
"""

import os
import unittest

from tests import support  # noqa: F401

from research.importers import deep_obd_xml, test_o_customjobs
from research.model import records_to_jsonl, validate_record

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


class DeepObd(unittest.TestCase):
    def setUp(self):
        self.records, self.problems = deep_obd_xml.import_ccpage(
            load("motor_ccpage_excerpt.xml")
        )
        self.by_id = {r.record_id: r for r in self.records}

    def test_job_record(self):
        job = self.by_id["deepobd.e90.STATUS_MESSWERTBLOCK_LESEN"]
        self.assertEqual(job.source["sgbd_group"], "d_motor")
        self.assertIn("ITOEL", job.source["args"])
        self.assertIn("ITMOT", job.source["args"])
        self.assertNotIn("NEIN", job.source["args"])   # block flag, not an ARG

    def test_result_name_records_are_partial(self):
        record = self.by_id[
            "deepobd.e90.STATUS_MESSWERTBLOCK_LESEN.MOTOROEL_TEMPERATUR"
        ]
        self.assertEqual(
            record.source["source_result_name"], "STAT_MOTOROEL_TEMPERATUR_WERT"
        )
        self.assertEqual(record.request["completeness"], "unknown")
        self.assertEqual(record.data["mul"], "unknown")
        self.assertEqual(record.evidence_tier, "C")

    def test_gpl_license_recorded(self):
        for record in self.records:
            self.assertEqual(record.license["source_license"], "GPL-3.0")

    def test_valid_and_deterministic(self):
        for record in self.records:
            self.assertEqual(validate_record(record), [], record.record_id)

        again, _ = deep_obd_xml.import_ccpage(load("motor_ccpage_excerpt.xml"))
        self.assertEqual(records_to_jsonl(self.records), records_to_jsonl(again))


class TestO(unittest.TestCase):
    def setUp(self):
        self.records, self.problems = test_o_customjobs.import_customjobs(
            load("customjobs_excerpt.xml")
        )
        self.by_id = {r.record_id: r for r in self.records}

    def test_n47_filter(self):
        self.assertTrue(self.records)

        for record in self.records:
            self.assertIn("N47", record.source["sgbd"])

    def test_identifier_sets_preserved(self):
        record = self.by_id["testo.D71N47A0.STATUS_MESSWERTBLOCK_LESEN.SET_1"]
        self.assertEqual(record.source["identifiers"], ["0x13A6", "0x0080"])
        self.assertEqual(record.source["job"], "STATUS_MESSWERTBLOCK_LESEN")

    def test_variants_stay_distinct(self):
        variants = {r.applicability["sgbd"] for r in self.records}
        self.assertIn("D71N47A0", variants)
        self.assertIn("D71N47C0", variants)
        self.assertIn("D71N47D0", variants)

    def test_nothing_claims_more_than_a_job_and_ids(self):
        for record in self.records:
            self.assertEqual(record.record_type, "job_definition")
            self.assertEqual(record.verification, "discovered")
            self.assertEqual(record.request["completeness"], "unknown")


if __name__ == "__main__":
    unittest.main()
