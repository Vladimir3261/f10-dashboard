"""
End-to-end wiring of live.py onto the mapping subsystem.

No socket and no vehicle: a fake HSFZ client answers Mode 01 requests
from a table of raw bytes. This covers the seams the refactor moved -
ObdSession's PID lengths, the executor, derived signals, telemetry
metadata and the recorder's params table.
"""

import os
import sqlite3
import tempfile
import time
import unittest

from . import support

import live
from bmwdiag.mapping import MappingExecutor, PollingPlan
from bmwdiag.mapping.registry import AllCapabilities
from bmwdiag.obd import ObdCapabilitySet


class FakeHsfzClient:
    """Answers OBD Mode 01, single or multi-PID, from canned data."""

    def __init__(self, data, multi=True):
        self.data = dict(data)
        self.multi = multi
        self.requests = []

    def request(self, payload, timeout=None, dst=None, expect_src=None):
        self.requests.append(bytes(payload))

        if payload[0] != 0x01:
            raise live.HsfzError("only mode 01 is faked")

        pids = list(payload[1:])

        if len(pids) > 1 and not self.multi:
            raise live.HsfzError("this ECU answers one PID at a time")

        out = bytearray([0x41])

        for pid in pids:
            if pid not in self.data:
                raise live.HsfzError(f"no data for PID 0x{pid:02X}")

            out.append(pid)
            out.extend(self.data[pid])

        return bytes(out)

    request_safe = request


class Args:
    """Just the attributes the mapping wiring reads off argparse."""

    tank = 70.0
    rate = 10.0
    slow_every = 10
    mappings = support.MAPPINGS


SAMPLE = {
    0x0C: b"\x0c\x3c",      # 783 rpm
    0x0B: b"\x9e",          # 158 kPa manifold
    0x05: b"\x83",          # 91 degC coolant
    0x33: b"\x63",          # 99 kPa barometric
    0x2F: b"\xa0",          # 62.745 % fuel
    0x42: b"\x37\x24",      # 14.116 V
}


class LiveWiringCase(unittest.TestCase):
    def build(self, supported=None, multi=True, data=None):
        registry = live.load_registry(Args.mappings)
        caps = (
            ObdCapabilitySet(supported) if supported is not None
            else AllCapabilities()
        )
        profile = registry.resolve(
            caps, config={"tank": Args.tank},
            targets={"discovered_engine": 0x12},
        )
        client = FakeHsfzClient(data if data is not None else SAMPLE, multi=multi)
        session = live.ObdSession(client, profile.obd_pid_lengths())
        plan = PollingPlan(profile.requests, live.polling_classes(registry, Args()))
        executor = MappingExecutor(
            profile,
            transport=live.HsfzTransport(client),
            obd_reader=session,
        )

        return registry, profile, client, session, plan, executor


class TestRegistryLoading(LiveWiringCase):
    def test_default_mapping_dir_is_the_shipped_one(self):
        self.assertTrue(os.path.isdir(live.DEFAULT_MAPPING_DIR))

    def test_production_load_excludes_the_fixture(self):
        registry = live.load_registry(Args.mappings)

        self.assertEqual([m.id for m in registry.mappings], ["sae-obd-engine"])
        self.assertEqual(len(registry.requests), 24)

    def test_no_pid_table_remains_in_live(self):
        for name in ("PIDS", "PID_BY_NUM", "PID_LEN", "COMPUTED", "Pid"):
            self.assertFalse(
                hasattr(live, name),
                f"live.{name} should have moved into the mapping layer",
            )


class TestObdSession(LiveWiringCase):
    def test_pid_lengths_come_from_the_registry(self):
        _, profile, _, session, _, _ = self.build()

        self.assertEqual(session.pid_len[0x0C], 2)
        self.assertEqual(session.pid_len[0x24], 4)
        #
        # The support-bitmask PIDs are protocol structure, contributed by
        # the OBD layer rather than by any mapping file.
        #
        self.assertEqual(session.pid_len[0x00], 4)
        self.assertEqual(session.pid_len[0x20], 4)

    def test_multi_pid_response_is_walked(self):
        _, _, _, session, _, _ = self.build()

        got = session.read([0x0C, 0x0B, 0x05])

        self.assertEqual(got, {0x0C: b"\x0c\x3c", 0x0B: b"\x9e", 0x05: b"\x83"})

    def test_falls_back_to_single_pid_reads(self):
        _, _, client, session, _, _ = self.build(multi=False)

        got = session.read([0x0C, 0x0B])

        self.assertEqual(got, {0x0C: b"\x0c\x3c", 0x0B: b"\x9e"})
        self.assertFalse(session.multi_ok)


