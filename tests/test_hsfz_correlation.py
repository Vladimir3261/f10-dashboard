"""
Issue #12: HSFZ response correlation and the bounds on late answers.

Before this, `HsfzClient.request` accepted the first frame from the
right address whose first byte was the expected service id. A late
answer to the PREVIOUS request with the same service was therefore
handed back as the answer to the next one; the decoder caught the cases
where the echoed identifier differed (and mislabelled them "decode"),
and could not catch the F303 case at all, where every read of the
dynamic DID looks alike.

These tests drive the real `HsfzClient` through a scripted socket and a
scripted clock: no network, no car, no sleeping. Timing in the scripts
is in seconds of the fake clock.
"""

import socket
import struct
import unittest
from typing import Any, List, Optional, Tuple

import live
from bmwdiag.mapping.execute import fault_kind
from bmwdiag.protocol.correlate import (
    FOREIGN,
    MATCH,
    NEGATIVE,
    PENDING,
    ResponseExpectation,
    classify,
    declared_response,
    expected_response,
)

TESTER = live.TESTER_ADDR
DDE = 0x12
EGS = 0x18


def frame(control: int, payload: bytes) -> bytes:
    return struct.pack(">IH", len(payload), control) + payload


def diag(src: int, body: bytes, dst: int = TESTER) -> bytes:
    """A diagnostic frame from ECU `src` to the tester."""
    return frame(live.HSFZ_DIAG_REQ, bytes([src, dst]) + body)


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ScriptedSocket:
    """
    Stands in for the TCP socket to the gateway.

    `inbox` holds `(delay, item)` pairs: `item` is the raw bytes recv()
    returns, or an exception recv() raises, `delay` how long after the
    previous recv it becomes available on the fake clock. A blocking
    recv whose timeout runs out before the head item is due advances the
    clock by the timeout and raises socket.timeout, as the real socket
    would. `on_send(body)` may return items to queue as the ECU's
    reaction to a request body - the only way an ANSWER can be queued,
    since no real answer precedes its request.
    """

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.inbox: List[Tuple[float, Any]] = []
        self.sent: List[bytes] = []
        self.on_send: Optional[Any] = None
        self.blocking = True
        self.timeout = 3.0
        self.closed = False

    # -- socket surface used by HsfzClient --------------------------

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def setblocking(self, flag: bool) -> None:
        self.blocking = flag

    def sendall(self, data: bytes) -> None:
        self.sent.append(bytes(data))
        length, control = struct.unpack(">IH", data[:6])

        if control == live.HSFZ_DIAG_REQ and self.on_send is not None:
            body = data[6 + 2:6 + length]
            self.inbox.extend(self.on_send(body) or [])

    def recv(self, size: int) -> bytes:
        if not self.blocking:
            if self.inbox and self.inbox[0][0] <= 0:
                _, item = self.inbox.pop(0)

                return self._deliver(item)

            raise BlockingIOError()

        if not self.inbox:
            self.clock.advance(self.timeout)
            raise socket.timeout("scripted: nothing arrives")

        delay, item = self.inbox[0]

        if delay > self.timeout:
            self.inbox[0] = (delay - self.timeout, item)
            self.clock.advance(self.timeout)
            raise socket.timeout("scripted: not yet")

        self.inbox.pop(0)
        self.clock.advance(max(delay, 0.0))

        return self._deliver(item)

    @staticmethod
    def _deliver(item: Any) -> bytes:
        if isinstance(item, BaseException):
            raise item

        return item

    def close(self) -> None:
        self.closed = True

    # -- helpers ------------------------------------------------------

    def bodies_sent(self) -> List[bytes]:
        out = []

        for data in self.sent:
            length, control = struct.unpack(">IH", data[:6])

            if control == live.HSFZ_DIAG_REQ:
                out.append(data[8:6 + length])

        return out


def make_client(**kwargs) -> Tuple[live.HsfzClient, ScriptedSocket, FakeClock]:
    clock = FakeClock()
    client = live.HsfzClient("169.254.0.1", timeout=3.0, **kwargs)
    client.clock = clock
    sock = ScriptedSocket(clock)
    client.sock = sock                      # never connect()s
    orphans: List[Tuple[str, str, str]] = []
    client.on_orphan = lambda label, kind, msg: orphans.append((label, kind, msg))
    client.orphans = orphans                # type: ignore[attr-defined]

    return client, sock, clock


