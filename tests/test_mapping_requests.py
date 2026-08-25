"""
Request building, transport dispatch and derived signals.

No socket, no gateway, no vehicle: the transport is a dictionary, which
is the whole reason DiagnosticTransport exists as a separate interface.
"""

import unittest

from . import support
from bmwdiag.mapping import MappingExecutor, load_file
from bmwdiag.mapping.errors import MappingError
from bmwdiag.mapping.registry import AllCapabilities, MappingRegistry
from bmwdiag.protocol.request import build_payload, build_request
from tests.support import hexb


class FakeTransport:
    """A DiagnosticTransport backed by a lookup table."""

    def __init__(self, responses):
        self.responses = dict(responses)
        self.sent = []

    def request(self, payload, *, dst, timeout=None):
        self.sent.append((bytes(payload), dst, timeout))
        key = (bytes(payload), dst)

        if key in self.responses:
            return self.responses[key]

        if bytes(payload) in self.responses:
            return self.responses[bytes(payload)]

        raise KeyError(f"no canned response for {payload.hex(' ')} -> 0x{dst:02X}")


class FakeObdReader:
    """An ObdPidReader backed by a PID -> data-bytes table."""

    def __init__(self, data):
        self.data = dict(data)
        self.calls = []

    def read(self, pids):
        self.calls.append(list(pids))

        return {pid: self.data[pid] for pid in pids if pid in self.data}


class TestPayloadBuilding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.obd = MappingRegistry([load_file(support.OBD_MAPPING)])
        cls.example = MappingRegistry([load_file(support.EXAMPLE_MAPPING)])

    def test_obd_mode01_payload(self):
        request = self.obd.find_request("obd.mode01.0C")

        self.assertEqual(build_payload(request), hexb("01 0C"))

    def test_uds_read_data_by_identifier_payload(self):
        request = self.example.find_request("example.uds.block")

        self.assertEqual(build_payload(request), hexb("22 F0 01"))

    def test_arbitrary_raw_job_payload(self):
        """A proprietary-shaped job needs no service/identifier convention."""
        request = self.example.find_request("example.raw.job")

        self.assertEqual(build_payload(request), hexb("31 01 AB CD"))

    def test_dynamic_target_is_resolved_at_bind_time(self):
        request = self.obd.find_request("obd.mode01.0C")

        self.assertTrue(request.target.is_dynamic)
        self.assertEqual(request.target.name, "discovered_engine")

        bound = build_request(request, {"discovered_engine": 0x12})

        self.assertEqual(bound.dst, 0x12)
        self.assertEqual(bound.payload, hexb("01 0C"))
        self.assertEqual(bound.expect_prefix, hexb("41 0C"))
        self.assertEqual(bound.min_length, 4)

    def test_unresolved_dynamic_target_fails_loudly(self):
        request = self.obd.find_request("obd.mode01.0C")

        with self.assertRaises(MappingError):
            build_request(request, {})

    def test_fixed_target_needs_no_resolution(self):
        request = self.example.find_request("example.uds.block")

        self.assertEqual(build_request(request, {}).dst, 0x7E)


