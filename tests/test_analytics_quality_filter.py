"""
The lake consumers must read the quality label, not re-derive it.

Two failure modes this guards, both of which were real in this repo:

  * a health query that interprets `value` as a physical measurement
    without filtering `quality`, so a sentinel or a railed sensor enters
    a trend as an ordinary observation;
  * a "data quality" panel that rediscovers a known case numerically -
    `value >= 255` for MAP, `value >= 2.0` for lambda - which is the
    ad-hoc interpretation the mapping now owns. Those two lines lived in
    insights.sql and in the Grafana dashboard for weeks.

These assert over the committed SQL rather than over a database, because
the defect is in the query text: it cannot be caught by running the query
against data that happens to contain nothing flagged.
"""

import json
import os
import re
import unittest

from tests import support

SQL = os.path.join(support.ROOT, "analysis", "clickhouse", "insights.sql")
DASHBOARD = os.path.join(
    support.ROOT, "infra", "grafana", "dashboards", "f10-health.json"
)

#: Numeric rediscovery of a case the mapping already declares. The
#: whitespace-insensitive forms of `value>=255` and `value>=2.0`.
REDISCOVERY = re.compile(r"value\s*>=\s*(255|2\.0)\b")

#: A query that REPORTS ON flagged rows rather than measuring - it names
#: a quality label other than 'ok'. Those must not be filtered to ok, or
#: they silently return nothing.
REPORTS_ON_QUALITY = re.compile(r"quality\s*=\s*'(?!ok')")

#: A subquery that pulls `value` for one named channel - i.e. one that is
#: about to interpret it as a measurement.
CHANNEL_SELECT = re.compile(
    r"FROM telemetry\.samples\s+WHERE[^)]*?channel(_raw)?\s*=\s*'[^']+'[^)]*",
    re.IGNORECASE | re.DOTALL,
)


def sql_text():
    with open(SQL) as fh:
        return fh.read()


def sql_code():
    """
    The battery with `--` comments stripped.

    Prose is allowed to mention the old numeric tests - the file explains
    why they were removed, and that explanation is worth keeping. Only
    executable SQL is scanned for them.
    """
    return "\n".join(
        re.sub(r"--.*$", "", line) for line in sql_text().splitlines()
    )


def dashboard():
    with open(DASHBOARD) as fh:
        return json.load(fh)


def panel_sql(panel):
    return " ".join(
        (t.get("rawSql") or "") for t in panel.get("targets", [])
    )


class NoNumericRediscovery(unittest.TestCase):
    """
    The mapping declares MAP's rail and lambda's sentinel. Nothing
    downstream may go back to guessing at the number.
    """

    def test_the_query_battery_is_clean(self):
        found = REDISCOVERY.findall(sql_code())

        self.assertEqual(
            found, [],
            "insights.sql rediscovers a declared case numerically; read "
            "the recorded `quality` label instead",
        )

    def test_the_grafana_dashboard_is_clean(self):
        for panel in dashboard()["panels"]:
            with self.subTest(panel=panel.get("title")):
                self.assertEqual(
                    REDISCOVERY.findall(panel_sql(panel)), [],
                    "this panel rediscovers a declared case numerically; "
                    "read the recorded `quality` label instead",
                )

    def test_the_two_known_cases_are_read_from_the_label(self):
        panels = {p["title"]: panel_sql(p) for p in dashboard()["panels"]}
        saturated = [q for t, q in panels.items() if "MAP saturated" in t]
        sentinel = [q for t, q in panels.items() if "sentinel" in t]

        self.assertTrue(saturated and "quality='saturated'" in saturated[0])
        self.assertTrue(sentinel and "quality='sentinel'" in sentinel[0])


class HealthQueriesExcludeFlaggedRows(unittest.TestCase):
    def _sections(self):
        """insights.sql split on its numbered section headers."""
        parts = re.split(r"\n-- (\d+[a-z]?)\. ", "\n" + sql_text())

        return list(zip(parts[1::2], parts[2::2]))

    def test_every_measurement_subquery_filters_quality(self):
        for number, body in self._sections():
            if body.lower().startswith("data quality"):
                continue                      # reporting ON flagged rows

            for match in CHANNEL_SELECT.finditer(body):
                with self.subTest(section=number, sql=match.group(0)[:70]):
                    self.assertIn(
                        "quality='ok'", match.group(0),
                        "this subquery interprets `value` as a measurement "
                        "but does not exclude flagged readings",
                    )

    def test_the_data_quality_sections_are_NOT_filtered(self):
        #
        # The inverse mistake, and just as wrong: filtering to ok inside
        # the section whose entire job is reporting the non-ok rows would
        # make it silently return nothing.
        #
        reporting = [
            body for number, body in self._sections()
            if body.lower().startswith("data quality")
            or body.lower().startswith("channels answering")
        ]

        self.assertTrue(reporting, "the quality-reporting sections vanished")

        for body in reporting:
            self.assertNotIn("quality='ok'", body)

    def test_every_grafana_health_panel_filters_quality(self):
        for panel in dashboard()["panels"]:
            sql = panel_sql(panel)

            if not CHANNEL_SELECT.search(sql):
                continue        # inventory panels select no channel

            if REPORTS_ON_QUALITY.search(sql):
                continue        # this panel's job IS the flagged rows

            with self.subTest(panel=panel.get("title")):
                self.assertIn(
                    "quality='ok'", sql,
                    "this health panel reads values without excluding "
                    "flagged readings",
                )

    def test_the_historical_caveat_is_recorded(self):
        #
        # Pre-quality rows are 'ok' because nothing labelled them, not
        # because they were verified clean. Anyone trending across that
        # boundary is comparing two different questions, and the file has
        # to say so.
        #
        self.assertIn("before the data-quality layer", sql_text())

        described = " ".join(
            p.get("description", "") for p in dashboard()["panels"]
        )

        self.assertIn("before the data-quality layer", described)