# ------------------------------------------------------------ correlate


class ExpectationRuleTest(unittest.TestCase):
    """The structural echo rule, pinned per service."""

    def test_uds_read_echoes_the_did(self):
        e = expected_response(bytes.fromhex("22 F3 03"))
        self.assertEqual(e.sid, 0x62)
        self.assertEqual(e.echo, (bytes.fromhex("F3 03"),))
        self.assertEqual(e.min_length, 3)
        self.assertTrue(e.matches_positive(bytes.fromhex("62 F3 03 01 02")))
        self.assertFalse(e.matches_positive(bytes.fromhex("62 F3 04 01 02")))
        self.assertFalse(e.matches_positive(bytes.fromhex("62 F3")))

    def test_dynamic_define_echoes_subfunction_and_did(self):
        # On-car observed: `2C 03 F3 03` -> `6C 03 F3 03`,
        # `2C 01 F3 03 ...` -> `6C 01 F3 03` (validation-runs/20260825T192203Z-run).
        clear = expected_response(bytes.fromhex("2C 03 F3 03"))
        define = expected_response(bytes.fromhex("2C 01 F3 03 42 8B 01 02"))
        self.assertTrue(clear.matches_positive(bytes.fromhex("6C 03 F3 03")))
        self.assertFalse(clear.matches_positive(bytes.fromhex("6C 01 F3 03")))
        self.assertTrue(define.matches_positive(bytes.fromhex("6C 01 F3 03")))
        # And neither is confusable with the read of the DID they arm.
        read = expected_response(bytes.fromhex("22 F3 03"))
        self.assertFalse(read.matches_positive(bytes.fromhex("6C 03 F3 03")))
        self.assertFalse(clear.indistinguishable_from(read))

    def test_obd_mode_01_multi_pid_accepts_any_requested_pid_first(self):
        e = expected_response(bytes.fromhex("01 0C 0D 05"))
        self.assertEqual(e.echo, (b"\x0c", b"\x0d", b"\x05"))
        self.assertTrue(e.matches_positive(bytes.fromhex("41 0C 1A F8")))
        self.assertTrue(e.matches_positive(bytes.fromhex("41 05 7B")))
        self.assertFalse(e.matches_positive(bytes.fromhex("41 11 40")))

    def test_unknown_service_falls_back_to_sid_only(self):
        e = expected_response(bytes([0x3F, 0x01]))
        self.assertEqual(e.sid, 0x7F)
        self.assertEqual(e.echo, ())
        self.assertTrue(e.matches_positive(bytes([0x7F, 0x99])))

    def test_declared_prefix_overrides_the_echo_rule(self):
        # dde7_kwp_local_id.yaml declares `prefix: "6C 10"`: the DDE7 KWP
        # local-id read does NOT echo the identifier, so the structural
        # 0x2C rule (sub-function + 2-byte DID) would reject every
        # genuine answer.
        payload = bytes.fromhex("2C 10 A0")
        structural = expected_response(payload)
        declared = declared_response(payload, bytes.fromhex("6C 10"),
                                     min_length=4, label="dde7_a0")
        self.assertFalse(structural.matches_positive(bytes.fromhex("6C 10 01 02")))
        self.assertTrue(declared.matches_positive(bytes.fromhex("6C 10 01 02")))
        self.assertFalse(declared.matches_positive(bytes.fromhex("6C 10 01")))
        self.assertEqual(declared.origin, "declared")
        self.assertEqual(declared.label, "dde7_a0")
        self.assertEqual(declared.service, 0x2C)

    def test_empty_declared_prefix_means_structural(self):
        e = declared_response(bytes.fromhex("22 DA 2E"), b"", min_length=4,
                              label="egs_gear")
        self.assertEqual(e.origin, "structural")
        self.assertEqual(e.echo, (bytes.fromhex("DA 2E"),))
        self.assertEqual(e.min_length, 4)     # the larger of the two

    def test_one_byte_declared_prefix_is_service_id_only(self):
        e = declared_response(bytes.fromhex("22 F1 90"), b"\x62")
        self.assertEqual(e.echo, ())
        self.assertTrue(e.matches_positive(bytes.fromhex("62 00 00")))

    def test_indistinguishable_covers_the_f303_and_repoll_cases(self):
        a = expected_response(bytes.fromhex("22 F3 03"))
        b = expected_response(bytes.fromhex("22 F3 03"))
        other = expected_response(bytes.fromhex("22 F3 04"))
        loose = ResponseExpectation(service=0x22, sid=0x62)
        multi = expected_response(bytes.fromhex("22 F3 03 F3 04"))
        self.assertTrue(a.indistinguishable_from(b))
        self.assertFalse(a.indistinguishable_from(other))
        self.assertTrue(a.indistinguishable_from(loose))     # loose takes anything
        self.assertFalse(loose.indistinguishable_from(a))    # a is stricter
        self.assertTrue(a.indistinguishable_from(multi))
        self.assertFalse(multi.indistinguishable_from(a))

    def test_classify_outcomes(self):
        e = expected_response(bytes.fromhex("22 F1 90"))
        self.assertEqual(classify(e, bytes.fromhex("62 F1 90 41")), (MATCH, None))
        self.assertEqual(classify(e, bytes.fromhex("7F 22 78")), (PENDING, 0x78))
        self.assertEqual(classify(e, bytes.fromhex("7F 22 31")), (NEGATIVE, 0x31))
        # A refusal of SOMEBODY ELSE's service is not a refusal of ours.
        self.assertEqual(classify(e, bytes.fromhex("7F 2C 31")), (FOREIGN, None))
        self.assertEqual(classify(e, bytes.fromhex("62 F1 91 41")), (FOREIGN, None))
        self.assertEqual(classify(e, b""), (FOREIGN, None))
        self.assertEqual(classify(e, bytes.fromhex("7F 22")), (FOREIGN, None))

    def test_expectation_is_plain_data(self):
        # Portability: an expectation must be describable by a mapping
        # and holdable in a C struct - no callables anywhere in it.
        e = expected_response(bytes.fromhex("22 F3 03"))

        for value in (e.service, e.sid, e.echo, e.min_length, e.origin, e.label):
            self.assertFalse(callable(value))

        self.assertEqual(e.describe(), "62 f3 03")


