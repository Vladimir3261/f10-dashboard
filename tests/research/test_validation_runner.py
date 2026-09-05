"""
The on-car validation runner - its safety properties, tested offline.

The runner itself needs a car; its two load-bearing guarantees do not.
This exercises the read-only allowlist (the single choke point every
frame passes through), the VIN redaction that keeps tracked artifacts
clean, and the frame log against a fake transport.
"""

import contextlib
import importlib.util
import io
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

            def request(self, payload, *, dst, timeout=None, expect=None):
                self.sent.append(payload)
                return b""

        spy = Spy()
        gated = vc.GatedTransport(spy, [])

        with self.assertRaises(vc.UnsafePayload):
            gated.request(hexb("2E F1 90 00"), dst=0x12)

        self.assertEqual(spy.sent, [])

    def test_gate_transport_records_a_negative_response_as_data(self):
        """An NRC is captured in the frame log as a number, not raised."""
        class Nrc:
            def request(self, payload, *, dst, timeout=None, expect=None):
                raise vc.live.HsfzNegativeResponse(0x22, 0x31, raw=hexb("7F 22 31"))

        log = []
        gated = vc.GatedTransport(Nrc(), log)
        out = gated.request(hexb("22 45 17"), dst=0x12)

        self.assertEqual(out, b"")
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["nrc"], 0x31)
        self.assertEqual(log[0]["nrc_hex"], "0x31")
        self.assertEqual(log[0]["nrc_name"], "requestOutOfRange")
        self.assertEqual(log[0]["service"], 0x22)
        self.assertEqual(log[0]["raw"], "7f 22 31")
        self.assertEqual(vc.nrc_text(log[0]), "NRC 0x31 requestOutOfRange (to 0x22)")

    def test_gate_transport_catches_the_negative_response_by_type(self):
        """Any NegativeResponse counts; any other fault still aborts."""
        from bmwdiag.protocol import NegativeResponse

        class Generic:
            def request(self, payload, *, dst, timeout=None, expect=None):
                raise NegativeResponse(0x22, 0x12)         # not an HsfzError

        log = []
        self.assertEqual(vc.GatedTransport(Generic(), log).request(
            hexb("22 45 17"), dst=0x12), b"")
        self.assertEqual(log[0]["nrc"], 0x12)
        self.assertEqual(log[0]["nrc_name"], "subFunctionNotSupported")

        class Prose:
            def request(self, payload, *, dst, timeout=None, expect=None):
                # The words are there; the type is not. Not a negative
                # response - the tool must not parse messages.
                raise vc.live.HsfzError("negative response to 0x22: NRC 0x31")

        with self.assertRaises(vc.live.HsfzError):
            vc.GatedTransport(Prose(), []).request(hexb("22 45 17"), dst=0x12)

    def test_old_artifacts_render_without_rewriting(self):
        """A pre-2026-09-05 frame carries the NRC as prose or null."""
        self.assertEqual(vc.nrc_text({"nrc": None}), "")
        self.assertEqual(vc.nrc_text({}), "")
        self.assertEqual(
            vc.nrc_text({"nrc": "negative response to 0x22: NRC 0x31"}),
            "negative response to 0x22: NRC 0x31",
        )
        self.assertEqual(vc.nrc_text({"nrc": 0x7F}), "NRC 0x7F serviceNotSupportedInActiveSession")
        self.assertEqual(vc.nrc_text({"nrc": 0x99}), "NRC 0x99 unknown")

    def test_run_one_records_the_nrc_numerically(self):
        request = self._d72_request("n47.d72.dyn.4517")

        class Client:
            def request(self, payload, timeout, dst, expect=None):
                if payload[0] == 0x22:
                    raise vc.live.HsfzNegativeResponse(0x22, 0x31, raw=hexb("7F 22 31"))
                return bytes([0x6C]) + bytes(payload[1:4])

        class Engine:
            addr = 0x12

            def label(self):
                return "0x12 (DDE)"

        with contextlib.redirect_stdout(io.StringIO()):
            result = vc._run_one(Client(), Engine(), request, decode_ok=True)

        self.assertEqual(result["outcome"], "negative_response")
        self.assertEqual(result["nrc"], 0x31)
        self.assertEqual(result["nrc_name"], "requestOutOfRange")
        self.assertEqual(result["service"], 0x22)
        self.assertEqual(result["raw"], "7f 22 31")

    def _d72_request(self, request_id):
        mapping = vc.load_file(os.path.join(
            support.ROOT, "mappings", "candidates", "bmw", "dde", "n47",
            "d72n47a0_dynamic.yaml"))
        for request in mapping.requests:
            if request.id == request_id:
                return request
        self.fail(request_id)


