"""
Issue #12, the executor's side: what it tells the transport to expect,
what it does after a fault in a dynamic-identifier sequence, and how a
discarded late answer reaches the diagnostics view.
"""

import unittest

from tests import support  # noqa: F401

import live
from bmwdiag.mapping.execute import MappingExecutor
from bmwdiag.mapping.loader import load_text
from bmwdiag.mapping.polling import PollingPlan, resolve_classes
from bmwdiag.mapping.registry import AllCapabilities, MappingRegistry

DYNAMIC = """
schema_version: 1

mapping:
  id: correlation-fixture
  version: 1
  production: false

ecu:
  family: test
  target: 0x12

requests:
  oil:
    protocol: uds
    service: 0x22
    did: 0xF303
    setup: ["2C 03 F3 03", "2C 01 F3 03 45 17 01 02"]
    response: {data_length: 2}
    signals:
      oil_t:
        label: Oil
        unit: C
        decode: {type: uint16_be}
  coolant:
    protocol: uds
    service: 0x22
    did: 0xF303
    setup: ["2C 03 F3 03", "2C 01 F3 03 42 8B 01 02"]
    response: {data_length: 2}
    signals:
      coolant_t:
        label: Coolant
        unit: C
        decode: {type: uint16_be}
  plain:
    protocol: uds
    service: 0x22
    did: 0xDA2E
    response: {data_length: 2}
    signals:
      gear:
        label: Gear
        unit: ''
        decode: {type: uint8}
  kwp:
    protocol: raw
    payload: "2C 10 A0"
    response: {prefix: "6C 10", data_length: 2}
    signals:
      kwp_v:
        label: K
        unit: ''
        decode: {type: uint8}
"""


class RecordingTransport:
    """Answers plausibly, remembers every call, raises on demand."""

    def __init__(self):
        self.calls = []
        self.fail_next = None

    def request(self, payload, *, dst, timeout=None, expect=None):
        payload = bytes(payload)
        self.calls.append((payload, expect))

        if self.fail_next is not None and self.fail_next(payload):
            self.fail_next = None
            raise TimeoutError("HSFZ read timeout")

        if payload[0] == 0x2C and payload[1] == 0x10:
            return bytes.fromhex("6C 10 07 00")

        if payload[0] == 0x2C:
            return bytes([0x6C, payload[1], payload[2], payload[3]])

        return bytes([0x62, payload[1], payload[2], 0x00, 0x2A])


def build():
    mapping = load_text(DYNAMIC, "test")
    registry = MappingRegistry([mapping])
    profile = registry.resolve(AllCapabilities(), config={})
    transport = RecordingTransport()

    return MappingExecutor(profile, transport=transport), profile, transport


def sent(transport):
    return [p for p, _ in transport.calls]


class WhatTheTransportIsTold(unittest.TestCase):
    def test_poll_carries_an_expectation_labelled_with_the_request_id(self):
        ex, profile, transport = build()
        ex.execute_detailed([profile.request("plain")])

        payload, expect = transport.calls[-1]
        self.assertEqual(payload, bytes.fromhex("22 DA 2E"))
        self.assertIsNotNone(expect)
        self.assertEqual(expect.label, "plain")
        self.assertEqual(expect.sid, 0x62)
        self.assertEqual(expect.echo, (bytes.fromhex("DA 2E"),))
        # prefix (3) + data_length (2): a shorter frame is not the answer.
        self.assertEqual(expect.min_length, 5)
        self.assertTrue(expect.matches_positive(bytes.fromhex("62 DA 2E 00 03")))
        self.assertFalse(expect.matches_positive(bytes.fromhex("62 DA 2F 00 03")))

    def test_dynamic_read_expectation_names_its_own_request(self):
        ex, profile, transport = build()
        ex.execute_detailed([profile.request("oil")])
        ex.execute_detailed([profile.request("coolant")])

        polls = [(p, e) for p, e in transport.calls if p[0] == 0x22]
        self.assertEqual([e.label for _, e in polls], ["oil", "coolant"])
        # Content-identical, as the issue says: the transport's other
        # two layers exist because of this.
        self.assertTrue(polls[0][1].indistinguishable_from(polls[1][1]))

    def test_declared_non_echoing_prefix_reaches_the_transport(self):
        ex, profile, transport = build()
        results = ex.execute_detailed([profile.request("kwp")])

        payload, expect = transport.calls[-1]
        self.assertEqual(payload, bytes.fromhex("2C 10 A0"))
        self.assertEqual(expect.origin, "declared")
        self.assertEqual(expect.sid, 0x6C)
        self.assertEqual(expect.echo, (b"\x10",))
        self.assertTrue(expect.matches_positive(bytes.fromhex("6C 10 07 00")))
        self.assertEqual(results[0].values["kwp_v"], 7)

    def test_setup_frames_carry_no_expectation_and_use_the_echo_rule(self):
        ex, profile, transport = build()
        ex.execute_detailed([profile.request("oil")])

        setup = [(p, e) for p, e in transport.calls if p[0] == 0x2C]
        self.assertEqual(len(setup), 2)
        self.assertTrue(all(e is None for _, e in setup))