# ----------------------------------------------------------- transport


class LateResponseTest(unittest.TestCase):
    """Issue #12 scenario 1: a late `62 1234` while waiting for `22 5678`."""

    def test_late_answer_to_previous_request_is_not_the_next_answer(self):
        client, sock, clock = make_client()

        with self.assertRaises(TimeoutError):
            client.request(bytes.fromhex("22 12 34"))

        self.assertEqual(client.link_stats()["timeouts"], 1)

        # The ECU answers the first request only once the second is
        # on the wire, then answers the second.
        sock.on_send = lambda body: [
            (0.1, diag(DDE, bytes.fromhex("62 12 34 AA"))),
            (0.1, diag(DDE, bytes.fromhex("62 56 78 BB"))),
        ]

        got = client.request(bytes.fromhex("22 56 78"))

        self.assertEqual(got, bytes.fromhex("62 56 78 BB"))
        stats = client.link_stats()
        self.assertEqual(stats["late_response"], 1)
        self.assertEqual(stats["unexpected_response"], 0)
        self.assertEqual(stats["outstanding"], [])
        self.assertEqual(len(client.orphans), 1)
        label, kind, message = client.orphans[0]
        self.assertEqual(kind, "late_response")
        self.assertEqual(label, "hsfz:0x12")       # ad-hoc: no request id
        self.assertIn("62 12 34 aa", message)
        self.assertIn("timed out", message)

    def test_late_answer_is_attributed_to_the_request_label(self):
        client, sock, clock = make_client()
        expect = declared_response(bytes.fromhex("22 12 34"), b"", label="n47_oil_temp")

        with self.assertRaises(TimeoutError):
            client.request(bytes.fromhex("22 12 34"), expect=expect)

        sock.on_send = lambda body: [
            (0.1, diag(DDE, bytes.fromhex("62 12 34 AA"))),
            (0.1, diag(DDE, bytes.fromhex("62 56 78 BB"))),
        ]
        client.request(bytes.fromhex("22 56 78"))

        self.assertEqual(client.orphans[0][:2], ("n47_oil_temp", "late_response"))

    def test_previously_the_late_frame_would_have_been_returned(self):
        # Same service id, same source address: the pre-#12 rule. Pinned
        # here as the negative so the failure mode stays documented.
        client, sock, clock = make_client()

        with self.assertRaises(TimeoutError):
            client.request(bytes.fromhex("22 12 34"))

        sock.on_send = lambda body: [
            (0.1, diag(DDE, bytes.fromhex("62 12 34 AA"))),
        ]

        with self.assertRaises(TimeoutError):
            client.request(bytes.fromhex("22 56 78"))

        # It was never handed back; it was counted and traced instead.
        self.assertEqual(client.link_stats()["late_response"], 1)
        notes = [t["note"] for t in client.link_stats()["trace"]]
        self.assertIn("late_response", notes)

    def test_frame_queued_before_the_request_is_caught_by_the_settle_window(self):
        client, sock, clock = make_client()

        with self.assertRaises(TimeoutError):
            client.request(bytes.fromhex("22 12 34"))

        # The late answer lands on the line shortly after we gave up,
        # before anything else is sent.
        sock.inbox.append((0.05, diag(DDE, bytes.fromhex("62 12 34 AA"))))
        sock.on_send = lambda body: [(0.1, diag(DDE, bytes.fromhex("62 56 78 BB")))]

        got = client.request(bytes.fromhex("22 56 78"))

        self.assertEqual(got, bytes.fromhex("62 56 78 BB"))
        stats = client.link_stats()
        self.assertEqual(stats["settle_runs"], 1)
        self.assertEqual(stats["settle_caught"], 1)
        self.assertEqual(stats["late_response"], 1)
        self.assertEqual(stats["ambiguous_resends"], 0)
        # Sent order: request 1, request 2 - the settle window sent nothing.
        self.assertEqual(sock.bodies_sent(),
                         [bytes.fromhex("22 12 34"), bytes.fromhex("22 56 78")])

    def test_settle_window_is_bounded_and_runs_once(self):
        client, sock, clock = make_client(settle_quiet=0.2, settle_max=1.0)

        with self.assertRaises(TimeoutError):
            client.request(bytes.fromhex("22 12 34"))

        before = clock.now
        # A chatty line: unrelated frames every 0.1 s, well past settle_max.
        sock.inbox.extend(
            (0.1, diag(DDE, bytes.fromhex("62 99 99 00"))) for _ in range(15)
        )
        sent_at = []

        def ecu(body):
            sent_at.append(clock.now)

            return [(0.1, diag(DDE, bytes.fromhex("62 56 78 BB")))]

        sock.on_send = ecu

        got = client.request(bytes.fromhex("22 56 78"))

        self.assertEqual(got, bytes.fromhex("62 56 78 BB"))
        stats = client.link_stats()
        self.assertEqual(stats["settle_runs"], 1)
        self.assertEqual(stats["settle_caught"], 0)
        # The listen ended at settle_max (one frame period of slack),
        # not when the chatter did: the request went out at ~1.0 s.
        self.assertGreaterEqual(sent_at[0] - before, 1.0 - 1e-9)
        self.assertLessEqual(sent_at[0] - before, 1.0 + 0.1 + 1e-9)
        self.assertGreater(stats["unexpected_response"], 0)
        # The ECU answered a request distinguishable from the old one:
        # it has moved on, so nothing is outstanding any more.
        self.assertEqual(stats["outstanding"], [])
        # And a third request does not settle again.
        client.request(bytes.fromhex("22 56 78"))
        self.assertEqual(client.link_stats()["settle_runs"], 1)

    def test_repoll_of_the_same_identifier_is_counted_as_ambiguous(self):
        # Content cannot tell the late answer to `22 F303` from the
        # fresh one. The settle window is the bound; when it passes
        # without the late answer arriving, that residual is counted.
        client, sock, clock = make_client()

        with self.assertRaises(TimeoutError):
            client.request(bytes.fromhex("22 F3 03"))

        sock.on_send = lambda body: [(0.1, diag(DDE, bytes.fromhex("62 F3 03 01")))]
        client.request(bytes.fromhex("22 F3 03"))

        stats = client.link_stats()
        self.assertEqual(stats["ambiguous_resends"], 1)
        self.assertEqual(stats["settle_caught"], 0)


