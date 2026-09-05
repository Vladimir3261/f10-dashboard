"""
The on-car validation runner - its safety properties, tested offline.

The runner itself needs a car; its two load-bearing guarantees do not.
This exercises the read-only allowlist (the single choke point every
frame passes through), the VIN redaction that keeps tracked artifacts
clean, and the frame log against a fake transport.
"""

import importlib.util
import os
import unittest

from tests import support  # noqa: F401
from tests.support import hexb

_VC = os.path.join(support.ROOT, "tools", "validate_candidate.py")
_spec = importlib.util.spec_from_file_location("validate_candidate", _VC)
vc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vc)


class ReadOnlyGate(unittest.TestCase):
    def test_every_read_service_passes(self):
        for frame in ("01 00", "09 0A", "22 F1 90", "22 F3 03",
                      "19 02 FF", "3E 00"):
            vc.assert_read_only(hexb(frame))   # must not raise

    def test_dynamic_define_and_clear_pass(self):
        for frame in ("2C 03 F3 03", "2C 01 F3 03 45 17 01 02",
                      "2C 02 F3 03 00 10 20 02", "2C 10 04 06"):
            vc.assert_read_only(hexb(frame))

    def test_write_and_control_services_are_blocked(self):
        for frame, _name in [
            ("2E F1 90 00", "WriteDataByIdentifier"),
            ("2F 10 03 00", "IOControl"),
            ("31 01 AB CD", "RoutineControl"),
            ("14 FF FF FF", "ClearDTC"),
            ("27 01", "SecurityAccess"),
            ("10 03", "SessionControl"),
            ("11 01", "ECUReset"),
            ("34 00 00", "RequestDownload"),
            ("28 00 01", "CommunicationControl"),
            ("85 02", "ControlDTCSetting"),
        ]:
            with self.assertRaises(vc.UnsafePayload, msg=frame):
                vc.assert_read_only(hexb(frame))

    def test_dynamic_define_with_a_write_subfunction_is_blocked(self):
        """0x2C is allowed only with define/clear/read subfunctions."""
        with self.assertRaises(vc.UnsafePayload):
            vc.assert_read_only(hexb("2C 04 F3 03"))   # not a read subfn

    def test_empty_payload_is_blocked(self):
        with self.assertRaises(vc.UnsafePayload):
            vc.assert_read_only(b"")

    def test_gate_transport_blocks_before_sending(self):
        """A blocked frame never reaches the inner transport."""
        class Spy:
            sent = []

            def request(self, payload, *, dst, timeout=None):
                self.sent.append(payload)
                return b""

        spy = Spy()
        gated = vc.GatedTransport(spy, [])

        with self.assertRaises(vc.UnsafePayload):
            gated.request(hexb("2E F1 90 00"), dst=0x12)

        self.assertEqual(spy.sent, [])

    def test_gate_transport_records_a_negative_response_as_data(self):
        """An NRC is captured in the frame log, not raised."""
        from bmwdiag.protocol.errors import NegativeResponse

        class Nrc:
            def request(self, payload, *, dst, timeout=None):
                raise NegativeResponse(0x22, 0x31, hexb("7F 22 31"), 0x12)

        log = []
        gated = vc.GatedTransport(Nrc(), log)
        out = gated.request(hexb("22 45 17"), dst=0x12)

        self.assertEqual(out, b"")
        self.assertEqual(len(log), 1)
        #: Numerically, with the readable forms beside it - never the
        #: exception's sentence.
        self.assertEqual(log[0]["nrc"], 0x31)
        self.assertEqual(log[0]["nrc_hex"], "0x31")
        self.assertEqual(log[0]["nrc_name"], "requestOutOfRange")
        self.assertEqual(log[0]["service"], "0x22")


class Redaction(unittest.TestCase):
    def test_vin_bearing_keys_are_masked(self):
        red = vc._redact({"vin": "WBAFAKE00000TEST0", "ecu": "0x12"})
        self.assertNotIn("WBAFW", red["vin"])
        self.assertEqual(red["ecu"], "0x12")

    def test_vin_shaped_values_anywhere_are_masked(self):
        red = vc._redact({"note": "car WBAFAKE00000TEST0 responded"})
        self.assertNotIn("WBAFAKE00000TEST0", red["note"])

    def test_ordinary_hex_is_not_over_redacted(self):
        red = vc._redact({"tx": "2c 01 f3 03 45 17 01 02", "ms": 6.1})
        self.assertEqual(red["tx"], "2c 01 f3 03 45 17 01 02")
        self.assertEqual(red["ms"], 6.1)


class Artifacts(unittest.TestCase):
    def test_write_produces_tracked_and_raw_sets(self):
        import json
        import tempfile

        art = vc.RunArtifacts("run")
        art.set_environment(gateway="169.254.1.1", ecu="0x12 (DDE)",
                            supported_pid_count=42)
        art.add({
            "kind": "run", "request": "n47.d72.dyn.4517", "ecu_addr": "0x12",
            "outcome": "decoded", "signals": {"n47d_oil_temp": 46.0},
            "plausibility_note": "oil ~ coolant when cold?",
            "frames": [
                {"dst": "0x12", "tx": "2c 03 f3 03", "rx": "6c 03 f3 03",
                 "nrc": None, "ms": 5.0},
                {"dst": "0x12", "tx": "22 f3 03", "rx": "62 f3 03 39 08",
                 "nrc": None, "ms": 6.1},
            ],
        })

        with tempfile.TemporaryDirectory() as tmp:
            orig_tracked, orig_raw = vc.TRACKED_RUNS, vc.RAW_RUNS
            vc.TRACKED_RUNS = os.path.join(tmp, "validation-runs")
            vc.RAW_RUNS = os.path.join(tmp, "raw")

            try:
                tracked, raw = art.write()
            finally:
                vc.TRACKED_RUNS, vc.RAW_RUNS = orig_tracked, orig_raw

            tdir = os.path.join(tmp, "validation-runs", art.slug)
            for name in ("run.json", "summary.md", "frames.ndjson"):
                self.assertTrue(os.path.isfile(os.path.join(tdir, name)), name)

            with open(os.path.join(tdir, "summary.md")) as fh:
                summary = fh.read()

            self.assertIn("n47.d72.dyn.4517", summary)
            self.assertIn("46.0", summary)
            self.assertIn("62 f3 03 39 08", summary)

            with open(os.path.join(tdir, "run.json")) as fh:
                data = json.load(fh)

            self.assertTrue(data["meta"]["read_only"])
            self.assertEqual(len(data["records"][0]["frames"]), 2)


if __name__ == "__main__":
    unittest.main()