class AfterAFaultInASequence(unittest.TestCase):
    def test_setup_is_sent_once_while_all_goes_well(self):
        ex, profile, transport = build()
        ex.execute_detailed([profile.request("oil")])
        ex.execute_detailed([profile.request("oil")])

        self.assertEqual(sent(transport), [
            bytes.fromhex("2C 03 F3 03"),
            bytes.fromhex("2C 01 F3 03 45 17 01 02"),
            bytes.fromhex("22 F3 03"),
            bytes.fromhex("22 F3 03"),
        ])

    def test_timeout_on_the_poll_re_arms_the_definition(self):
        # The define may never have been processed, or the answer may
        # still be in flight: the next read must re-send clear+define,
        # two in-order exchanges the late answer cannot get past.
        ex, profile, transport = build()
        ex.execute_detailed([profile.request("oil")])
        transport.fail_next = lambda p: p[0] == 0x22
        ex.execute_detailed([profile.request("oil")])
        ex.execute_detailed([profile.request("oil")])

        self.assertEqual(sent(transport)[3:], [
            bytes.fromhex("22 F3 03"),                       # timed out
            bytes.fromhex("2C 03 F3 03"),                    # re-armed
            bytes.fromhex("2C 01 F3 03 45 17 01 02"),
            bytes.fromhex("22 F3 03"),
        ])
        st = ex.stats()["oil"]
        self.assertEqual(st["sent"], 3)
        self.assertEqual(st["ok"], 2)
        self.assertEqual(st["kinds"], {"transport_timeout": 1})

    def test_timeout_on_a_setup_frame_re_arms_too(self):
        ex, profile, transport = build()
        transport.fail_next = lambda p: p[:2] == bytes.fromhex("2C 01")
        ex.execute_detailed([profile.request("oil")])
        ex.execute_detailed([profile.request("oil")])

        self.assertEqual(sent(transport), [
            bytes.fromhex("2C 03 F3 03"),
            bytes.fromhex("2C 01 F3 03 45 17 01 02"),        # timed out
            bytes.fromhex("2C 03 F3 03"),
            bytes.fromhex("2C 01 F3 03 45 17 01 02"),
            bytes.fromhex("22 F3 03"),
        ])

    def test_a_fault_on_a_plain_request_does_not_disturb_the_armed_did(self):
        ex, profile, transport = build()
        ex.execute_detailed([profile.request("oil")])
        transport.fail_next = lambda p: p == bytes.fromhex("22 DA 2E")
        ex.execute_detailed([profile.request("plain")])
        ex.execute_detailed([profile.request("oil")])

        self.assertEqual(sent(transport)[3:], [
            bytes.fromhex("22 DA 2E"),
            bytes.fromhex("22 F3 03"),                       # still armed
        ])


class LateAnswersInTheStats(unittest.TestCase):
    def test_late_counter_is_per_request_and_starts_at_zero(self):
        ex, profile, transport = build()
        ex.execute_detailed([profile.request("plain")])

        self.assertEqual(ex.stats()["plain"]["late"], 0)
        ex.note_late_response("plain", "discarded 62 da 2e ...")
        ex.note_late_response("plain")
        self.assertEqual(ex.stats()["plain"]["late"], 2)
        self.assertEqual(ex.stats()["plain"]["failed"], 0)   # not a new fault

    def test_unknown_labels_are_ignored(self):
        # A pseudo id (`hsfz:0x12`) or an ad-hoc probe's label is not a
        # mapped request; it is recorded in channel_errors, not here.
        ex, profile, transport = build()
        ex.note_late_response("hsfz:0x12")
        ex.note_late_response("")
        self.assertNotIn("hsfz:0x12", ex.stats())
        self.assertNotIn("", ex.stats())