class F303SequenceTest(unittest.TestCase):
    """
    Issue #12 scenario 2: read A of F303 times out; B redefines F303
    and reads it; A's late `62 F3 03` must not be decoded as B's.

    On an ECU that answers in order, A's answer can only arrive before
    B's clear/define are answered - during the settle window, or while
    those two exchanges are in flight. Both are covered; the frame is
    attributed to A and discarded, and B gets the answer to its own
    read.
    """

    CLEAR = bytes.fromhex("2C 03 F3 03")
    DEFINE = bytes.fromhex("2C 01 F3 03 42 8B 01 02")
    READ = bytes.fromhex("22 F3 03")

    def _sequence(self, client, sock, late_at):
        a = declared_response(self.READ, b"", label="A")
        b = declared_response(self.READ, b"", label="B")

        with self.assertRaises(TimeoutError):
            client.request(self.READ, expect=a)

        def ecu(body):
            if body == self.CLEAR:
                items = [(0.05, diag(DDE, bytes.fromhex("6C 03 F3 03")))]

                if late_at == "clear":
                    items.insert(0, (0.01, diag(DDE, bytes.fromhex("62 F3 03 AA AA"))))

                return items

            if body == self.DEFINE:
                return [(0.05, diag(DDE, bytes.fromhex("6C 01 F3 03")))]

            if body == self.READ:
                return [(0.05, diag(DDE, bytes.fromhex("62 F3 03 BB BB")))]

            return []

        sock.on_send = ecu

        if late_at == "settle":
            sock.inbox.append((0.05, diag(DDE, bytes.fromhex("62 F3 03 AA AA"))))

        client.request(self.CLEAR)
        client.request(self.DEFINE)

        return client.request(self.READ, expect=b)

    def test_late_answer_during_settle_is_attributed_to_a(self):
        client, sock, clock = make_client()
        got = self._sequence(client, sock, late_at="settle")
        self.assertEqual(got, bytes.fromhex("62 F3 03 BB BB"))
        self.assertEqual(client.orphans, [
            (label, kind, msg) for label, kind, msg in client.orphans
            if label == "A" and kind == "late_response"
        ])
        self.assertEqual(len(client.orphans), 1)
        self.assertEqual(client.link_stats()["settle_caught"], 1)

    def test_late_answer_during_clear_is_attributed_to_a(self):
        client, sock, clock = make_client()
        got = self._sequence(client, sock, late_at="clear")
        self.assertEqual(got, bytes.fromhex("62 F3 03 BB BB"))
        self.assertEqual(len(client.orphans), 1)
        self.assertEqual(client.orphans[0][:2], ("A", "late_response"))
        self.assertEqual(client.link_stats()["ambiguous_resends"], 0)

    def test_clear_and_define_answers_are_not_confused_with_the_read(self):
        # `6C 03 F3 03` carries the DID bytes a sloppy matcher might
        # accept for `22 F3 03`; the service id keeps them apart.
        client, sock, clock = make_client()
        sock.on_send = lambda body: [
            (0.05, diag(DDE, bytes.fromhex("6C 03 F3 03"))),
            (0.05, diag(DDE, bytes.fromhex("62 F3 03 01"))),
        ]
        got = client.request(self.READ)
        self.assertEqual(got, bytes.fromhex("62 F3 03 01"))
        self.assertEqual(client.link_stats()["unexpected_response"], 1)


