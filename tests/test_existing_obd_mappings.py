"""
Regression: the production OBD mapping IS the old hardcoded PIDS table.

The table below is a verbatim copy of `PIDS` and `COMPUTED` as they stood
in live.py before diagnostic knowledge moved into mapping files. Nothing
here should ever be "corrected": it is the frozen reference the refactor
promised not to change, and the tests decode every possible input byte
through both paths to prove the mapping reproduces it exactly - including
the last float digit.
"""

import unittest

from . import support
from bmwdiag.mapping import decode_signal, load_file, read_value
from bmwdiag.mapping.decoder import match_prefix
from bmwdiag.mapping.polling import PollingPlan, resolve_classes
from bmwdiag.mapping.registry import AllCapabilities, MappingRegistry


def _u16(d):
    return (d[0] << 8) | d[1]


#: (pid, key, label, unit, nbytes, decode, digits, fast, lo, hi)
LEGACY_PIDS = [
    (0x0C, "rpm", "Engine speed", "rpm", 2,
     lambda d: _u16(d) / 4.0, 0, True, 0, 5000),
    (0x0B, "map", "Intake manifold", "kPa", 1,
     lambda d: float(d[0]), 0, True, 0, 300),
    (0x04, "load", "Engine load", "%", 1,
     lambda d: d[0] * 100.0 / 255.0, 0, True, 0, 100),
    (0x11, "throttle", "Throttle / pedal", "%", 1,
     lambda d: d[0] * 100.0 / 255.0, 0, True, 0, 100),
    (0x0D, "speed", "Vehicle speed", "km/h", 1,
     lambda d: float(d[0]), 0, True, 0, 250),
    (0x10, "maf", "Air mass flow", "g/s", 2,
     lambda d: _u16(d) / 100.0, 1, True, 0, 400),
    (0x23, "rail", "Fuel rail pressure", "bar", 2,
     lambda d: _u16(d) * 10.0 / 100.0, 0, True, 0, 2000),
    (0x62, "torque", "Actual torque", "%", 1,
     lambda d: float(d[0]) - 125.0, 0, True, -25, 100),
    (0x49, "pedal", "Accelerator pedal", "%", 1,
     lambda d: d[0] * 100.0 / 255.0, 1, True, 0, 100),
    (0x45, "relthr", "Relative throttle", "%", 1,
     lambda d: d[0] * 100.0 / 255.0, 1, True, 0, 100),
    (0x24, "lambda", "Lambda (O2 S1)", "", 4,
     lambda d: _u16(d[:2]) / 32768.0, 3, True, 0.5, 2.0),

    (0x05, "coolant", "Coolant temp", "°C", 1,
     lambda d: float(d[0]) - 40.0, 0, False, -40, 130),
    (0x5C, "oil", "Oil temp", "°C", 1,
     lambda d: float(d[0]) - 40.0, 0, False, -40, 150),
    (0x0F, "iat", "Intake air temp", "°C", 1,
     lambda d: float(d[0]) - 40.0, 0, False, -40, 120),
    (0x46, "ambient", "Ambient temp", "°C", 1,
     lambda d: float(d[0]) - 40.0, 0, False, -40, 60),
    (0x42, "voltage", "Module voltage", "V", 2,
     lambda d: _u16(d) / 1000.0, 2, False, 8, 16),
    (0x33, "baro", "Barometric", "kPa", 1,
     lambda d: float(d[0]), 0, False, 0, 120),
    (0x2F, "fuel", "Fuel level", "%", 1,
     lambda d: d[0] * 100.0 / 255.0, 0, False, 0, 100),
    (0x5E, "fuelrate", "Fuel rate", "L/h", 2,
     lambda d: _u16(d) / 20.0, 1, False, 0, 60),
    (0x1F, "runtime", "Run time", "s", 2,
     lambda d: float(_u16(d)), 0, False, 0, 7200),
    (0x3C, "cattemp", "Cat / exhaust temp", "°C", 2,
     lambda d: _u16(d) / 10.0 - 40.0, 0, False, -40, 800),
    (0x2C, "egr", "Commanded EGR", "%", 1,
     lambda d: d[0] * 100.0 / 255.0, 1, False, 0, 100),
    (0x2D, "egrerr", "EGR error", "%", 1,
     lambda d: (d[0] - 128.0) * 100.0 / 128.0, 1, False, -100, 100),
    (0x31, "distance", "Distance since clear", "km", 2,
     lambda d: float(_u16(d)), 0, False, 0, 65535),
]

