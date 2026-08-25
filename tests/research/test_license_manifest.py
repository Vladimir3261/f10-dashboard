"""
The source manifest: pins and license metadata are mandatory.
"""

import unittest

from tests import support  # noqa: F401

from research.manifest import (
    check_source_ids,
    load_manifest,
    load_relationships,
    validate_manifest,
)
from research.model import EVIDENCE_RELATIONSHIPS, ResearchRecord


class Manifest(unittest.TestCase):
    def setUp(self):
        self.sources = load_manifest()

    def test_manifest_loads_and_validates(self):
        self.assertGreaterEqual(len(self.sources), 20)
        self.assertEqual(validate_manifest(self.sources), [])

    def test_every_source_has_license_metadata(self):
        for source_id, entry in self.sources.items():
            self.assertIn("id", entry["license"], source_id)

    def test_unknown_is_an_explicit_license_determination(self):
        self.assertEqual(self.sources["morguux-d73n47a0"]["license"]["id"],
                         "unknown")
        self.assertEqual(self.sources["bmw-xdfs-testo"]["license"]["id"],
                         "unknown")

    def test_restrictive_licenses_are_marked_no_runtime_copy(self):
        for source_id in ("klartext", "ediabaslib", "ediabasx", "bimmerz-box"):
            self.assertIs(
                self.sources[source_id]["license"]
                ["commercial_runtime_copy_allowed"],
                False, source_id,
            )

    def test_git_sources_are_pinned(self):
        for source_id, entry in self.sources.items():
            if entry.get("type") == "git_repository":
                self.assertTrue(
                    entry.get("commit") or entry.get("revision"), source_id
                )

    def test_missing_license_fails_validation(self):
        broken = {"x": {"name": "X", "type": "note", "url": "u",
                        "retrieved_at": "t"}}
        problems = validate_manifest(broken)
        self.assertTrue(any("license" in p for p in problems))

    def test_missing_pin_fails_validation(self):
        broken = {"x": {"name": "X", "type": "git_repository", "url": "u",
                        "retrieved_at": "t", "license": {"id": "MIT"}}}
        problems = validate_manifest(broken)
        self.assertTrue(any("pinned" in p for p in problems))

    def test_record_with_unmanifested_source_is_flagged(self):
        record = ResearchRecord(
            record_id="x", record_type="signal_definition",
            source_id="some-blog-i-remember", evidence_tier="D",
            verification="discovered", safety="unknown",
            request={"completeness": "unknown"},
            license={"source_license": "unknown"},
        )
        problems = check_source_ids([record], self.sources)
        self.assertEqual(len(problems), 1)
        self.assertIn("some-blog-i-remember", problems[0])

    def test_relationships_use_the_closed_vocabulary(self):
        rows = load_relationships()
        self.assertGreaterEqual(len(rows), 5)

        for row in rows:
            self.assertIn(row["type"], EVIDENCE_RELATIONSHIPS)
            self.assertIn(row["a"], self.sources)
            self.assertIn(row["b"], self.sources)

    def test_rejected_sources_are_recorded_not_forgotten(self):
        self.assertEqual(
            self.sources["govmateai-bmw-pro-diagnostic"]["trust"]["tier"], "D"
        )
        self.assertEqual(
            self.sources["freecarly-decompiled"]["trust"]["tier"], "D"
        )


if __name__ == "__main__":
    unittest.main()