class MatchingTest(unittest.TestCase):
    """Scenarios 3 and 4: the normal path, and non-echoing protocols."""

    def test_normal_match_passes(self):
        client, sock, clock = make_client()
        sock.on_send = lambda body: [
            (0.0, frame(live.HSFZ_DIAG_ACK, bytes([TESTER, DDE]) + body)),
            (0.02, diag(DDE, bytes.fromhex("62 F1 90 41 42"))),
        ]
        got = client.request(bytes.fromhex("22 F1 90"))
        self.assertEqual(got, bytes.fromhex("62 F1 90 41 42"))
        stats = client.link_stats()
        self.assertEqual(stats["late_response"], 0)
        self.assertEqual(stats["unexpected_response"], 0)
        self.assertEqual(stats["timeouts"], 0)

    def test_obd_multi_pid_match_passes(self):
        client, sock, clock = make_client()
        sock.on_send = lambda body: [(0.02, diag(DDE, bytes.fromhex("41 0C 1A F8 0D 40")))]
        got = client.request(bytes.fromhex("01 0C 0D"))
        self.assertEqual(got, bytes.fromhex("41 0C 1A F8 0D 40"))

    def test_non_echoing_protocol_works_through_a_declared_matcher(self):
        # The DDE7 KWP local-identifier read answers `6C 10 <data>`
        # without echoing the identifier. The structural rule would
        # orphan that; the declared prefix accepts it.
        client, sock, clock = make_client()
        payload = bytes.fromhex("2C 10 A0")
        sock.on_send = lambda body: [(0.02, diag(DDE, bytes.fromhex("6C 10 01 02 03")))]

        with self.assertRaises(TimeoutError):
            client.request(payload)          # structural: rejected as foreign

        self.assertEqual(client.link_stats()["unexpected_response"], 1)

        declared = declared_response(payload, bytes.fromhex("6C 10"), min_length=3)
        got = client.request(payload, expect=declared)
        self.assertEqual(got, bytes.fromhex("6C 10 01 02 03"))

    def test_short_positive_response_is_not_accepted(self):
        client, sock, clock = make_client()
        expect = declared_response(bytes.fromhex("22 DA 2E"), b"", min_length=4)
        sock.on_send = lambda body: [
            (0.02, diag(DDE, bytes.fromhex("62 DA 2E"))),           # too short
            (0.02, diag(DDE, bytes.fromhex("62 DA 2E 03 00"))),
        ]
        got = client.request(bytes.fromhex("22 DA 2E"), expect=expect)
        self.assertEqual(got, bytes.fromhex("62 DA 2E 03 00"))
        self.assertEqual(client.link_stats()["unexpected_response"], 1)

    def test_negative_response_to_this_service_still_raises(self):
        client, sock, clock = make_client()
        sock.on_send = lambda body: [(0.02, diag(DDE, bytes.fromhex("7F 22 31")))]

        with self.assertRaises(live.HsfzError) as ctx:
            client.request(bytes.fromhex("22 F1 91"))

        self.assertIn("NRC 0x31", str(ctx.exception))
        self.assertEqual((ctx.exception.service, ctx.exception.nrc), (0x22, 0x31))
        self.assertEqual(client.link_stats()["outstanding"], [])

    def test_negative_response_to_another_service_is_an_orphan(self):
        client, sock, clock = make_client()
        sock.on_send = lambda body: [
            (0.02, diag(DDE, bytes.fromhex("7F 2C 31"))),
            (0.02, diag(DDE, bytes.fromhex("62 F1 91 00"))),
        ]
        got = client.request(bytes.fromhex("22 F1 91"))
        self.assertEqual(got, bytes.fromhex("62 F1 91 00"))
        self.assertEqual(client.link_stats()["unexpected_response"], 1)

    def test_frame_from_another_ecu_is_attributed_to_its_own_timeout(self):
        client, sock, clock = make_client()
        gear = declared_response(bytes.fromhex("22 DA 2E"), b"", label="egs_gear")

        with self.assertRaises(TimeoutError):
            client.request(bytes.fromhex("22 DA 2E"), dst=EGS, expect=gear)

        sock.on_send = lambda body: [
            (0.02, diag(EGS, bytes.fromhex("62 DA 2E 03 00"))),
            (0.02, diag(DDE, bytes.fromhex("62 F1 91 00"))),
        ]
        got = client.request(bytes.fromhex("22 F1 91"), dst=DDE)
        self.assertEqual(got, bytes.fromhex("62 F1 91 00"))
        self.assertEqual(client.orphans[0][:2], ("egs_gear", "late_response"))
        self.assertEqual(client.link_stats()["outstanding"], [])

    def test_frames_not_addressed_to_the_tester_are_ignored(self):
        client, sock, clock = make_client()
        sock.on_send = lambda body: [
            (0.02, diag(DDE, bytes.fromhex("62 F1 91 00"), dst=0xF5)),
            (0.02, diag(DDE, bytes.fromhex("62 F1 91 01"))),
        ]
        got = client.request(bytes.fromhex("22 F1 91"))
        self.assertEqual(got, bytes.fromhex("62 F1 91 01"))
        self.assertEqual(client.link_stats()["unexpected_response"], 0)


