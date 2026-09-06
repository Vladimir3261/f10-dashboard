"""
The tracked generated reports, and the one command that could truncate them.

`research/reports/n47-coverage.md` and `n47-conflicts.md` are committed
output of the full build (with the source cache). Two things must hold
without the cache present:

* the tracked coverage report is the withheld-bulk view: it COUNTS the
  `morguux-d73n47a0` rows and lists only the named ones (a regression
  to the 1,645-row table would be a licence problem, a regression to
  the 16-record evidence-only view would silently drop every cached
  source);
* `python3 -m research.build --reports-only` refuses to regenerate the
  reports from an evidence-only normalized set, because that is exactly
  the sequence the documentation suggests (`--evidence-only`, then
  `--reports-only`) and it used to overwrite both reports.
"""

import contextlib
import io
import os
import re
import tempfile
import unittest
from unittest import mock

from tests import support  # noqa: F401

from research import build

REPORTS = os.path.join(support.ROOT, "research", "reports")


def _read(name):
    with open(os.path.join(REPORTS, name), encoding="utf-8") as handle:
        return handle.read()


class TrackedCoverageReport(unittest.TestCase):
    """The committed file, not the generator on synthetic records."""

    def setUp(self):
        self.text = _read("n47-coverage.md")

    def test_total_counts_the_full_build(self):
        #: 1685 signal-definition records = 1645 D73N47A0 rows + the
        #: rest; an --evidence-only regeneration would say 16.
        self.assertIn("Signal-definition records: **1685**", self.text)

    def test_withheld_rows_are_counted_not_listed(self):
        self.assertIn("counted per group but\nnot listed", self.text)
        self.assertIn("**1619** rows from `morguux-d73n47a0`", self.text)

    def test_d73_rows_are_the_named_few(self):
        rows = len(re.findall(r"\| D73N47A0 \|", self.text))
        # 26 at the time of writing: only rows carrying a normalized
        # name. 0 means the report was rebuilt without the cache;
        # hundreds means the withheld bulk is back in the tree.
        self.assertGreaterEqual(rows, 1, "no D73N47A0 rows: rebuilt without the cache?")
        self.assertLessEqual(rows, 40, "the withheld D73N47A0 bulk is listed again")

    def test_no_raw_d73_source_identifiers_beyond_the_named_rows(self):
        # Every listed D73 row carries a normalized name in column 1.
        for line in self.text.splitlines():
            if "| D73N47A0 |" in line:
                name = line.split("|")[1].strip()
                self.assertNotEqual(name, "", line)
                self.assertNotEqual(name, "-", line)


class TrackedConflictsReport(unittest.TestCase):
    def test_conflicts_report_still_covers_the_cached_sources(self):
        text = _read("n47-conflicts.md")
        # The D73-vs-D73 alias conflicts only exist when the CSV was
        # imported; the evidence-only build reports none of them.
        self.assertIn("same_result_name_different_raw_type", text)
        self.assertIn("D73N47A0", text)


class ReportsOnlyRefusesPartialOutput(unittest.TestCase):
    """`--evidence-only` then `--reports-only` must not touch the reports."""

    def _run(self, argv, normalized, reports):
        out, err = io.StringIO(), io.StringIO()

        with mock.patch.object(build, "NORMALIZED", normalized), \
                mock.patch.object(build, "REPORTS", reports), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = build.main(argv)

        return code, out.getvalue(), err.getvalue()

    def test_sequence_leaves_the_reports_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            normalized = os.path.join(tmp, "normalized")
            reports = os.path.join(tmp, "reports")
            os.makedirs(reports)
            sentinel = "# do not overwrite\n"

            for name in ("n47-coverage.md", "n47-conflicts.md"):
                with open(os.path.join(reports, name), "w", encoding="utf-8") as handle:
                    handle.write(sentinel)

            code, _, _ = self._run(["--evidence-only"], normalized, reports)
            self.assertEqual(code, 0)
            self.assertTrue(os.path.isfile(os.path.join(normalized, "signals.jsonl")))

            code, _, err = self._run(["--reports-only"], normalized, reports)
            self.assertEqual(code, 1)
            self.assertIn("refused", err)
            self.assertIn("morguux-d73n47a0", err)

            for name in ("n47-coverage.md", "n47-conflicts.md"):
                with open(os.path.join(reports, name), encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), sentinel, name)

            # --force is the explicit escape hatch, and says what it drops.
            code, _, err = self._run(["--reports-only", "--force"], normalized, reports)
            self.assertEqual(code, 0)
            self.assertIn("--force", err)

            with open(os.path.join(reports, "n47-coverage.md"), encoding="utf-8") as handle:
                self.assertIn("Signal-definition records: **16**", handle.read())

    def test_detection_is_by_cached_source_id(self):
        self.assertEqual(
            build.missing_cached_sources([]),
            sorted(build.CACHED_SOURCE_IDS),
        )
        self.assertEqual(
            build.CACHED_SOURCE_IDS,
            {"morguux-d73n47a0", "ediabaslib", "bmw-xdfs-testo"},
        )


if __name__ == "__main__":
    unittest.main()