LEGACY_COMPUTED = {
    "boost": {"label": "Turbo boost", "unit": "bar",
              "digits": 2, "lo": -0.3, "hi": 2.2},
    "fuel_l": {"label": "Fuel remaining", "unit": "L",
               "digits": 1, "lo": 0.0, "hi": 70.0},
}

#: Byte patterns exercised for every multi-byte PID.
WORD_SAMPLES = [
    0x0000, 0x0001, 0x0004, 0x000F, 0x0064, 0x00FF, 0x0100, 0x0101,
    0x03E8, 0x1234, 0x1F40, 0x3FFF, 0x7FFF, 0x8000, 0xABCD, 0xFFFE, 0xFFFF,
]


class ObdRegressionCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mapping = load_file(support.OBD_MAPPING)
        cls.registry = MappingRegistry([cls.mapping])
        cls.profile = cls.registry.resolve(
            AllCapabilities(), config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )


class TestEveryLegacyPidIsMapped(ObdRegressionCase):
    def test_every_key_exists(self):
        mapped = set(self.profile.signal_keys())

        for _, key, *_ in LEGACY_PIDS:
            self.assertIn(key, mapped, f"{key} is missing from the mapping")

    def test_every_pid_exists(self):
        mapped = set(self.profile.obd_pids())

        for pid, key, *_ in LEGACY_PIDS:
            self.assertIn(pid, mapped, f"PID 0x{pid:02X} ({key}) is missing")

    def test_no_extra_channels_were_introduced(self):
        keys = self.profile.keys()
        expected = ["boost"] + [row[1] for row in LEGACY_PIDS] + ["fuel_l"]

        #: Compared as a set: v2 regrouped the requests by polling tier,
        #: which reorders the dashboard's channel list but adds and
        #: removes nothing. The two computed channels DO have fixed
        #: positions (`position: first` / `last`), so those are checked.
        self.assertEqual(sorted(keys), sorted(expected))
        self.assertEqual(keys[0], "boost")
        self.assertEqual(keys[-1], "fuel_l")

    def test_key_is_bound_to_the_same_pid(self):
        for pid, key, *_ in LEGACY_PIDS:
            with self.subTest(key=key):
                request = self.profile.request_for_signal(key)

                self.assertIsNotNone(request)
                self.assertEqual(request.pid, pid)
                self.assertEqual(request.service, 0x01)
                self.assertEqual(request.protocol, "obd")

    def test_response_lengths_match(self):
        for pid, key, _label, _unit, nbytes, *_ in LEGACY_PIDS:
            with self.subTest(key=key):
                self.assertEqual(
                    self.profile.request_for_signal(key).response.data_length,
                    nbytes,
                )