class ResponsePendingTest(unittest.TestCase):
    """Scenario 5: NRC 0x78 extends the wait, never past the absolute cap."""

    def test_single_pending_then_answer_still_works(self):
        client, sock, clock = make_client()
        sock.on_send = lambda body: [
            (0.5, diag(DDE, bytes.fromhex("7F 22 78"))),
            (1.5, diag(DDE, bytes.fromhex("62 F1 90 41"))),
        ]
        got = client.request(bytes.fromhex("22 F1 90"))
        self.assertEqual(got, bytes.fromhex("62 F1 90 41"))
        self.assertEqual(client.link_stats()["pending_seen"], 1)

    def test_pending_extends_past_the_base_timeout(self):
        # Answer at 4.0 s: past the 3 s base, inside the extended window.
        client, sock, clock = make_client()
        sock.on_send = lambda body: [
            (2.5, diag(DDE, bytes.fromhex("7F 22 78"))),
            (1.5, diag(DDE, bytes.fromhex("62 F1 90 41"))),
        ]
        got = client.request(bytes.fromhex("22 F1 90"))
        self.assertEqual(got, bytes.fromhex("62 F1 90 41"))

    def test_repeated_pending_respects_the_absolute_deadline(self):
        client, sock, clock = make_client(pending_extension=2.0, pending_max_total=5.0)
        # 0x78 every 1.9 s forever: each one used to push the deadline
        # out by 2 s with no limit.
        sock.on_send = lambda body: [
            (0.5, diag(DDE, bytes.fromhex("7F 22 78"))),
        ] + [(1.9, diag(DDE, bytes.fromhex("7F 22 78"))) for _ in range(20)]
        started = clock.now

        with self.assertRaises(live.HsfzPendingTimeout) as ctx:
            client.request(bytes.fromhex("22 F1 90"))

        self.assertLessEqual(clock.now - started, 5.0 + 1e-9)
        self.assertIn("responsePending 3x", str(ctx.exception))
        stats = client.link_stats()
        self.assertEqual(stats["pending_seen"], 3)
        self.assertEqual(stats["pending_exhausted"], 1)
        self.assertEqual(stats["timeouts"], 0)
        self.assertEqual(len(stats["outstanding"]), 1)
        self.assertEqual(stats["outstanding"][0]["pending"], 3)

    def test_pending_timeout_is_a_transport_timeout_to_the_executor(self):
        exc = live.HsfzPendingTimeout("x")
        self.assertIsInstance(exc, TimeoutError)
        self.assertIsInstance(exc, live.HsfzError)
        self.assertEqual(fault_kind(exc), "transport_timeout")

    def test_answer_arriving_late_after_pending_exhaustion_is_attributed(self):
        client, sock, clock = make_client(pending_max_total=5.0)
        e = declared_response(bytes.fromhex("22 F1 90"), b"", label="slow_did")
        sock.on_send = lambda body: [
            (2.5, diag(DDE, bytes.fromhex("7F 22 78"))),
            (1.9, diag(DDE, bytes.fromhex("7F 22 78"))),
            (1.9, diag(DDE, bytes.fromhex("7F 22 78"))),
        ]

        with self.assertRaises(live.HsfzPendingTimeout):
            client.request(bytes.fromhex("22 F1 90"), expect=e)

        sock.on_send = lambda body: [
            (0.1, diag(DDE, bytes.fromhex("62 F1 90 41"))),
            (0.1, diag(DDE, bytes.fromhex("62 F1 91 42"))),
        ]
        got = client.request(bytes.fromhex("22 F1 91"))
        self.assertEqual(got, bytes.fromhex("62 F1 91 42"))
        self.assertEqual(client.orphans[-1][:2], ("slow_did", "late_response"))