class TestPollCycle(LiveWiringCase):
    def cycle(self, executor, profile, plan, values, cycle_no):
        """One iteration of the loop body, exactly as poll_loop runs it."""
        fresh = executor.execute(plan.due(cycle_no, time.monotonic()))
        values.update(fresh)

        derived = profile.apply_derived(values, fresh)
        values.update(derived)
        fresh.update(derived)

        return fresh

    def test_first_cycle_reads_everything_available(self):
        _, profile, _, _, plan, executor = self.build(supported=set(SAMPLE))
        values = {}

        fresh = self.cycle(executor, profile, plan, values, 0)

        self.assertEqual(fresh["rpm"], 783.0)
        self.assertEqual(fresh["map"], 158.0)
        self.assertEqual(fresh["coolant"], 91.0)
        self.assertEqual(fresh["baro"], 99.0)
        self.assertEqual(fresh["boost"], 0.59)
        self.assertEqual(fresh["fuel_l"], round(62.745 / 100.0 * 70.0, 2))

    def test_slow_channels_are_skipped_between_slow_cycles(self):
        _, profile, _, _, plan, executor = self.build(supported=set(SAMPLE))
        values = {}

        self.cycle(executor, profile, plan, values, 0)
        fresh = self.cycle(executor, profile, plan, values, 1)

        self.assertIn("rpm", fresh)
        self.assertIn("map", fresh)
        self.assertIn("boost", fresh)
        #
        # Coolant is slow; its cached value stays in `values` but is not
        # re-reported as fresh, so nothing carried-forward is re-logged.
        #
        self.assertNotIn("coolant", fresh)
        self.assertEqual(values["coolant"], 91.0)
        self.assertNotIn("fuel_l", fresh)

    def test_boost_uses_the_carried_forward_barometric_reading(self):
        _, profile, _, _, plan, executor = self.build(supported=set(SAMPLE))
        values = {}

        self.cycle(executor, profile, plan, values, 0)
        fresh = self.cycle(executor, profile, plan, values, 3)

        self.assertEqual(fresh["boost"], round((158.0 - 99.0) / 100.0, 3))

    def test_adding_a_request_needs_no_poll_loop_change(self):
        """
        A new diagnostic request is a mapping entry, not code.

        The loop body above is untouched; only the registry differs.
        """
        from bmwdiag.mapping import load_file, load_text
        from bmwdiag.mapping.registry import MappingRegistry

        extra = load_text("""
schema_version: 1
mapping:
  id: extra-test-channel
ecu:
  family: engine
  target: discovered_engine
  match:
    capability:
      obd_mode01_pid: 0x0C
requests:
  obd.mode01.0E:
    protocol: obd
    pid: 0x0E
    polling: {class: fast}
    response: {data_length: 1}
    signals:
      timing:
        label: Timing advance
        unit: deg
        display: {digits: 1, min: -64, max: 64}
        decode: {type: uint8, pre_add: -128.0, divide: 2.0}
""", "extra.yaml")

        registry = MappingRegistry([load_file(support.OBD_MAPPING), extra])
        profile = registry.resolve(
            ObdCapabilitySet(set(SAMPLE) | {0x0E}),
            config={"tank": 70.0}, targets={"discovered_engine": 0x12},
        )
        data = dict(SAMPLE)
        data[0x0E] = b"\xa0"
        client = FakeHsfzClient(data)
        executor = MappingExecutor(
            profile,
            transport=live.HsfzTransport(client),
            obd_reader=live.ObdSession(client, profile.obd_pid_lengths()),
        )
        plan = PollingPlan(profile.requests, live.polling_classes(registry, Args()))

        fresh = self.cycle(executor, profile, plan, {}, 0)

        self.assertEqual(fresh["timing"], (0xA0 - 128.0) / 2.0)
        self.assertEqual(fresh["rpm"], 783.0)


class TestTelemetryMetadata(LiveWiringCase):
    def test_set_meta_takes_registry_rows(self):
        _, profile, _, _, _, _ = self.build()
        tel = live.Telemetry()

        tel.set_meta(profile.meta())

        self.assertEqual(tel.meta_version, 1)
        self.assertEqual([m["key"] for m in tel.meta], profile.keys())
        self.assertEqual(
            set(tel.meta[0]), {"key", "label", "unit", "digits", "lo", "hi"}
        )

    def test_meta_shape_is_identical_for_obd_and_synthetic_channels(self):
        """The dashboard must not be able to tell where a channel came from."""
        from bmwdiag.mapping import load_tree
        from bmwdiag.mapping.registry import MappingRegistry

        profile = MappingRegistry(load_tree(support.MAPPINGS)).resolve(
            AllCapabilities(), config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )
        rows = {m["key"]: m for m in profile.meta()}

        self.assertEqual(set(rows["rpm"]), set(rows["example_speed"]))
        self.assertEqual(set(rows["rpm"]), set(rows["boost"]))