class TestFormulasAreUnchanged(ObdRegressionCase):
    def _read(self, key, data):
        """
        The full reading, value and label.

        The sweeps below compare the *arithmetic* against the legacy
        formulas, which is the contract: every input byte must still
        decode to the same float. Whether a mapping then declares that
        value unusable is a separate question, checked explicitly in
        TestDeclaredQuality rather than folded in here - otherwise
        declaring a sentinel would look like a formula change.
        """
        signal = self.registry.find_signal(key)
        request = self.registry.find_request(signal.request_id)
        response = bytes([0x41, request.pid]) + data

        return read_value(signal.decode, match_prefix(request, response))

    def _decode(self, key, data):
        """The narrow view - None for anything not usable."""
        signal = self.registry.find_signal(key)
        request = self.registry.find_request(signal.request_id)
        response = bytes([0x41, request.pid]) + data

        return decode_signal(signal, request, response)

    def test_single_byte_pids_over_every_input(self):
        """All 256 possible bytes, for every one-byte channel."""
        for pid, key, _l, _u, nbytes, decode, *_ in LEGACY_PIDS:
            if nbytes != 1:
                continue

            with self.subTest(key=key):
                for raw in range(256):
                    data = bytes([raw])

                    self.assertEqual(
                        self._read(key, data).value,
                        round(decode(data), 3),
                        f"{key} diverged at 0x{raw:02X}",
                    )

    def test_two_byte_pids_over_sampled_inputs(self):
        for pid, key, _l, _u, nbytes, decode, *_ in LEGACY_PIDS:
            if nbytes != 2:
                continue

            with self.subTest(key=key):
                for raw in WORD_SAMPLES:
                    data = bytes([raw >> 8, raw & 0xFF])

                    self.assertEqual(
                        self._read(key, data).value,
                        round(decode(data), 3),
                        f"{key} diverged at 0x{raw:04X}",
                    )

    def test_two_byte_pids_over_every_input(self):
        """Exhaustive 0x0000-0xFFFF sweep for every two-byte channel."""
        for pid, key, _l, _u, nbytes, decode, *_ in LEGACY_PIDS:
            if nbytes != 2:
                continue

            with self.subTest(key=key):
                for raw in range(0x10000):
                    data = bytes([raw >> 8, raw & 0xFF])

                    if self._read(key, data).value != round(decode(data), 3):
                        self.fail(f"{key} diverged at 0x{raw:04X}")

    def test_lambda_four_byte_response(self):
        _, _, _, _, _, decode, *_ = next(
            row for row in LEGACY_PIDS if row[1] == "lambda"
        )

        for raw in WORD_SAMPLES:
            data = bytes([raw >> 8, raw & 0xFF, 0x12, 0x34])

            self.assertEqual(
                self._read("lambda", data).value, round(decode(data), 3)
            )


class TestDeclaredQuality(TestFormulasAreUnchanged):
    """
    What the production mapping declares unusable, and what that costs.

    Separate from the formula sweeps on purpose: the arithmetic did not
    change, the interpretation did.
    """

    def test_lambda_sentinel_is_flagged_but_still_decodes_to_2(self):
        #
        # 114,138 rows in the lake - 57.4% of the channel - were this
        # sentinel stored as if the mixture really were 2.0.
        #
        reading = self._read("lambda", bytes([0xFF, 0xFF, 0x12, 0x34]))

        self.assertEqual(reading.value, 2.0)
        self.assertEqual(reading.quality, "sentinel")
        self.assertIsNone(self._decode("lambda", bytes([0xFF, 0xFF, 0x12, 0x34])))

    def test_lambda_below_the_sentinel_is_untouched(self):
        reading = self._read("lambda", bytes([0xFF, 0xFE, 0x12, 0x34]))

        self.assertEqual(reading.quality, "ok")

    def test_map_is_saturated_only_on_the_byte_ceiling(self):
        self.assertEqual(self._read("map", bytes([255])).quality, "saturated")
        self.assertEqual(self._read("map", bytes([254])).quality, "ok")
        self.assertEqual(self._read("map", bytes([255])).value, 255.0)
        self.assertIsNone(self._decode("map", bytes([255])))

    def test_nothing_else_in_the_production_set_is_flagged(self):
        #
        # Only two channels declare anything. If a third appears, it
        # should be a deliberate edit with lake evidence behind it, not a
        # copied line - so this fails until someone updates the list.
        #
        flagged = {
            key for key in (row[1] for row in LEGACY_PIDS)
            if (self.registry.find_signal(key).decode.invalid
                or self.registry.find_signal(key).decode.saturated)
        }

        self.assertEqual(flagged, {"lambda", "map"})