class TestObdExecution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = MappingRegistry([load_file(support.OBD_MAPPING)])

    def profile(self, **kwargs):
        return self.registry.resolve(
            AllCapabilities(),
            targets={"discovered_engine": 0x12},
            **kwargs,
        )

    def test_decodes_several_pids_in_one_read(self):
        profile = self.profile()
        reader = FakeObdReader({
            0x0C: hexb("0C 3C"),
            0x05: hexb("5B"),
            0x0B: hexb("64"),
        })
        executor = MappingExecutor(profile, obd_reader=reader)
        requests = [
            profile.request("obd.mode01.0C"),
            profile.request("obd.mode01.05"),
            profile.request("obd.mode01.0B"),
        ]

        values = executor.execute(requests)

        self.assertEqual(values, {"rpm": 783.0, "coolant": 51.0, "map": 100.0})
        self.assertEqual(reader.calls, [[0x0C, 0x05, 0x0B]])

    def test_a_pid_the_ecu_did_not_answer_simply_yields_nothing(self):
        profile = self.profile()
        reader = FakeObdReader({0x0C: hexb("0C 3C")})
        executor = MappingExecutor(profile, obd_reader=reader)

        values = executor.execute([
            profile.request("obd.mode01.0C"), profile.request("obd.mode01.05"),
        ])

        self.assertEqual(values, {"rpm": 783.0})

    def test_a_garbled_reply_costs_one_channel_not_the_loop(self):
        profile = self.profile()
        reader = FakeObdReader({0x0C: b"\x0C", 0x05: hexb("5B")})
        seen = []
        executor = MappingExecutor(
            profile, obd_reader=reader,
            on_error=lambda rid, exc: seen.append(rid),
        )

        values = executor.execute([
            profile.request("obd.mode01.0C"), profile.request("obd.mode01.05"),
        ])

        self.assertEqual(values, {"coolant": 51.0})
        self.assertEqual(seen, ["obd.mode01.0C"])

    def test_pid_lengths_come_from_the_mapping(self):
        lengths = self.profile().obd_pid_lengths()

        self.assertEqual(lengths[0x0C], 2)
        self.assertEqual(lengths[0x05], 1)
        self.assertEqual(lengths[0x24], 4)


class TestGenericExecution(unittest.TestCase):
    """
    TEST FIXTURE mappings only - synthetic service 0x22 and a raw job.

    Proves the executor reaches a UDS or proprietary request without the
    decoder architecture changing, and without assuming UDS at all.
    """

    @classmethod
    def setUpClass(cls):
        cls.registry = MappingRegistry([load_file(support.EXAMPLE_MAPPING)])
        cls.profile = cls.registry.resolve(AllCapabilities())

    def test_uds_request_round_trip(self):
        transport = FakeTransport({
            (hexb("22 F0 01"), 0x7E): hexb("62 F0 01 04 B0 FF 38 07 00"),
        })
        executor = MappingExecutor(self.profile, transport=transport)

        values = executor.execute([self.profile.request("example.uds.block")])

        self.assertEqual(values["example_speed"], 120.0)
        self.assertEqual(values["example_temp"], -100.0)
        self.assertEqual(values["example_gear"], 7.0)
        self.assertEqual(transport.sent[0][1], 0x7E)

    def test_raw_job_round_trip(self):
        transport = FakeTransport({
            (hexb("31 01 AB CD"), 0x7E): hexb("71 01 AB CD 01 08 40 7F"),
        })
        executor = MappingExecutor(self.profile, transport=transport)

        values = executor.execute([self.profile.request("example.raw.job")])

        self.assertEqual(values["example_state"], "running")
        self.assertEqual(values["example_flag"], 1.0)

    def test_wrong_prefix_is_rejected_not_decoded(self):
        transport = FakeTransport({
            (hexb("22 F0 01"), 0x7E): hexb("62 F0 09 04 B0 FF 38 07 00"),
        })
        errors = []
        executor = MappingExecutor(
            self.profile, transport=transport,
            on_error=lambda rid, exc: errors.append(exc),
        )

        self.assertEqual(
            executor.execute([self.profile.request("example.uds.block")]), {}
        )
        self.assertEqual(len(errors), 1)

    def test_mixed_protocols_in_one_pass(self):
        obd = MappingRegistry([load_file(support.OBD_MAPPING)])
        merged = MappingRegistry(
            obd.mappings + [load_file(support.EXAMPLE_MAPPING)]
        )
        profile = merged.resolve(
            AllCapabilities(), targets={"discovered_engine": 0x12}
        )
        executor = MappingExecutor(
            profile,
            obd_reader=FakeObdReader({0x0C: hexb("0C 3C")}),
            transport=FakeTransport({
                (hexb("22 F0 01"), 0x7E): hexb("62 F0 01 04 B0 FF 38 07 00"),
            }),
        )

        values = executor.execute([
            profile.request("obd.mode01.0C"),
            profile.request("example.uds.block"),
        ])

        self.assertEqual(values["rpm"], 783.0)
        self.assertEqual(values["example_speed"], 120.0)