class TestRecorderCompatibility(LiveWiringCase):
    def test_params_table_keeps_its_columns_and_values(self):
        _, profile, _, _, _, _ = self.build()
        path = os.path.join(tempfile.mkdtemp(), "telemetry.db")
        rec = live.Recorder(path, chunk=1, interval=0.05)
        rec.open()

        try:
            rec.set_metadata(profile)
            rec.start_run("TESTVIN", "127.0.0.1", "0x12", 0x12)
            rec.write(time.time(), {"rpm": 783.0, "boost": 0.59})

            deadline = time.monotonic() + 5.0

            while rec.rows < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            rec.close()

        db = sqlite3.connect(path)

        try:
            columns = [
                row[1] for row in db.execute("PRAGMA table_info(params)")
            ]
            rows = dict(
                (r[0], r[1:]) for r in
                db.execute("SELECT key, pid, label, unit FROM params")
            )
            samples = db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        finally:
            db.close()

        self.assertEqual(columns, ["id", "key", "pid", "label", "unit"])
        self.assertEqual(rows["rpm"], (0x0C, "Engine speed", "rpm"))
        #
        # A derived channel has no PID; the column must be NULL, not 0.
        #
        self.assertEqual(rows["boost"], (None, "Turbo boost", "bar"))
        self.assertEqual(samples, 2)

    def test_numeric_only_filters_non_numeric_channels(self):
        values = {"rpm": 783.0, "runtime": 12, "state": "running",
                  "flag": True, "raw": b"\x01"}

        self.assertEqual(live.numeric_only(values), {"rpm": 783.0, "runtime": 12})


class TestPollingClassWiring(LiveWiringCase):
    def test_slow_every_reaches_the_plan(self):
        registry = live.load_registry(Args.mappings)

        class Slower(Args):
            slow_every = 4

        classes = live.polling_classes(registry, Slower())

        self.assertEqual(classes["slow"].value, 4.0)
        self.assertEqual(classes["fast"].value, 1.0)

    def test_ecu_scoring_uses_the_mapped_pids(self):
        registry = live.load_registry(Args.mappings)
        engine = live.EcuInfo(addr=0x12, supported={0x0C, 0x05, 0x0B})
        other = live.EcuInfo(addr=0x60, supported={0x0C})

        self.assertTrue(engine.is_engine)
        self.assertGreater(
            engine.score(registry.obd_pids()), other.score(registry.obd_pids())
        )

    def test_ecu_capabilities_feed_the_registry(self):
        registry = live.load_registry(Args.mappings)
        engine = live.EcuInfo(addr=0x12, supported={0x0C, 0x05})

        profile = registry.resolve(
            engine.capabilities(), config={"tank": 70.0},
            targets={"discovered_engine": engine.addr},
        )

        self.assertEqual(profile.signal_keys(), ["rpm", "coolant"])


class TestObdCapabilityDiscovery(unittest.TestCase):
    """Bitmask traversal stays out of the generic mapping layer."""

    def test_read_supported_walks_the_blocks(self):
        responses = {
            b"\x01\x00": bytes([0x41, 0x00, 0x18, 0x3B, 0x80, 0x11]),
            b"\x01\x20": bytes([0x41, 0x20, 0x80, 0x00, 0x00, 0x00]),
        }

        class Client:
            def request_safe(self, payload, timeout=None, dst=None):
                if bytes(payload) not in responses:
                    raise live.HsfzError("no answer")

                return responses[bytes(payload)]

        supported = live.read_supported(Client(), 0x12, 0.3)

        self.assertIn(0x04, supported)
        self.assertIn(0x05, supported)
        self.assertIn(0x0C, supported)
        self.assertIn(0x21, supported)
        self.assertNotIn(0x02, supported)

    def test_walk_stops_when_the_ecu_stops_answering(self):
        class Client:
            def request_safe(self, payload, timeout=None, dst=None):
                raise live.HsfzError("nobody home")

        self.assertEqual(live.read_supported(Client(), 0x12, 0.3), set())


if __name__ == "__main__":
    unittest.main()