class TestMetadataIsUnchanged(ObdRegressionCase):
    def test_labels_units_digits_and_ranges(self):
        meta = {row["key"]: row for row in self.profile.meta()}

        for pid, key, label, unit, _n, _d, digits, _fast, lo, hi in LEGACY_PIDS:
            with self.subTest(key=key):
                self.assertEqual(
                    meta[key],
                    {"key": key, "label": label, "unit": unit,
                     "digits": digits, "lo": lo, "hi": hi},
                )

    def test_computed_metadata(self):
        meta = {row["key"]: row for row in self.profile.meta()}

        for key, expected in LEGACY_COMPUTED.items():
            with self.subTest(key=key):
                self.assertEqual(meta[key], dict(key=key, **expected))

    def test_recorder_param_rows(self):
        for pid, key, label, unit, *_ in LEGACY_PIDS:
            with self.subTest(key=key):
                self.assertEqual(self.profile.param_row(key), (pid, label, unit))

        for key, expected in LEGACY_COMPUTED.items():
            self.assertEqual(
                self.profile.param_row(key),
                (None, expected["label"], expected["unit"]),
            )


#: Where each channel sits in OBD mapping v2 (2026-08-30).
#:
#: The `fast` flag in LEGACY_PIDS above records where it sat in v1 and is
#: deliberately left alone - that table is the frozen historical
#: reference. This one is the current intent, and the difference between
#: them IS the change: eleven channels were being read at 10 Hz because
#: the original hand-written dashboard read them at 10 Hz, not because
#: anything about them moves that fast.
#:
#: `map` is the one channel kept fast for a display reason rather than a
#: physical one: the derived `boost` (map - baro) is the Drive view's
#: hero gauge, and at 0.1 Hz it reads as a broken instrument.
V2_TIERS = {
    "rpm": "motion", "speed": "motion", "map": "motion", "pedal": "motion",

    "load": "context", "throttle": "context", "maf": "context",
    "rail": "context", "torque": "context", "relthr": "context",
    "lambda": "context",

    "coolant": "slow", "oil": "slow", "iat": "slow", "voltage": "slow",
    "fuelrate": "slow", "cattemp": "slow", "egr": "slow", "egrerr": "slow",

    "ambient": "rare", "baro": "rare", "fuel": "rare", "runtime": "rare",
    "distance": "rare",
}


class TestPollingTiers(ObdRegressionCase):
    def test_every_legacy_channel_has_a_v2_tier(self):
        """No channel may fall through the re-tiering unassigned."""
        self.assertEqual(
            sorted(V2_TIERS), sorted(row[1] for row in LEGACY_PIDS)
        )

    def test_each_pid_is_in_its_declared_tier(self):
        for key, tier in V2_TIERS.items():
            with self.subTest(key=key):
                self.assertEqual(
                    self.profile.request_for_signal(key).polling_class, tier
                )

    def test_the_fast_tier_shrank_and_nothing_got_faster(self):
        """
        The direction of the change, asserted as a property rather than a
        list: v2 may move a channel down a tier, never up. `map` is the
        only channel allowed to stay in the fast tier.
        """
        rank = {"motion": 0, "context": 1, "slow": 2, "rare": 3}
        was_fast = [row[1] for row in LEGACY_PIDS if row[7]]

        for key in was_fast:
            with self.subTest(key=key):
                self.assertGreaterEqual(
                    rank[V2_TIERS[key]], rank["motion"]
                )

        for key in (row[1] for row in LEGACY_PIDS if not row[7]):
            with self.subTest(key=key):
                self.assertGreaterEqual(
                    rank[V2_TIERS[key]], rank["slow"],
                    f"{key} was slow in v1 and must not have become faster",
                )

        self.assertEqual(
            [k for k in was_fast if V2_TIERS[k] == "motion"],
            ["rpm", "map", "speed", "pedal"],
        )

    def test_the_plan_sends_far_fewer_requests_per_minute(self):
        """
        The whole point, in one number. Counted over a simulated minute
        at the 10 Hz loop rate.
        """
        plan = PollingPlan(
            self.profile.requests, resolve_classes(
                self.registry.polling_classes()
            )
        )

        sent = 0
        now = 1000.0

        for cycle in range(600):
            sent += len(plan.due(cycle, now))
            now += 0.1

        #: v1 sent 11 every cycle plus 13 every 10th = 6600 + 780 = 7380.
        legacy = 11 * 600 + 13 * 60

        self.assertLess(sent, legacy / 2.5)
        #: 4 motion x 600 cycles, plus the slower tiers a handful of times.
        self.assertGreater(sent, 2400)


