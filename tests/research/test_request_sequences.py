"""
Multi-step request sequences: the Klartext F25 dynamic-F303 exchange.

The candidate mapping must carry the EXACT frames from the pcap-verified
session, the executor must send them once per session in declared order
before the poll, and the captured response must decode to exactly
46.0 degC through the runtime decoder.
"""

import os
import unittest

from tests import support  # noqa: F401
from tests.support import hexb

from bmwdiag.mapping import MappingExecutor, load_file, load_text
from bmwdiag.mapping.errors import InvalidFieldError
from bmwdiag.mapping.registry import AllCapabilities, MappingRegistry
from research.gate import candidate_gate
from research.importers import klartext_f25
from research.model import records_to_jsonl, validate_record

CANDIDATE = os.path.join(
    support.ROOT, "mappings", "candidates", "bmw", "dde", "n47",
    "d72n47a0_dynamic.yaml",
)


class RecordingTransport:
    """A DiagnosticTransport that logs every frame it is asked to send."""

    def __init__(self, table):
        self.table = table
        self.sent = []

    def request(self, payload, *, dst, timeout=None):
        self.sent.append((dst, bytes(payload)))
        return self.table.get(bytes(payload), b"\x6c\x03\xf3\x03")


class Records(unittest.TestCase):
    def setUp(self):
        self.records = klartext_f25.import_evidence()
        self.by_id = {r.record_id: r for r in self.records}

    def test_records_validate(self):
        for record in self.records:
            self.assertEqual(validate_record(record), [], record.record_id)

    def test_sequence_is_a_sequence_not_a_fabricated_did(self):
        oil = self.by_id["klartext.d72n47a0.ITOEL"]
        self.assertEqual(oil.request["sequence"], [
            "2C 03 F3 03",
            "2C 01 F3 03 45 17 01 02",
            "22 F3 03",
        ])
        # source id, wire DID, position and width are all distinct facts
        self.assertEqual(oil.source["source_identifier"], "0x4517")

    def test_on_car_records_pass_the_gate(self):
        for rid in ("klartext.d72n47a0.ITOEL", "klartext.d72n47a0.IMRUP",
                    "klartext.d72n47a0.IMPAS", "klartext.d72n47a0.ITMOT"):
            self.assertEqual(candidate_gate(self.by_id[rid]), [], rid)

    def test_itmot_is_candidate_not_externally_verified(self):
        """Klartext itself marks ITMOT derived-from-disassembly."""
        self.assertEqual(
            self.by_id["klartext.d72n47a0.ITMOT"].verification, "candidate"
        )

    def test_deterministic(self):
        self.assertEqual(
            records_to_jsonl(self.records),
            records_to_jsonl(klartext_f25.import_evidence()),
        )


class CandidateSequence(unittest.TestCase):
    def setUp(self):
        self.mapping = load_file(CANDIDATE)
        self.oil = next(
            r for r in self.mapping.requests if r.id == "n47.d72.dyn.4517"
        )

    def test_setup_frames_are_the_captured_bytes(self):
        self.assertEqual(
            [bytes(f) for f in self.oil.setup],
            [hexb("2C 03 F3 03"), hexb("2C 01 F3 03 45 17 01 02")],
        )
        # bound payload is the poll, never a fabricated DID
        from bmwdiag.protocol.request import build_payload
        self.assertEqual(build_payload(self.oil), hexb("22 F3 03"))

    def test_executor_sends_setup_once_in_order_then_polls(self):
        registry = MappingRegistry([self.mapping])
        profile = registry.resolve(
            AllCapabilities(), targets={"discovered_engine": 0x12}
        )
        transport = RecordingTransport({
            hexb("22 F3 03"): hexb("62 F3 03 39 08"),
        })
        executor = MappingExecutor(profile, transport=transport)

        values = executor.execute([self.oil])
        self.assertEqual(values, {"n47d_oil_temp": 46.0})

        self.assertEqual([p for _, p in transport.sent], [
            hexb("2C 03 F3 03"),
            hexb("2C 01 F3 03 45 17 01 02"),
            hexb("22 F3 03"),
        ])
        self.assertTrue(all(dst == 0x12 for dst, _ in transport.sent))

        # second poll: setup is armed, only the read goes out
        executor.execute([self.oil])
        self.assertEqual([p for _, p in transport.sent][3:], [hexb("22 F3 03")])

        # a NEW executor (fresh connection) re-arms the define
        executor2 = MappingExecutor(profile, transport=transport)
        executor2.execute([self.oil])
        self.assertEqual(
            [p for _, p in transport.sent][4], hexb("2C 03 F3 03")
        )

    def test_dpf_values_decode_to_the_session_values(self):
        """0.015259 and 0.01 g/bit against raws chosen from the scales."""
        from bmwdiag.mapping import decode_signal

        soot_meas = next(
            r for r in self.mapping.requests if r.id == "n47.d72.dyn.44BE"
        )
        # 1015 * 0.015259 = 15.487... -> 15.49 (the session's measured value)
        value = decode_signal(
            soot_meas.signals[0], soot_meas, hexb("62 F3 03 03 F7")
        )
        self.assertEqual(value, 15.49)

    def test_empty_setup_frame_is_rejected_by_the_loader(self):
        bad = """
schema_version: 1
mapping: {id: bad-setup, version: 1, production: false}
ecu: {target: 0x12}
requests:
  r1:
    protocol: uds
    service: 0x22
    did: 0xF303
    target: 0x12
    setup: [""]
    response: {data_length: 2}
    signals:
      s1: {label: X, unit: y, decode: {type: uint16_be}}
"""
        with self.assertRaises(InvalidFieldError):
            load_text(bad, source="<bad-setup>")

    def test_existing_mappings_are_unaffected_by_the_setup_field(self):
        production = load_file(support.OBD_MAPPING)

        for request in production.requests:
            self.assertEqual(request.setup, ())


if __name__ == "__main__":
    unittest.main()
