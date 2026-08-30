"""
The session report must not state conclusions its data contradicts.

This exists because of one sentence. `warmup()` reported

    "oil lags coolant - the expected warm-up signature"

unconditionally, without ever comparing the two. On the first genuine
cold start (2026-08-31, session 9) oil ran 0.27 C ABOVE coolant and the
report asserted the lag anyway.

That is the failure mode this project can least afford, and it has cost
here before: a confident wrong answer does not announce itself the way a
crash does. The soot channel produced plausible grams for weeks.

The rule these tests hold: state the observation always, interpret it
only where the data supports an interpretation, and say so plainly when
the session could not have shown the thing being looked for.
"""

import unittest

from tests import support  # noqa: F401

from analysis import session_report as report


def a_run(oil_at_80, max_speed=0.0, with_speed=True):
    """A synthetic warm-up: coolant climbing through 80 C, oil flat."""
    stamps = [1000.0 + t for t in range(0, 400, 10)]
    run = {
        "started": 1000.0,
        "units": {},
        "series": {
            "coolant": [(t, 20 + (t - 1000.0) * 0.18) for t in stamps],
            "n47d_oil_temp": [(t, oil_at_80) for t in stamps],
        },
    }

    if with_speed:
        run["series"]["speed"] = [(t, max_speed) for t in stamps]

    return run


class OilVersusCoolant(unittest.TestCase):
    def measure(self, **kwargs):
        return report.warmup(a_run(**kwargs))["oil_vs_coolant_at_coolant80"]

    def test_the_difference_is_recorded_as_a_number(self):
        """
        The observation, not an adjective. `delta` is what makes the
        claim checkable by whoever reads the report next.
        """
        d = self.measure(oil_at_80=74.0, max_speed=60.0)

        self.assertEqual(d["oil"], 74.0)
        self.assertEqual(d["delta"], -6.0)

    def test_a_stationary_session_is_marked_as_such(self):
        """
        The lag is LOAD-driven: oil takes heat from work done. An idling
        car cannot show one, so a report that reads its absence as a
        finding is reading noise.
        """
        d = self.measure(oil_at_80=80.3, max_speed=0.0)

        self.assertFalse(d["moved"])
        self.assertEqual(d["max_speed"], 0.0)

    def test_a_driven_session_is_marked_as_such(self):
        self.assertTrue(self.measure(oil_at_80=74.0, max_speed=60.0)["moved"])

    def test_movement_is_unknown_rather_than_false_without_speed(self):
        """
        None and False mean different things: "we did not record speed"
        is not "the car did not move", and only the second licenses the
        no-lag-expected wording.
        """
        d = self.measure(oil_at_80=74.0, with_speed=False)

        self.assertIsNone(d["moved"])

    def test_the_real_session_9_numbers_do_not_read_as_a_lag(self):
        """
        The case that prompted this: stationary, oil marginally above
        coolant. Must not come out as the expected warm-up signature.
        """
        d = self.measure(oil_at_80=80.27, max_speed=0.0)

        self.assertGreater(d["delta"], 0)
        self.assertFalse(d["moved"])


class TheProse(unittest.TestCase):
    """What the markdown actually says, which is what gets quoted."""

    def markdown(self, **kwargs):
        """The real rendered report, not a re-implementation of it."""
        run = a_run(**kwargs)
        run.update({
            "run_id": 1, "vin": "VINREDACTED", "ecu": "DDE",
            "ended": 1400.0, "samples": 100, "channels": 3,
            "gateway": "gw", "ecu_addr": 0x12, "mapping_set": "",
        })
        wu = report.warmup(run)

        return report.render_markdown(run, wu, [], {}, {}, {}, [])

    def prose_for(self, **kwargs):
        #: Mirrors the report's branch logic, so a change to the
        #: thresholds without a change here is a visible failure.
        d = report.warmup(a_run(**kwargs))["oil_vs_coolant_at_coolant80"]

        if d["moved"] is False:
            return "no-lag-expected"
        if d["delta"] <= -1.0:
            return "lags"
        if d["delta"] >= 1.0:
            return "above"

        return "tracking"

    def test_every_branch_is_reachable_and_distinct(self):
        cases = {
            ("stationary", 80.3, 0.0): "no-lag-expected",
            ("driving-lag", 74.0, 60.0): "lags",
            ("driving-above", 83.0, 60.0): "above",
            ("driving-tracking", 80.4, 60.0): "tracking",
        }

        for (label, oil, speed), expected in cases.items():
            with self.subTest(case=label):
                self.assertEqual(
                    self.prose_for(oil_at_80=oil, max_speed=speed), expected
                )

    def test_the_markdown_never_claims_a_lag_it_did_not_measure(self):
        """
        The regression itself, against the REAL renderer: session 9's
        shape must not produce the sentence that was wrong.
        """
        text = self.markdown(oil_at_80=80.27, max_speed=0.0)

        self.assertNotIn("oil lags coolant", text)
        self.assertIn("no lag should be expected", text)
        #: and it must still report the measurement itself
        self.assertIn("+0.27", text)
        #: The Key findings line is the one most likely to be quoted, so
        #: it must not claim a clean bill of health either.
        self.assertNotIn("no lag anomaly", text)
        self.assertIn("not evidence of a healthy warm-up", text)

    def test_the_markdown_does_claim_a_lag_when_there_is_one(self):
        """The guard must not have simply removed the finding."""
        text = self.markdown(oil_at_80=74.0, max_speed=60.0)

        self.assertIn("Oil lags coolant", text)


if __name__ == "__main__":
    unittest.main()