class TestDerivedSignals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = MappingRegistry([load_file(support.OBD_MAPPING)])

    def profile(self, tank=70.0):
        return cls_profile(self.registry, tank)

    def test_boost_matches_the_historical_formula(self):
        profile = self.profile()
        values = {"map": 158.0, "baro": 99.0}

        derived = profile.apply_derived(values, {"map": 158.0})

        self.assertEqual(derived["boost"], round((158.0 - 99.0) / 100.0, 3))
        self.assertEqual(derived["boost"], 0.59)

    def test_boost_falls_back_to_100_kpa_before_the_first_barometric_read(self):
        profile = self.profile()

        derived = profile.apply_derived({"map": 158.0}, {"map": 158.0})

        self.assertEqual(derived["boost"], round((158.0 - 100.0) / 100.0, 3))

    def test_boost_uses_the_carried_forward_barometric_value(self):
        """baro is a slow channel; boost must not wait for it to be fresh."""
        profile = self.profile()
        values = {"map": 110.0, "baro": 95.0}

        derived = profile.apply_derived(values, {"map": 110.0})

        self.assertEqual(derived["boost"], 0.15)

    def test_boost_only_recomputes_when_manifold_pressure_is_fresh(self):
        profile = self.profile()
        values = {"map": 158.0, "baro": 99.0}

        self.assertEqual(profile.apply_derived(values, {"baro": 99.0}), {})

    def test_fuel_litres_use_the_configured_tank_capacity(self):
        for tank in (45.0, 63.0, 70.0, 82.5):
            with self.subTest(tank=tank):
                profile = self.profile(tank)

                derived = profile.apply_derived({"fuel": 63.0}, {"fuel": 63.0})

                self.assertEqual(
                    derived["fuel_l"], round(63.0 / 100.0 * tank, 2)
                )

    def test_fuel_litres_display_range_tracks_the_tank(self):
        meta = {m["key"]: m for m in self.profile(63.0).meta()}

        self.assertEqual(meta["fuel_l"]["hi"], 63.0)
        self.assertEqual(meta["fuel_l"]["lo"], 0.0)

    def test_derived_channel_is_dropped_when_its_input_is_unavailable(self):
        from bmwdiag.obd import ObdCapabilitySet

        #
        # An ECU that advertises rpm but not manifold pressure or fuel
        # level should not be offered boost or fuel litres at all.
        #
        profile = self.registry.resolve(
            ObdCapabilitySet({0x0C, 0x05}),
            config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )

        self.assertEqual([d.key for d in profile.derived], [])
        self.assertEqual(profile.keys(), ["rpm", "coolant"])

    def test_boost_survives_without_barometric_support(self):
        from bmwdiag.obd import ObdCapabilitySet

        profile = self.registry.resolve(
            ObdCapabilitySet({0x0C, 0x0B}),
            config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )

        self.assertEqual([d.key for d in profile.derived], ["boost"])
        self.assertEqual(profile.keys(), ["boost", "rpm", "map"])

    def test_recorder_metadata_rows(self):
        profile = self.profile()

        self.assertEqual(profile.param_row("rpm"), (0x0C, "Engine speed", "rpm"))
        self.assertEqual(profile.param_row("coolant"), (0x05, "Coolant temp", "°C"))
        #
        # A derived channel has no PID; the column stays NULL.
        #
        self.assertEqual(profile.param_row("boost"), (None, "Turbo boost", "bar"))
        self.assertEqual(profile.param_row("fuel_l"),
                         (None, "Fuel remaining", "L"))
        self.assertEqual(profile.param_row("nonexistent"), (None, "nonexistent", ""))

    def test_non_obd_signals_record_a_null_pid(self):
        registry = MappingRegistry([load_file(support.EXAMPLE_MAPPING)])
        profile = registry.resolve(AllCapabilities())

        self.assertEqual(profile.param_row("example_speed")[0], None)


def cls_profile(registry, tank):
    return registry.resolve(
        AllCapabilities(),
        config={"tank": tank},
        targets={"discovered_engine": 0x12},
    )


if __name__ == "__main__":
    unittest.main()