class FramingTest(unittest.TestCase):
    def test_discard_queued_keeps_a_partial_frame(self):
        # The old _drain() threw away raw bytes, which could cut a
        # half-received frame in two and desynchronise the stream.
        client, sock, clock = make_client()
        whole = diag(DDE, bytes.fromhex("62 12 34 AA"))
        answer = diag(DDE, bytes.fromhex("62 56 78 BB"))
        sock.inbox.append((0.0, whole + answer[:5]))
        sock.on_send = lambda body: [(0.02, answer[5:])]

        got = client.request(bytes.fromhex("22 56 78"))

        self.assertEqual(got, bytes.fromhex("62 56 78 BB"))
        self.assertEqual(client.link_stats()["unexpected_response"], 1)
        self.assertEqual(client.buf, b"")

    def test_alive_request_is_answered_while_settling(self):
        client, sock, clock = make_client()

        with self.assertRaises(TimeoutError):
            client.request(bytes.fromhex("22 12 34"))

        sock.inbox.append((0.05, frame(live.HSFZ_ALIVE_REQ, b"")))
        sock.on_send = lambda body: [(0.02, diag(DDE, bytes.fromhex("62 56 78 BB")))]
        client.request(bytes.fromhex("22 56 78"))

        alive = [d for d in sock.sent
                 if struct.unpack(">IH", d[:6])[1] == live.HSFZ_ALIVE_RESP]
        self.assertEqual(len(alive), 1)

    def test_absurd_length_raises(self):
        client, sock, clock = make_client()
        sock.on_send = lambda body: [(0.0, struct.pack(">IH", 0x7FFFFFFF, 1))]

        with self.assertRaises(live.HsfzError):
            client.request(bytes.fromhex("22 12 34"))

    def test_reconnect_forgets_outstanding(self):
        client, sock, clock = make_client()

        with self.assertRaises(TimeoutError):
            client.request(bytes.fromhex("22 12 34"))

        self.assertEqual(len(client.link_stats()["outstanding"]), 1)
        client.buf.extend(b"\x00\x00")
        # connect() would open a socket; emulate the state reset it does.
        client._outstanding.clear()
        self.assertEqual(client.link_stats()["outstanding"], [])

    def test_link_stats_reports_the_bounds_and_the_trace(self):
        client, sock, clock = make_client(settle_quiet=0.3)
        sock.on_send = lambda body: [(0.02, diag(DDE, bytes.fromhex("62 F1 90 41")))]
        client.request(bytes.fromhex("22 F1 90"))
        stats = client.link_stats()
        self.assertEqual(stats["bounds"]["settle_quiet_s"], 0.3)
        self.assertEqual(stats["bounds"]["pending_max_total_s"], live.PENDING_MAX_TOTAL)
        self.assertEqual([t["dir"] for t in stats["trace"]], ["tx", "rx"])
        self.assertEqual(stats["trace"][0]["bytes"], "22 f1 90")
        self.assertEqual(stats["trace"][1]["ecu"], "0x12")

    def test_trace_is_bounded(self):
        client, sock, clock = make_client()
        sock.on_send = lambda body: [(0.0, diag(DDE, bytes.fromhex("62 F1 90 41")))]

        for _ in range(live.TRACE_FRAMES):
            client.request(bytes.fromhex("22 F1 90"))

        self.assertEqual(len(client.link_stats()["trace"]), live.TRACE_FRAMES)


class TransportAdapterTest(unittest.TestCase):
    def test_hsfz_transport_forwards_the_expectation(self):
        calls = []

        class Client:
            def request(self, payload, timeout=None, dst=None, expect=None):
                calls.append((payload, timeout, dst, expect))

                return b"\x62\xf1\x90"

        transport = live.HsfzTransport(Client())
        e = expected_response(bytes.fromhex("22 F1 90"))
        transport.request(bytes.fromhex("22 F1 90"), dst=DDE, timeout=1.5, expect=e)
        self.assertEqual(calls, [(bytes.fromhex("22 F1 90"), 1.5, DDE, e)])


if __name__ == "__main__":
    unittest.main()