_KWP = os.path.join(support.ROOT, "mappings", "candidates", "bmw", "dde",
                    "n47", "dde7_kwp_local_id.yaml")
_D72 = os.path.join(support.ROOT, "mappings", "candidates", "bmw", "dde",
                    "n47", "d72n47a0_dynamic.yaml")


class Correlation(unittest.TestCase):
    """
    The tool polls MAPPED requests, so it must hand the transport the
    mapping's declared response shape - not leave the transport to
    derive the structural echo. The KWP local-identifier read is the
    case that breaks otherwise: `2C 10 04 06` is answered by `6C 10 ..`
    (no identifier echo), which the structural rule would discard as
    an orphan and the tool would report as "no answer".
    """

    class Spy:
        def __init__(self, answer=b""):
            self.calls = []
            self.answer = answer

        def request(self, payload, *, dst, timeout=None, expect=None):
            self.calls.append((bytes(payload), dst, expect))
            return self.answer

    def _request(self, path, request_id):
        mapping = vc.load_file(path)
        for request in mapping.requests:
            if request.id == request_id:
                return request
        self.fail(f"{request_id} not in {path}")

    def test_poll_value_passes_the_declared_expectation(self):
        request = self._request(_KWP, "n47.dde7.local.0406")
        spy = self.Spy(answer=hexb("6C 10 00 2A"))
        gated = vc.GatedTransport(spy, [])

        values = vc._poll_value(gated, request, 0x12)

        self.assertEqual(values, {"dde7_soot": 0.42})
        self.assertEqual(len(spy.calls), 1)
        payload, dst, expect = spy.calls[0]
        self.assertEqual(payload, hexb("2C 10 04 06"))
        self.assertEqual(dst, 0x12)
        self.assertIsNotNone(expect)
        self.assertEqual(expect.origin, "declared")
        self.assertEqual(expect.label, "n47.dde7.local.0406")
        # The declared shape accepts the non-echoing answer ...
        self.assertTrue(expect.matches_positive(hexb("6C 10 00 2A")))
        # ... which the structural rule would have thrown away.
        structural = vc.declared_response(hexb("2C 10 04 06"), b"", 0)
        self.assertFalse(structural.matches_positive(hexb("6C 10 00 2A")))

    def test_run_one_passes_the_declared_expectation_after_setup(self):
        """Setup frames stay structural (they echo); the poll is declared."""
        request = self._request(_D72, "n47.d72.dyn.4517")
        self.assertTrue(request.setup, "the d72 dynamic read has a setup")

        class Client:
            def __init__(self):
                self.calls = []

            def request(self, payload, timeout, dst, expect=None):
                self.calls.append((bytes(payload), dst, expect))
                return hexb("62 F3 03 39 08")

        class Engine:
            addr = 0x12

            def label(self):
                return "0x12 (DDE)"

        client = Client()
        with contextlib.redirect_stdout(io.StringIO()):
            result = vc._run_one(client, Engine(), request, decode_ok=True)

        self.assertEqual(result["outcome"], "decoded")
        polls = [c for c in client.calls if c[0] == hexb("22 F3 03")]
        self.assertEqual(len(polls), 1)
        _payload, _dst, expect = polls[0]
        self.assertEqual(expect.origin, "declared")
        self.assertEqual(expect.label, "n47.d72.dyn.4517")
        setups = [c for c in client.calls if c[0] != hexb("22 F3 03")]
        self.assertEqual(len(setups), len(request.setup))
        for _payload, _dst, expect in setups:
            self.assertIsNone(expect)


class IdentProbe(unittest.TestCase):
    def test_a_refused_ident_read_is_reported_with_its_nrc(self):
        class Client:
            def request(self, payload, timeout=None, dst=None, expect=None):
                if payload[0] == 0x22:
                    raise vc.live.HsfzNegativeResponse(0x22, 0x31)
                raise TimeoutError("nothing")

        with contextlib.redirect_stdout(io.StringIO()):
            out = vc.read_ident(Client(), 0x12)

        self.assertEqual(out["hw_f191"], "(NRC 0x31 requestOutOfRange (to 0x22))")
        self.assertEqual(out["ecu_name_0900"], "(no answer)")


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
