"""
The on-demand fault-memory path (issue #15, domain 7): read-only, twice.

`bmwdiag.dtc` builds 0x19 payloads and parses 0x59 replies; `tools/dtc.py`
drives them through the same request function the validation tool uses.
What these tests pin:

  * every builder emits service 0x19 and passes the observational gate;
  * the gate refuses 0x14 ClearDiagnosticInformation, so nothing that
    imports this module could clear the memory even by mistake;
  * the tool's readout records ONLY 0x19 frames, including with --detail;
  * a truncated DTC list is an error, never "fewer faults";
  * the two tools share ONE definition of the status-byte semantics.

No car, no network: a fake request function answers from a script.
"""

import importlib.util
import os
import unittest

from tests import support  # noqa: F401
from tests.support import hexb

from bmwdiag import dtc as udsdtc
from bmwdiag.protocol.safety import (
    OBSERVATIONAL_SERVICES,
    WRITE_SERVICES,
    UnsafePayload,
    assert_observational,
)


def load_tool(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(support.ROOT, "tools", f"{name}.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


class TheBuildersAreReads(unittest.TestCase):
    BUILDERS = (
        ("report_by_status_mask", (0x0C,), "19 02 0C"),
        ("report_number_by_status_mask", (0xFF,), "19 01 FF"),
        ("snapshot_by_dtc", (0x123456,), "19 04 12 34 56 FF"),
        ("snapshot_by_dtc", (0x123456, 1), "19 04 12 34 56 01"),
        ("extended_data_by_dtc", (0xABCDEF,), "19 06 AB CD EF FF"),
        ("supported_dtcs", (), "19 0A"),
    )

    def test_every_builder_emits_0x19_and_passes_the_gate(self):
        for name, args, expected in self.BUILDERS:
            with self.subTest(builder=name, args=args):
                payload = getattr(udsdtc, name)(*args)

                self.assertEqual(payload, hexb(expected))
                self.assertEqual(payload[0], udsdtc.SERVICE)
                self.assertIn(payload[0], OBSERVATIONAL_SERVICES)
                assert_observational(payload)   # would raise

    def test_the_gate_refuses_a_clear(self):
        """
        0x14 is on the refusal list by name. The DTC module has no
        builder for it; this pins that the gate would stop one anyway.
        """
        self.assertIn(0x14, WRITE_SERVICES)

        with self.assertRaises(UnsafePayload) as caught:
            assert_observational(bytes([0x14, 0xFF, 0xFF, 0xFF]))

        self.assertIn("ClearDiagnosticInformation", str(caught.exception))
        self.assertFalse(hasattr(udsdtc, "clear"))
        self.assertFalse(any("clear" in name.lower() for name in udsdtc.__all__))

    def test_the_builder_guard_itself_fails_closed(self):
        """A future builder producing a non-0x19 read must not pass silently."""
        with self.assertRaises(AssertionError):
            udsdtc._built(bytes([0x22, 0xF1, 0x90]))

        with self.assertRaises(UnsafePayload):
            udsdtc._built(bytes([0x14, 0xFF, 0xFF, 0xFF]))

    def test_dtc_codes_are_24_bit(self):
        with self.assertRaises(ValueError):
            udsdtc.snapshot_by_dtc(0x1000000)

        with self.assertRaises(ValueError):
            udsdtc.extended_data_by_dtc(-1)

    def test_the_ista_default_mask_is_the_one_the_table_quotes(self):
        self.assertEqual(udsdtc.MASK_PENDING_OR_CONFIRMED, 0x0C)
        self.assertEqual(udsdtc.report_by_status_mask(udsdtc.MASK_PENDING_OR_CONFIRMED),
                         hexb("19 02 0C"))


class TheParsers(unittest.TestCase):
    def test_a_report_is_a_list_of_bmw_codes_with_status(self):
        report = udsdtc.parse_report(hexb("59 02 FF 12 34 56 08 AB CD EF 2C"))

        self.assertEqual(report.subfunction, 0x02)
        self.assertEqual(report.availability_mask, 0xFF)
        self.assertEqual(len(report.dtcs), 2)

        first, second = report.dtcs
        self.assertEqual((first.fault, first.ftb, first.status), (0x1234, 0x56, 0x08))
        self.assertEqual(first.severity, "stored")
        self.assertEqual(first.flags, ["confirmed"])
        self.assertEqual(first.text, "0x1234/0x56 stored [confirmed]")
        self.assertEqual((second.fault, second.ftb), (0xABCD, 0xEF))
        self.assertEqual(second.severity, "stored")
        self.assertEqual(second.flags, ["pending", "confirmed", "failedSinceClear"])

    def test_an_empty_report_is_zero_faults(self):
        report = udsdtc.parse_report(hexb("59 02 FF"))
        self.assertEqual(report.dtcs, ())

    def test_a_truncated_list_is_an_error_not_fewer_faults(self):
        for tail in ("12", "12 34", "12 34 56", "12 34 56 08 AB"):
            with self.subTest(tail=tail):
                with self.assertRaises(udsdtc.DtcParseError) as caught:
                    udsdtc.parse_report(hexb("59 02 FF " + tail))

                self.assertIn("multiple of 4", str(caught.exception))

    def test_a_negative_or_foreign_reply_does_not_parse(self):
        with self.assertRaises(udsdtc.DtcParseError):
            udsdtc.parse_report(hexb("7F 19 31"))

        with self.assertRaises(udsdtc.DtcParseError):
            udsdtc.parse_report(hexb("59 0A FF 12 34 56 08"))   # wrong subfunction

        with self.assertRaises(udsdtc.DtcParseError):
            udsdtc.parse_report(b"")

        #: 0x0A parses with the same shape when asked for
        report = udsdtc.parse_report(hexb("59 0A FF 12 34 56 50"), udsdtc.SUBFN_SUPPORTED)
        self.assertEqual(report.dtcs[0].severity, "not-run")

    def test_the_count_reply_has_a_fixed_shape(self):
        count = udsdtc.parse_number(hexb("59 01 FF 01 00 03"))
        self.assertEqual((count.availability_mask, count.format_identifier, count.count),
                         (0xFF, 0x01, 3))

        for bad in ("59 01 FF 01 00", "59 01 FF 01 00 03 00", "59 02 FF 01 00 03"):
            with self.subTest(bad=bad):
                with self.assertRaises(udsdtc.DtcParseError):
                    udsdtc.parse_number(hexb(bad))

    def test_detail_keeps_the_body_raw(self):
        detail = udsdtc.parse_detail(hexb("59 04 12 34 56 08 01 46 1B 39 08"), 0x04)

        self.assertEqual(detail.subfunction, 0x04)
        self.assertEqual((detail.dtc.fault, detail.dtc.ftb, detail.dtc.status),
                         (0x1234, 0x56, 0x08))
        self.assertEqual(detail.record_number, 1)
        self.assertEqual(detail.body, hexb("01 46 1B 39 08"))

        empty = udsdtc.parse_detail(hexb("59 06 12 34 56 08"), 0x06)
        self.assertIsNone(empty.record_number)
        self.assertEqual(empty.body, b"")

        with self.assertRaises(udsdtc.DtcParseError):
            udsdtc.parse_detail(hexb("59 02 FF"), 0x02)   # not a detail subfunction

        with self.assertRaises(udsdtc.DtcParseError):
            udsdtc.parse_detail(hexb("59 04 12 34"), 0x04)

    def test_severity_order_is_active_stored_pending_historic_notrun(self):
        self.assertEqual(udsdtc.severity(0x09), "ACTIVE")
        self.assertEqual(udsdtc.severity(0x08), "stored")
        self.assertEqual(udsdtc.severity(0x04), "pending")
        self.assertEqual(udsdtc.severity(0x20), "historic")
        self.assertEqual(udsdtc.severity(0x50), "not-run")
        self.assertEqual(udsdtc.severity(0x00), "-")


class ScriptedEcu:
    """Answers 0x19 requests from a script; records every payload seen."""

    def __init__(self, replies, negative=None):
        self.replies = {hexb(k): hexb(v) for k, v in replies.items()}
        self.negative = negative or {}
        self.sent = []

    def __call__(self, payload, dst, timeout):
        self.sent.append((bytes(payload), dst, timeout))

        if payload in self.negative:
            raise self.negative[payload]

        return self.replies[bytes(payload)]


class Refused(Exception):
    def __init__(self, nrc):
        super().__init__(f"NRC 0x{nrc:02X}")
        self.nrc = nrc


class TheToolSendsOnlyReads(unittest.TestCase):
    def setUp(self):
        self.tool = load_tool("dtc")

    def test_a_plain_readout_is_one_0x19_exchange(self):
        ecu = ScriptedEcu({"19 02 FF": "59 02 FF 12 34 56 08"})
        records = self.tool.read_faults(ecu, 0x12)

        self.assertEqual([p for p, _, _ in ecu.sent], [hexb("19 02 FF")])
        self.assertEqual([(d, t) for _, d, t in ecu.sent], [(0x12, 3.0)])

        [record] = records
        self.assertEqual(record["kind"], "dtc_report")
        self.assertEqual(record["outcome"], "ok")
        self.assertEqual(record["dtcs"], [{
            "fault": "0x1234", "ftb": "0x56", "status": "0x08",
            "severity": "stored", "flags": ["confirmed"],
        }])
        self.assertEqual(record["frames"][0]["rx"], "59 02 ff 12 34 56 08")
        self.assertIn("ISTA", record["plausibility_note"])

    def test_count_and_detail_add_only_more_0x19_frames(self):
        ecu = ScriptedEcu({
            "19 01 0C": "59 01 FF 01 00 02",
            "19 02 0C": "59 02 FF 12 34 56 08 AB CD EF 2C",
            "19 04 12 34 56 FF": "59 04 12 34 56 08 01 46 1B",
            "19 06 12 34 56 FF": "59 06 12 34 56 08 01 02",
            "19 04 AB CD EF FF": "59 04 AB CD EF 2C",
            "19 06 AB CD EF FF": "59 06 AB CD EF 2C 01 05",
        })
        records = self.tool.read_faults(
            ecu, 0x18, mask=0x0C, count=True, detail=True, timeout=1.5,
        )

        self.assertEqual(len(ecu.sent), 6)
        for payload, dst, timeout in ecu.sent:
            self.assertEqual(payload[0], 0x19)
            self.assertEqual((dst, timeout), (0x18, 1.5))
            assert_observational(payload)

        kinds = [r["kind"] for r in records]
        self.assertEqual(kinds, ["dtc_count", "dtc_report", "dtc_snapshot",
                                 "dtc_extended", "dtc_snapshot", "dtc_extended"])
        self.assertEqual(records[0]["count"], 2)
        self.assertEqual(records[0]["signals"], {"dtc_count": 2})
        self.assertEqual(records[2]["body"], "01 46 1b")
        self.assertEqual(records[2]["record_number"], 1)
        self.assertEqual(records[4]["body_bytes"], 0)
        self.assertTrue(all(r["outcome"] == "ok" for r in records))
        self.assertTrue(all(r["ecu_addr"] == "0x18" for r in records))

    def test_a_refusal_is_recorded_as_data_and_the_readout_continues(self):
        ecu = ScriptedEcu(
            {"19 02 FF": "59 02 FF 12 34 56 08",
             "19 06 12 34 56 FF": "59 06 12 34 56 08"},
            negative={hexb("19 04 12 34 56 FF"): Refused(0x12)},
        )
        records = self.tool.read_faults(
            ecu, 0x12, detail=True,
            nrc_fields=lambda exc: {"nrc": f"0x{exc.nrc:02X}",
                                    "nrc_name": "subFunctionNotSupported",
                                    "service": "0x19"},
            negative_type=Refused,
        )

        self.assertEqual([r["outcome"] for r in records],
                         ["ok", "negative_response", "ok"])
        snapshot = records[1]
        self.assertEqual(snapshot["nrc"], "0x12")
        self.assertEqual(snapshot["nrc_name"], "subFunctionNotSupported")
        self.assertEqual(snapshot["frames"][0]["nrc"], "0x12")
        self.assertIsNone(snapshot["frames"][0]["rx"])

    def test_a_transport_failure_is_recorded_not_raised(self):
        def dead(payload, dst, timeout):
            raise TimeoutError("HSFZ read timeout")

        [record] = self.tool.read_faults(dead, 0x12)
        self.assertEqual(record["outcome"], "no_response: TimeoutError: HSFZ read timeout")
        self.assertNotIn("dtcs", record)

    def test_a_garbled_reply_is_unparsed_not_fewer_faults(self):
        ecu = ScriptedEcu({"19 02 FF": "59 02 FF 12 34"})
        [record] = self.tool.read_faults(ecu, 0x12)

        self.assertTrue(record["outcome"].startswith("unparsed:"))
        self.assertNotIn("dtcs", record)

    def test_the_tool_never_builds_a_payload_itself(self):
        """
        Every payload the tool sends comes from a `bmwdiag.dtc` builder.
        Pinned at the token level: the tool's CODE (comments and
        docstrings aside) contains no `bytes(` construction and no
        service-byte literal, so a new frame can only enter through the
        gated module.
        """
        import ast

        path = os.path.join(support.ROOT, "tools", "dtc.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), path)

        constructed = []
        literals = []
        called = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else (
                    func.attr if isinstance(func, ast.Attribute) else "")
                called.append(name)
                if name in ("bytes", "bytearray", "fromhex", "to_bytes"):
                    constructed.append(name)
            elif isinstance(node, ast.Constant) and isinstance(node.value, (int, bytes)) \
                    and not isinstance(node.value, bool):
                literals.append(node.value)

        self.assertEqual(constructed, [])
        self.assertNotIn(0x14, literals)
        self.assertNotIn(0x19, literals)
        self.assertFalse(any(isinstance(v, bytes) for v in literals))
        self.assertIn("assert_observational", called)
        self.assertIn("report_by_status_mask", called)

    def test_only_the_transport_nrc_type_counts_as_negative(self):
        """
        The default negative type is the protocol's NegativeResponse -
        not `Exception`, which would file a dead link as "the ECU said no".
        """
        from bmwdiag.protocol import NegativeResponse

        def refusing(payload, dst, timeout):
            raise NegativeResponse(0x19, 0x12)

        [record] = self.tool.read_faults(refusing, 0x12)
        self.assertEqual(record["outcome"], "negative_response")


class OneDefinitionOfTheStatusByte(unittest.TestCase):
    def test_egs_and_dtc_tools_share_the_module(self):
        egs = load_tool("egs")
        dtc = load_tool("dtc")

        self.assertIs(egs.STATUS_BITS, udsdtc.STATUS_BITS)
        self.assertIs(egs.dtc_severity, udsdtc.severity)
        self.assertIs(dtc.udsdtc, udsdtc)


if __name__ == "__main__":
    unittest.main()
