"""
D73N47A0 CSV importer: deterministic, preserve-everything, invent-nothing.

The committed fixture is a verbatim excerpt of the pinned gist; the full
1645-row import runs only when the cached copy is present and matches
its pinned hash - exactly what research.build enforces.
"""

import hashlib
import os
import unittest

from tests import support  # noqa: F401  (sys.path)

from research.importers import d73n47_csv
from research.model import records_to_jsonl, validate_record

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "d73_excerpt.csv")
CACHE = os.path.join(
    support.ROOT, "local", "research-cache", "gists", "morguux", "D73N47A0.csv"
)


def load_fixture():
    with open(FIXTURE, encoding="utf-8") as handle:
        return handle.read()


class CsvParsing(unittest.TestCase):
    def setUp(self):
        self.records, self.summary = d73n47_csv.import_csv(load_fixture())
        self.by_id = {r.record_id: r for r in self.records}

    def test_every_row_parses(self):
        self.assertEqual(self.summary.rows, 19)
        self.assertEqual(len(self.records), 19)
        self.assertEqual(self.summary.problems, [])

    def test_quoted_comma_row_parses(self):
        """The CSV contains a quoted field with an embedded comma."""
        record = self.by_id["d73n47a0.FBC_DXSl_mp_2"]
        self.assertIn("tooth pitch error 2", record.source["source_description"])

    def test_every_original_field_preserved(self):
        record = self.by_id["d73n47a0.PFltLd_mSotSimCont"]
        source = record.source
        self.assertEqual(source["source_record"], "PFltLd_mSotSimCont")
        self.assertEqual(source["source_identifier_raw"], "0x0406")
        self.assertEqual(source["source_result_name"], "STAT_PFltLd_mSotSimCont_WERT")
        self.assertEqual(source["source_data_type"], "unsigned int")
        self.assertEqual(source["source_unit"], "g")
        self.assertEqual(source["source_mul"], "0.010000")
        self.assertEqual(source["source_add"], "0.000000")
        self.assertEqual(source["source_label"], "PFltLd_mSotSimCont")
        self.assertEqual(
            source["source_description"], "Continuously simulated particulate mass"
        )

    def test_hex_id_normalization(self):
        self.assertEqual(
            self.by_id["d73n47a0.OBD_PID05_CEngDsT_tSens"]
            .source["source_identifier"], "0x0005",
        )
        self.assertEqual(
            self.by_id["d73n47a0.ITMOT"].source["source_identifier"], "0x0AF1"
        )

    def test_decimal_factor_precision_is_not_floated(self):
        """Factors stay decimal strings; 0.015259 must not round-trip."""
        record = self.by_id["d73n47a0.IMRUP"]
        self.assertEqual(record.data["mul"], "0.015259")
        self.assertEqual(self.by_id["d73n47a0.ITOEL"].data["add"], "-100.000000")

    def test_data_type_normalization(self):
        self.assertEqual(self.by_id["d73n47a0.ITMOT"].data["raw_type"], "uint16")
        self.assertEqual(
            self.by_id["d73n47a0.Com_tGbxOil"].data["raw_type"], "uint8"
        )
        self.assertEqual(
            self.by_id["d73n47a0.PFltRgn_tiSnceRgn"].data["raw_type"], "uint32"
        )
        float_rec = self.by_id["d73n47a0.AirCtl_dmEGRDes_r32"]
        self.assertEqual(float_rec.data["raw_type"], "float32")
        self.assertEqual(float_rec.data["byte_order"], "big")   # 'motorola'

    def test_unknown_data_type_preserved_not_invented(self):
        text = load_fixture() + (
            "Weird,0x1111,STAT_WEIRD_WERT,signed frob,x,1.0,0.0,W,w\n"
        )
        records, summary = d73n47_csv.import_csv(text)
        record = {r.record_id: r for r in records}["d73n47a0.Weird"]
        self.assertEqual(record.data["raw_type"], "unknown")
        self.assertEqual(record.source["source_data_type"], "signed frob")
        self.assertEqual(record.category, "unknown")

    def test_integer_byte_order_stays_unknown(self):
        self.assertEqual(self.by_id["d73n47a0.ITMOT"].data["byte_order"], "unknown")

    def test_request_semantics_never_fabricated(self):
        for record in self.records:
            self.assertIn(record.request["completeness"], ("unknown",))
            self.assertEqual(record.request["sequence"], "unknown")
            self.assertEqual(record.request["target"], "unknown")

    def test_applicability_and_license(self):
        for record in self.records:
            self.assertEqual(record.applicability["sgbd"], "D73N47A0")
            self.assertEqual(
                record.applicability["target_chassis_status"], "unverified"
            )
            self.assertEqual(record.license["source_license"], "unknown")

    def test_categories(self):
        self.assertEqual(
            self.by_id["d73n47a0.OBD_PID0C_Epm_nEngRaw"].category,
            "standard_obd_crossref",
        )
        # File order puts IMRUP (0x03EA) before PFltLd_mSotMeas (0x0405),
        # so the PFltLd row is the one flagged as the alias.
        self.assertEqual(
            self.by_id["d73n47a0.IMRUP"].category, "partial_signal_definition"
        )
        self.assertEqual(
            self.by_id["d73n47a0.PFltLd_mSotMeas"].category, "duplicate_alias"
        )

    def test_duplicate_id_detection(self):
        text = load_fixture()
        dup = "Dup,0x0406,STAT_DUP_WERT,unsigned int,g,1.0,0.0,Dup,dup\n"
        _, summary = d73n47_csv.import_csv(text + dup)
        self.assertIn("0x0406", summary.duplicate_ids)

    def test_records_validate(self):
        for record in self.records:
            self.assertEqual(validate_record(record), [], record.record_id)

    def test_deterministic_output(self):
        again, _ = d73n47_csv.import_csv(load_fixture())
        self.assertEqual(records_to_jsonl(self.records), records_to_jsonl(again))


class FullCsv(unittest.TestCase):
    """Runs only when the pinned cache copy is present."""

    def setUp(self):
        if not os.path.isfile(CACHE):
            self.skipTest("source cache not populated")

        with open(CACHE, encoding="utf-8") as handle:
            self.text = handle.read()

        digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()

        if digest != d73n47_csv.PINNED_SHA256:
            self.fail(f"cached CSV does not match pin: {digest}")

    def test_full_import(self):
        records, summary = d73n47_csv.import_csv(self.text)
        self.assertEqual(summary.rows, 1645)
        self.assertEqual(len(records), 1645)
        self.assertEqual(summary.duplicate_ids, [])
        self.assertEqual(summary.problems, [])
        # No executable candidates may come from a table with no
        # request semantics.
        self.assertNotIn("complete_runtime_candidate", summary.categories)
        # The brief's identifier claims, verified against the real rows.
        by_ident = {
            r.source["source_identifier"]: r for r in records
        }
        for ident in ("0x0405", "0x0406", "0x0407", "0x0408", "0x0409",
                      "0x040A", "0x040F", "0x0604", "0x0605", "0x0607",
                      "0x0608", "0x0EA6", "0x0EA7"):
            self.assertIn(ident, by_ident, ident)


if __name__ == "__main__":
    unittest.main()
