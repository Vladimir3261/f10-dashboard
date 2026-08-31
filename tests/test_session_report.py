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


def a_run(offset, max_speed=0.0, with_speed=True):
    """
    A synthetic warm-up: coolant climbing through 80 C, with oil tracking
    it at a constant `offset`.

    Oil tracks rather than sitting at a fixed value, because the report
    now averages the difference over MATCHED PAIRS across the ramp rather
    than reading one sample at the crossing. A flat oil series would make
    the mean an artifact of where the ramp started.
    """
    stamps = [1000.0 + t for t in range(0, 400, 10)]
    run = {
        "started": 1000.0,
        "units": {},
        #: These fixtures exercise warm-up behaviour, which is entirely
        #: time-derived, so the run has to declare a disciplined clock.
        #: The analysis fails closed without it - by design, see
        #: session_report.time_trusted().
        "clock_synced": 1,
        "series": {
            "coolant": [(t, 20 + (t - 1000.0) * 0.18) for t in stamps],
            #: +2 s, so pairing has to tolerate real sampling skew.
            "n47d_oil_temp": [
                (t + 2.0, 20 + (t - 1000.0) * 0.18 + offset) for t in stamps
            ],
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
        d = self.measure(offset=-6.0, max_speed=60.0)

        self.assertAlmostEqual(d["delta"], -6.0, places=1)
        self.assertGreater(d["pairs"], 10)

    def test_the_delta_is_a_mean_over_pairs_not_one_sample(self):
        """
        A single instant is within the sampling error: on session 9 the
        same data gave -0.2, +0.27 or +0.50 C depending purely on how the
        two series were lined up. The range is reported alongside so a
        reader can see the spread rather than trusting a point estimate.
        """
        d = self.measure(offset=0.35, max_speed=0.0)

        self.assertAlmostEqual(d["delta"], 0.35, places=1)
        self.assertIsNotNone(d["delta_min"])
        self.assertIsNotNone(d["delta_max"])

    def test_unpairable_series_give_no_delta_rather_than_a_wrong_one(self):
        """
        If nothing lines up within tolerance there is no measurement, and
        the report must say nothing rather than invent a number.
        """
        run = a_run(offset=0.0, max_speed=0.0)
        #: Shift oil far outside the pairing tolerance.
        run["series"]["n47d_oil_temp"] = [
            (t + 600.0, v) for t, v in run["series"]["n47d_oil_temp"]
        ]
        d = report.warmup(run)["oil_vs_coolant_at_coolant80"]

        self.assertIsNone(d["delta"])

    def test_a_stationary_session_is_marked_as_such(self):
        """
        The lag is LOAD-driven: oil takes heat from work done. An idling
        car cannot show one, so a report that reads its absence as a
        finding is reading noise.
        """
        d = self.measure(offset=0.3, max_speed=0.0)

        self.assertFalse(d["moved"])
        self.assertEqual(d["max_speed"], 0.0)

    def test_a_driven_session_is_marked_as_such(self):
        self.assertTrue(self.measure(offset=-6.0, max_speed=60.0)["moved"])

    def test_movement_is_unknown_rather_than_false_without_speed(self):
        """
        None and False mean different things: "we did not record speed"
        is not "the car did not move", and only the second licenses the
        no-lag-expected wording.
        """
        d = self.measure(offset=-6.0, with_speed=False)

        self.assertIsNone(d["moved"])

    def test_the_real_session_9_numbers_do_not_read_as_a_lag(self):
        """
        The case that prompted this: stationary, oil marginally above
        coolant. Must not come out as the expected warm-up signature.
        """
        d = self.measure(offset=0.35, max_speed=0.0)

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
            ("stationary", 0.3, 0.0): "no-lag-expected",
            ("driving-lag", -6.0, 60.0): "lags",
            ("driving-above", 3.0, 60.0): "above",
            ("driving-tracking", 0.4, 60.0): "tracking",
        }

        for (label, oil, speed), expected in cases.items():
            with self.subTest(case=label):
                self.assertEqual(
                    self.prose_for(offset=oil, max_speed=speed), expected
                )

    def test_the_markdown_never_claims_a_lag_it_did_not_measure(self):
        """
        The regression itself, against the REAL renderer: session 9's
        shape must not produce the sentence that was wrong.
        """
        text = self.markdown(offset=0.35, max_speed=0.0)

        self.assertNotIn("oil lags coolant", text)
        self.assertIn("no lag should be expected", text)
        #: and it must still report the measurement itself
        self.assertIn("+0.35", text)
        #: The Key findings line is the one most likely to be quoted, so
        #: it must not claim a clean bill of health either.
        self.assertNotIn("no lag anomaly", text)
        self.assertIn("not evidence of a healthy warm-up", text)

    def test_the_markdown_does_claim_a_lag_when_there_is_one(self):
        """The guard must not have simply removed the finding."""
        text = self.markdown(offset=-6.0, max_speed=60.0)

        self.assertIn("Oil lags coolant", text)


if __name__ == "__main__":
    unittest.main()