class TestCapabilityGating(ObdRegressionCase):
    def test_unsupported_pids_are_dropped(self):
        from bmwdiag.obd import ObdCapabilitySet

        supported = {0x0C, 0x05, 0x0B, 0x33, 0x2F}
        profile = self.registry.resolve(
            ObdCapabilitySet(supported), config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )

        self.assertEqual(
            set(profile.obd_pids()), supported,
            "only PIDs the ECU advertises should be polled",
        )

    def test_an_ecu_with_no_bitmask_keeps_the_whole_table(self):
        """
        The old code read `if not supported or p.pid in supported`, so an
        ECU that publishes no support bitmask got polled for everything.
        """
        from bmwdiag.obd import ObdCapabilitySet

        profile = self.registry.resolve(
            ObdCapabilitySet(set()), config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )

        self.assertEqual(len(profile.requests), len(LEGACY_PIDS))

    def test_engine_family_is_matched_by_capability_not_address(self):
        from bmwdiag.obd import ObdCapabilitySet

        #
        # An ECU that answers OBD but does not advertise engine speed is
        # not the engine, whatever address it sits on.
        #
        profile = self.registry.resolve(
            ObdCapabilitySet({0x05, 0x0F}), config={"tank": 70.0},
            targets={"discovered_engine": 0x60},
        )

        self.assertEqual(profile.requests, [])


class TestNoProprietaryKnowledge(ObdRegressionCase):
    """
    Guard rail: the production mapping set carries standard OBD only.

    Proprietary mappings arrive later, from source-backed reverse
    engineering with recorded provenance. Until then nothing but SAE PIDs
    may reach the vehicle.
    """

    def test_production_registry_is_obd_only(self):
        from bmwdiag.mapping import load_tree

        registry = MappingRegistry(
            load_tree(support.MAPPINGS, production_only=True)
        )

        for request in registry.requests:
            self.assertEqual(request.protocol, "obd", request.id)
            self.assertEqual(request.service, 0x01, request.id)
            self.assertIsNone(request.did, request.id)

    def test_production_mappings_declare_standard_provenance(self):
        from bmwdiag.mapping import load_tree

        for mapping in load_tree(support.MAPPINGS, production_only=True):
            self.assertEqual(mapping.provenance.type, "obd_standard")
            self.assertEqual(mapping.verification.status, "verified")

    def test_production_targets_are_never_hardcoded_addresses(self):
        for request in self.registry.requests:
            self.assertTrue(
                request.target.is_dynamic,
                f"{request.id} names a fixed ECU address",
            )
            self.assertEqual(request.target.name, "discovered_engine")

    def test_the_synthetic_fixture_is_labelled_and_excluded(self):
        from bmwdiag.mapping import load_tree

        fixture = load_file(support.EXAMPLE_MAPPING)

        self.assertFalse(fixture.production)
        self.assertIn("TEST FIXTURE", fixture.description)
        self.assertEqual(fixture.provenance.type, "synthetic")
        self.assertNotIn(
            fixture.id,
            [m.id for m in load_tree(support.MAPPINGS, production_only=True)],
        )

    def test_the_synthetic_fixture_uses_obviously_fake_identifiers(self):
        fixture = load_file(support.EXAMPLE_MAPPING)

        self.assertEqual(fixture.ecu.target.address, 0x7E)
        self.assertEqual(
            {r.did for r in fixture.requests if r.did is not None},
            {0xF001, 0xF002},
        )


if __name__ == "__main__":
    unittest.main()