class InTheDiagnosticsView(unittest.TestCase):
    def build(self, client=None):
        mapping = load_text(DYNAMIC, "test")
        registry = MappingRegistry([mapping])
        profile = registry.resolve(AllCapabilities(), config={})
        plan = PollingPlan(
            profile.requests, resolve_classes(registry.polling_classes())
        )
        executor = MappingExecutor(profile, transport=RecordingTransport())
        diag = live.Diagnostics()
        diag.publish(profile=profile, executor=executor, plan=plan,
                     client=client, ecu="DDE", ecu_addr=0x12,
                     gateway="169.254.1.1", other_ecus=[], variants=[])

        return diag, profile, executor

    def test_late_appears_per_request_and_in_the_totals(self):
        diag, profile, executor = self.build()
        executor.execute_detailed([profile.request("plain")])
        executor.note_late_response("plain")

        report = diag.report()
        plain = next(r for r in report["requests"] if r["id"] == "plain")
        self.assertEqual(plain["late"], 1)
        self.assertEqual(plain["sent"], 1)
        self.assertEqual(plain["ok"], 1)
        self.assertEqual(report["totals"]["late"], 1)

    def test_transport_section_is_the_clients_link_stats(self):
        class Client:
            def link_stats(self):
                return {"timeouts": 2, "late_response": 1, "outstanding": []}

        diag, profile, executor = self.build(client=Client())
        self.assertEqual(
            diag.report()["transport"],
            {"timeouts": 2, "late_response": 1, "outstanding": []},
        )

    def test_transport_section_is_none_without_a_client(self):
        diag, profile, executor = self.build()
        self.assertIsNone(diag.report()["transport"])


class OrphanWiringTest(unittest.TestCase):
    """
    The transport reports an orphan as (label, kind, message); the poll
    loop's `note_orphan` turns that into a channel_errors row and, for a
    late answer, a tick on the request's `late` counter. This pins the
    contract those two sides share.
    """

    def test_recorder_accepts_the_new_kinds(self):
        import os
        import sqlite3
        import tempfile
        import time

        path = os.path.join(tempfile.mkdtemp(), "rec.db")
        rec = live.Recorder(path)
        rec.open()
        rec.start_run("VINREDACTED", "gw", "DDE", 0x12)
        time.sleep(0.05)
        rec.error("oil", "late_response", "discarded 62 f3 03 ...")
        rec.error("hsfz:0x12", "unexpected_response", "discarded 62 99 99")
        rec.close()

        rows = sqlite3.connect(path).execute(
            "SELECT request_id, kind FROM errors ORDER BY rowid"
        ).fetchall()

        self.assertEqual(rows, [
            ("oil", "late_response"),
            ("hsfz:0x12", "unexpected_response"),
        ])


class ProfileProbeTest(unittest.TestCase):
    """
    The connect-time profile probe replays a mapping's nominated read
    through the same client. It must hand the transport the mapping's
    declared shape, or a non-echoing protocol (`prefix: "6C 10"`) would
    have its genuine answer orphaned and the profile marked unsupported
    for a timeout that never happened on the wire.
    """

    def test_probe_passes_the_declared_expectation(self):
        from bmwdiag.variant import ProfileProbe

        mapping = load_text(DYNAMIC, "test")
        req = next(r for r in mapping.requests if r.id == "kwp")
        seen = []

        def request(payload, *, dst, timeout=None, expect=None):
            seen.append((bytes(payload), expect))

            return bytes.fromhex("6C 10 07 00")

        result = ProfileProbe(request, timeout=1.0).probe_one(req, 0x12)

        self.assertTrue(result.answered, result)
        payload, expect = seen[-1]
        self.assertEqual(payload, bytes.fromhex("2C 10 A0"))
        self.assertEqual(expect.origin, "declared")
        self.assertEqual(expect.label, "kwp")
        self.assertTrue(expect.matches_positive(bytes.fromhex("6C 10 07 00")))

    def test_setup_frames_in_a_probe_use_the_echo_rule(self):
        from bmwdiag.variant import ProfileProbe

        mapping = load_text(DYNAMIC, "test")
        req = next(r for r in mapping.requests if r.id == "oil")
        seen = []

        def request(payload, *, dst, timeout=None, expect=None):
            seen.append((bytes(payload), expect))

            if payload[0] == 0x2C:
                return bytes([0x6C]) + bytes(payload[1:4])

            return bytes.fromhex("62 F3 03 01 02")

        result = ProfileProbe(request).probe_one(req, 0x12)

        self.assertTrue(result.answered, result)
        self.assertEqual([e for _, e in seen[:2]], [None, None])
        self.assertEqual(seen[2][1].label, "oil")
        self.assertEqual(seen[2][1].echo, (bytes.fromhex("F3 03"),))


if __name__ == "__main__":
    unittest.main()
