"""
Structured diagnostic errors (issue #11).

Before this, what went wrong on the wire was decided by reading prose:
the executor matched exception class names ending in ``Nack``, the
validation tool searched ``str(exc)`` for ``"NRC"``, and the reconnect
path looked for the word ``"closed"``. A negative UDS response - the ECU
answering, in order to decline - was a generic ``HsfzError`` that no
policy recognised, so it counted as a dead link, tore the session down
and split the drive into a new run.

These pin the taxonomy in ``bmwdiag.protocol.errors`` end to end: the
HSFZ client raises exactly one category per failure with the evidence as
fields; the executor and the OBD session decide policy from the type;
the recorder, sync agent and ingest builder carry the structured detail;
and nothing on the runtime or validation path inspects exception text or
class-name suffixes.
"""

import json
import os
import pickle
import socket
import sqlite3
import struct
import sys
import tempfile
import time
import unittest

from tests import support  # noqa: F401
from tests.support import hexb

import live
from bmwdiag.mapping import (
    MappingExecutor,
    MappingRegistry,
    fault_detail,
    fault_kind,
    load_text,
)
from bmwdiag.mapping.errors import DecodeError, MappingError
from bmwdiag.mapping.execute import (
    NoResponse,
    TRANSPORT_FAULT_BUDGET,
    _is_request_fault,
)
from bmwdiag.mapping.registry import AllCapabilities
from bmwdiag.protocol.errors import (
    DiagnosticError,
    LinkError,
    NegativeResponse,
    RequestTimeout,
    ResponseMismatch,
    RoutingNack,
    TransportError,
    nrc_name,
)

sys.path.insert(0, os.path.join(support.ROOT, "infra"))
from sync import agent as sync_agent          # noqa: E402
from ingest import server as ingest_server    # noqa: E402
from common import wire                       # noqa: E402


# ----------------------------------------------------------------------
# A scripted socket, so HsfzClient can be driven through every failure
# without a gateway. Frames are queued as the gateway would send them.
# ----------------------------------------------------------------------


TESTER = live.TESTER_ADDR
DDE = 0x12


def frame(control, payload):
    return struct.pack(">IH", len(payload), control) + payload


def diag(src, body):
    """A diagnostic frame from ECU `src` to the tester."""
    return frame(live.HSFZ_DIAG_REQ, bytes([src, TESTER]) + body)


class FakeSocket:
    """
    Feeds scripted chunks to recv(). An item may be bytes (delivered as
    one chunk), an exception (raised from recv) or a callable applied to
    the socket. When the script runs out, recv times out - which is what
    a real socket does when the ECU stays silent.
    """

    def __init__(self, script=(), send_error=None):
        self.script = list(script)
        self.sent = []
        self.blocking = True
        self.send_error = send_error
        self.closed = False

    def settimeout(self, value):
        pass

    def setblocking(self, flag):
        self.blocking = bool(flag)

    def setsockopt(self, *args):
        pass

    def sendall(self, data):
        if self.send_error is not None:
            raise self.send_error

        self.sent.append(bytes(data))

    def recv(self, n):
        if not self.blocking:
            #: _drain() empties the socket before every request; nothing
            #: is pending on a scripted socket.
            raise BlockingIOError()

        if not self.script:
            raise socket.timeout("timed out")

        item = self.script.pop(0)

        if isinstance(item, BaseException):
            raise item

        return item

    def close(self):
        self.closed = True


def client(script=(), send_error=None):
    c = live.HsfzClient("169.254.0.1", timeout=0.5)
    c.sock = FakeSocket(script, send_error)

    return c


class TheClientRaisesOneCategoryPerFailure(unittest.TestCase):
    """HsfzClient.request() fails with exactly one structured type."""

    def test_a_negative_response_is_structured(self):
        """`7F 22 31` -> NegativeResponse with the bytes as fields."""
        c = client([diag(DDE, hexb("7F 22 31"))])

        with self.assertRaises(NegativeResponse) as ctx:
            c.request(hexb("22 F1 90"), dst=DDE)

        exc = ctx.exception
        self.assertEqual(exc.service, 0x22)
        self.assertEqual(exc.nrc, 0x31)
        self.assertEqual(exc.raw, hexb("7F 22 31"))
        self.assertEqual(exc.target, DDE)
        self.assertEqual(exc.nrc_hex, "0x31")
        self.assertEqual(exc.name, "requestOutOfRange")
        self.assertEqual(exc.kind, "negative_response")
        #: An answer, not a transport failure - and not a socket one.
        self.assertNotIsInstance(exc, TransportError)
        self.assertNotIsInstance(exc, ConnectionError)
        self.assertNotIsInstance(exc, TimeoutError)

    def test_the_negative_response_message_carries_the_value(self):
        """Car link shows `NRC 0x31 (requestOutOfRange)`, not only prose."""
        c = client([diag(DDE, hexb("7F 22 31"))])

        with self.assertRaises(NegativeResponse) as ctx:
            c.request(hexb("22 F1 90"), dst=DDE)

        self.assertIn("NRC 0x31", str(ctx.exception))
        self.assertIn("requestOutOfRange", str(ctx.exception))
        self.assertEqual(ctx.exception.detail(), {
            "service": 0x22, "nrc": 0x31, "nrc_hex": "0x31",
            "nrc_name": "requestOutOfRange", "raw": "7f 22 31",
            "target": DDE,
        })

    def test_a_routing_nack_is_distinct_from_a_negative_response(self):
        for control in live.HSFZ_NACK_CONTROLS:
            with self.subTest(control=hex(control)):
                c = client([frame(control, b"")])

                with self.assertRaises(RoutingNack) as ctx:
                    c.request(hexb("22 DA 2E"), dst=0x18)

                exc = ctx.exception
                self.assertEqual(exc.target, 0x18)
                self.assertEqual(exc.control, control)
                self.assertEqual(exc.kind, "transport_nack")
                self.assertNotIsInstance(exc, NegativeResponse)
                self.assertNotIsInstance(exc, ConnectionError)
                self.assertNotIsInstance(exc, TimeoutError)

    def test_silence_is_a_request_timeout_not_a_link_error(self):
        c = client([])                          # nothing ever arrives

        with self.assertRaises(RequestTimeout) as ctx:
            c.request(hexb("22 F1 90"), dst=DDE, timeout=0.25)

        exc = ctx.exception
        self.assertIsInstance(exc, TimeoutError)
        self.assertNotIsInstance(exc, ConnectionError)
        self.assertEqual(exc.kind, "transport_timeout")
        self.assertEqual(exc.detail(), {"target": DDE, "timeout_s": 0.25})

    def test_the_gateway_closing_the_socket_is_a_link_error(self):
        c = client([b""])                       # orderly EOF

        with self.assertRaises(LinkError) as ctx:
            c.request(hexb("22 F1 90"), dst=DDE)

        exc = ctx.exception
        self.assertIsInstance(exc, ConnectionError)
        self.assertNotIsInstance(exc, TimeoutError)
        self.assertEqual(exc.reason, "closed")
        self.assertEqual(exc.kind, "transport_link")

    def test_a_tcp_reset_is_a_link_error_that_keeps_its_cause(self):
        """The ZGW serves one client; another tool shows up as a reset."""
        c = client([ConnectionResetError(104, "Connection reset by peer")])

        with self.assertRaises(LinkError) as ctx:
            c.request(hexb("22 F1 90"), dst=DDE)

        self.assertEqual(ctx.exception.reason, "reset")
        self.assertIsInstance(ctx.exception.__cause__, ConnectionResetError)

    def test_a_broken_pipe_on_send_is_a_link_error(self):
        c = client([], send_error=BrokenPipeError(32, "Broken pipe"))

        with self.assertRaises(LinkError) as ctx:
            c.request(hexb("22 F1 90"), dst=DDE)

        self.assertEqual(ctx.exception.reason, "broken_pipe")

    def test_a_desynchronised_stream_is_a_link_error(self):
        c = client([struct.pack(">IH", 0x7FFFFFFF, live.HSFZ_DIAG_REQ)])

        with self.assertRaises(LinkError) as ctx:
            c.request(hexb("22 F1 90"), dst=DDE)

        self.assertEqual(ctx.exception.reason, "framing")

    def test_not_connected_is_a_link_error(self):
        c = live.HsfzClient("169.254.0.1")

        with self.assertRaises(LinkError) as ctx:
            c.request(hexb("22 F1 90"), dst=DDE)

        self.assertEqual(ctx.exception.reason, "not_connected")

    def test_response_pending_is_not_a_negative_response(self):
        """
        NRC 0x78 means "still working"; the answer that follows it is
        the response. It must never surface as a fault.
        """
        c = client([
            diag(DDE, hexb("7F 22 78")),
            diag(DDE, hexb("62 F1 90 41 42")),
        ])

        self.assertEqual(
            c.request(hexb("22 F1 90"), dst=DDE), hexb("62 F1 90 41 42")
        )

    def test_a_positive_response_is_returned_unchanged(self):
        c = client([
            frame(live.HSFZ_DIAG_ACK, b""),
            diag(DDE, hexb("62 F1 90 41")),
        ])

        self.assertEqual(c.request(hexb("22 F1 90"), dst=DDE), hexb("62 F1 90 41"))


class ReconnectIsDecidedByCategory(unittest.TestCase):
    """request_safe() reconnects on a LinkError and on nothing else."""

    def _reconnecting(self, script):
        c = client(script)
        c.reconnects = 0

        def reconnect():
            c.reconnects += 1
            c.sock = FakeSocket([diag(DDE, hexb("62 F1 90 41"))])

        c.reconnect = reconnect

        return c

    def test_a_dead_link_reconnects_and_retries(self):
        c = self._reconnecting([b""])

        self.assertEqual(c.request_safe(hexb("22 F1 90"), dst=DDE), hexb("62 F1 90 41"))
        self.assertEqual(c.reconnects, 1)

    def test_a_reset_reconnects_without_reading_the_message(self):
        c = self._reconnecting([ConnectionResetError("reset")])

        c.request_safe(hexb("22 F1 90"), dst=DDE)

        self.assertEqual(c.reconnects, 1)

    def test_a_negative_response_does_not_reconnect(self):
        c = self._reconnecting([diag(DDE, hexb("7F 22 31"))])

        with self.assertRaises(NegativeResponse):
            c.request_safe(hexb("22 F1 90"), dst=DDE)

        self.assertEqual(c.reconnects, 0)

    def test_a_routing_nack_does_not_reconnect(self):
        c = self._reconnecting([frame(live.HSFZ_DIAG_NACK, b"")])

        with self.assertRaises(RoutingNack):
            c.request_safe(hexb("22 DA 2E"), dst=0x18)

        self.assertEqual(c.reconnects, 0)

    def test_a_timeout_does_not_reconnect(self):
        c = self._reconnecting([])

        with self.assertRaises(RequestTimeout):
            c.request_safe(hexb("22 F1 90"), dst=DDE, timeout=0.2)

        self.assertEqual(c.reconnects, 0)


# ----------------------------------------------------------------------
# Executor policy from the type.
# ----------------------------------------------------------------------


TWO_REQUESTS = """
schema_version: 1
mapping: {id: fixture, version: 1, production: false}
ecu: {family: test, target: 0x12}
requests:
  first:
    protocol: uds
    service: 0x22
    did: 0xF300
    response: {data_length: 1}
    signals:
      alpha: {label: Alpha, unit: C, decode: {type: uint8}}
  second:
    protocol: uds
    service: 0x22
    did: 0xF301
    response: {data_length: 1}
    signals:
      beta: {label: Beta, unit: C, decode: {type: uint8}}
"""


class ScriptedTransport:
    def __init__(self, script):
        self.script = list(script)

    def request(self, payload, *, dst, timeout=None):
        item = self.script.pop(0) if self.script else None

        if isinstance(item, Exception):
            raise item

        return bytes([payload[0] + 0x40, payload[1], payload[2], 0x2A])


def executor(script, on_error=None):
    mapping = load_text(TWO_REQUESTS, "test")
    profile = MappingRegistry([mapping]).resolve(AllCapabilities(), config={})

    return MappingExecutor(
        profile, transport=ScriptedTransport(script), on_error=on_error
    ), profile


class Classification(unittest.TestCase):
    def test_kinds_come_from_the_type(self):
        cases = [
            (LinkError("closed", reason="closed"), "transport_link"),
            (RoutingNack(0x18), "transport_nack"),
            (RequestTimeout(0x12, 0.4), "transport_timeout"),
            (NegativeResponse(0x22, 0x31), "negative_response"),
            (ResponseMismatch("bad shape"), "response_mismatch"),
            (NoResponse("dropped"), "no_response"),
            (DecodeError("bad", "f", "p"), "decode"),
            (MappingError("bad"), "decode"),
            (ConnectionResetError("raw"), "transport_link"),
            (BrokenPipeError("raw"), "transport_link"),
            (TimeoutError("raw"), "transport_timeout"),
            (socket.timeout("raw"), "transport_timeout"),
            (live.HsfzError("no ECU answered"), "other"),
            (ValueError("unrelated"), "other"),
        ]

        for exc, expected in cases:
            with self.subTest(exc=exc):
                self.assertEqual(fault_kind(exc), expected)

    def test_a_class_name_no_longer_decides_anything(self):
        """
        The old classifier matched `__name__.endswith("Nack")`. A class
        that merely sounds like a refusal is unclassified now - and
        unclassified means link fault, the conservative direction.
        """
        class GatewayNack(Exception):
            pass

        self.assertEqual(fault_kind(GatewayNack("no route")), "other")
        self.assertFalse(_is_request_fault(GatewayNack("no route")))

    def test_the_detail_is_the_evidence_as_values(self):
        self.assertEqual(
            fault_detail(NegativeResponse(0x22, 0x31, hexb("7F 22 31"), 0x12)),
            {"service": 0x22, "nrc": 0x31, "nrc_hex": "0x31",
             "nrc_name": "requestOutOfRange", "raw": "7f 22 31", "target": 0x12},
        )
        self.assertEqual(fault_detail(RoutingNack(0x18, 3)), {"target": 0x18, "control": 3})
        self.assertEqual(fault_detail(LinkError("x", reason="reset")), {"reason": "reset"})
        self.assertEqual(
            fault_detail(RequestTimeout(0x12, 0.4)), {"target": 0x12, "timeout_s": 0.4}
        )
        self.assertEqual(
            fault_detail(ResponseMismatch("bad", raw=b"\x7e", expected="41")),
            {"raw": "7e", "expected": "41"},
        )
        self.assertEqual(
            fault_detail(DecodeError("bad", "file.yaml", "requests.x")),
            {"source": "file.yaml", "path": "requests.x"},
        )
        self.assertEqual(fault_detail(ValueError("x")), {})

    def test_request_versus_link_by_category(self):
        for exc in (RoutingNack(0x18), RequestTimeout(0x12, 0.4),
                    NegativeResponse(0x22, 0x31), ResponseMismatch("bad"),
                    TimeoutError("raw"), socket.timeout("raw")):
            with self.subTest(exc=exc):
                self.assertTrue(_is_request_fault(exc))

        for exc in (LinkError("closed", reason="closed"),
                    ConnectionResetError("raw"), BrokenPipeError("raw"),
                    live.HsfzError("unclassified"), ValueError("x")):
            with self.subTest(exc=exc):
                self.assertFalse(_is_request_fault(exc))

    def test_unknown_nrcs_keep_their_number(self):
        self.assertEqual(nrc_name(0x31), "requestOutOfRange")
        self.assertEqual(nrc_name(0x99), "unknown")
        self.assertEqual(NegativeResponse(0x22, 0x99).nrc_hex, "0x99")

    def test_the_types_survive_pickling(self):
        for exc in (LinkError("closed", reason="closed"), RoutingNack(0x18, 3),
                    RequestTimeout(0x12, 0.4),
                    NegativeResponse(0x22, 0x31, hexb("7F 22 31"), 0x12),
                    ResponseMismatch("bad", raw=b"\x7e", expected="41", target=0x12)):
            with self.subTest(exc=exc):
                back = pickle.loads(pickle.dumps(exc))
                self.assertEqual(type(back), type(exc))
                self.assertEqual(str(back), str(exc))
                self.assertEqual(back.detail(), exc.detail())


class ANegativeResponseIsOneRequestsFailure(unittest.TestCase):
    """
    The defect that motivated the issue: an NRC in the poll loop used to
    be an unrecognised HsfzError, which the executor treated as a dead
    link. The ECU declining one DID tore the session down.
    """

    def test_the_other_requests_keep_flowing(self):
        ex, profile = executor([NegativeResponse(0x22, 0x31), None])

        out = ex.execute_detailed(profile.requests)

        self.assertEqual([r.request_id for r in out], ["second"])

    def test_it_is_recorded_under_its_own_kind_with_detail(self):
        noted = []
        ex, profile = executor(
            [NegativeResponse(0x22, 0x31, hexb("7F 22 31"), 0x12), None],
            on_error=lambda rid, exc: noted.append((rid, exc)),
        )

        ex.execute_detailed(profile.requests)
        st = ex.stats()["first"]

        self.assertEqual(st["kinds"], {"negative_response": 1})
        self.assertEqual(st["last_detail"]["nrc"], 0x31)
        self.assertEqual(st["last_detail"]["nrc_hex"], "0x31")
        self.assertIn("NRC 0x31", st["last_error"])
        self.assertEqual([rid for rid, _ in noted], ["first"])
        self.assertIsInstance(noted[0][1], NegativeResponse)

    def test_it_never_counts_towards_a_reconnect(self):
        """The ECU answered. That is proof the link is alive."""
        ex, profile = executor(
            [NegativeResponse(0x22, 0x31)] * (TRANSPORT_FAULT_BUDGET * 4)
        )

        for _ in range(TRANSPORT_FAULT_BUDGET * 2):
            ex.execute_detailed(profile.requests)

        self.assertEqual(ex._transport_faults, 0)

    def test_a_malformed_reply_is_treated_the_same_way(self):
        ex, profile = executor([ResponseMismatch("unexpected reply", raw=b"\x7e"), None])

        out = ex.execute_detailed(profile.requests)

        self.assertEqual([r.request_id for r in out], ["second"])
        self.assertEqual(ex.stats()["first"]["kinds"], {"response_mismatch": 1})
        self.assertEqual(ex._transport_faults, 0)


class ALinkErrorReachesTheReconnectLogic(unittest.TestCase):
    def test_it_propagates_immediately(self):
        ex, profile = executor([LinkError("gateway closed", reason="closed"), None])

        with self.assertRaises(LinkError):
            ex.execute_detailed(profile.requests)

    def test_a_timeout_is_absorbed_but_still_counted_against_the_budget(self):
        ex, profile = executor([RequestTimeout(0x12, 0.4), None])

        out = ex.execute_detailed(profile.requests)

        self.assertEqual([r.request_id for r in out], ["second"])
        self.assertEqual(ex.stats()["first"]["kinds"], {"transport_timeout": 1})

    def test_only_silence_reaches_the_budget(self):
        ex, profile = executor([RequestTimeout(0x12, 0.4)] * (TRANSPORT_FAULT_BUDGET * 3))

        with self.assertRaises(RequestTimeout):
            for _ in range(TRANSPORT_FAULT_BUDGET + 2):
                ex.execute_detailed(profile.requests)

    def test_a_decode_failure_stays_distinct_from_the_wire(self):
        """Bytes came back; the mapping could not read them. Not a fault
        of the link, the gateway or the ECU."""
        class Garbled:
            def request(self, payload, *, dst, timeout=None):
                return b"\x62"                  # too short for any signal

        mapping = load_text(TWO_REQUESTS, "test")
        profile = MappingRegistry([mapping]).resolve(AllCapabilities(), config={})
        noted = []
        ex = MappingExecutor(
            profile, transport=Garbled(),
            on_error=lambda rid, exc: noted.append(fault_kind(exc)),
        )

        ex.execute_detailed(profile.requests)

        self.assertEqual(set(noted), {"decode"})
        self.assertEqual(ex._transport_faults, 0)


# ----------------------------------------------------------------------
# The OBD session decides by category too.
# ----------------------------------------------------------------------


class ScriptedClient:
    """A client whose request() raises or answers per call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def request(self, payload, timeout=None, dst=None):
        self.calls.append(bytes(payload))
        item = self.script.pop(0) if self.script else None

        if isinstance(item, Exception):
            raise item

        if item is None:
            out = bytearray([0x41])

            for pid in payload[1:]:
                out.extend([pid, 0x10, 0x20])

            return bytes(out)

        return item


class TheObdSession(unittest.TestCase):
    LENGTHS = {0x0C: 2, 0x0D: 2}

    def test_a_dead_link_propagates_from_the_batched_path(self):
        """
        Before: any HsfzError - a closed socket included - just switched
        off multi-PID batching, and the per-PID fallback then absorbed
        the dead link three strikes at a time. Nothing reconnected.
        """
        session = live.ObdSession(
            ScriptedClient([LinkError("gateway closed", reason="closed")]),
            self.LENGTHS,
        )

        with self.assertRaises(LinkError):
            session.read([0x0C, 0x0D])

        self.assertTrue(session.multi_ok, "a dead link says nothing about batching")

    def test_a_dead_link_propagates_from_the_per_pid_path(self):
        session = live.ObdSession(
            ScriptedClient([ConnectionResetError("reset")]), self.LENGTHS
        )
        session.multi_ok = False

        with self.assertRaises(ConnectionResetError):
            session.read([0x0C])

    def test_a_negative_response_retires_the_pid_not_the_link(self):
        session = live.ObdSession(
            ScriptedClient([NegativeResponse(0x01, 0x31)] * 5), self.LENGTHS
        )
        session.multi_ok = False

        for _ in range(3):
            session.read([0x0C])

        self.assertIn(0x0C, session.dead)

    def test_a_malformed_reply_switches_off_batching(self):
        session = live.ObdSession(
            ScriptedClient([b"\x7e\x00", None, None]), self.LENGTHS
        )

        got = session.read([0x0C, 0x0D])

        self.assertFalse(session.multi_ok)
        self.assertEqual(set(got), {0x0C, 0x0D})

    def test_a_reply_of_the_wrong_shape_is_a_response_mismatch(self):
        session = live.ObdSession(ScriptedClient([b"\x7e\x00"]), self.LENGTHS)

        with self.assertRaises(ResponseMismatch) as ctx:
            session._mode01([0x0C])

        self.assertEqual(ctx.exception.detail(), {"raw": "7e 00", "expected": "41"})


# ----------------------------------------------------------------------
# Storage: the structured detail travels beside kind + message.
# ----------------------------------------------------------------------


class Persistence(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "rec.db")
        self.rec = live.Recorder(self.db)
        self.rec.open()
        self.rec.start_run("VINREDACTED", "gw", "DDE", 0x12)
        time.sleep(0.05)

    def tearDown(self):
        try:
            self.rec.close()
        except Exception:
            pass

    def _rows(self):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(
                "SELECT request_id, kind, message, detail FROM errors ORDER BY rowid"
            ).fetchall()
        finally:
            con.close()

    def test_the_detail_is_stored_as_json(self):
        exc = NegativeResponse(0x22, 0x31, hexb("7F 22 31"), 0x12)
        self.rec.error("n47.d72.flow.4E18", fault_kind(exc), str(exc), fault_detail(exc))
        time.sleep(0.3)
        self.rec.close()

        rows = self._rows()
        self.assertEqual(rows[0][1], "negative_response")
        self.assertEqual(json.loads(rows[0][3]), {
            "service": 0x22, "nrc": 0x31, "nrc_hex": "0x31",
            "nrc_name": "requestOutOfRange", "raw": "7f 22 31", "target": 0x12,
        })

    def test_no_detail_is_an_empty_object_not_null(self):
        """NULL means "recorded before detail existed"; {} means none."""
        self.rec.error("r", "other", "x")
        time.sleep(0.3)
        self.rec.close()

        self.assertEqual(self._rows()[0][3], "{}")

    def test_a_huge_detail_drops_its_raw_bytes_rather_than_the_row(self):
        exc = ResponseMismatch("junk", raw=bytes(range(256)) * 8)
        self.rec.error("r", fault_kind(exc), str(exc), fault_detail(exc))
        time.sleep(0.3)
        self.rec.close()

        stored = json.loads(self._rows()[0][3])
        self.assertNotIn("raw", stored)
        self.assertLessEqual(len(self._rows()[0][3]), live.FAULT_DETAIL_LIMIT)

    def test_the_executor_to_recorder_path_carries_it(self):
        """The hop live.py wires: on_error -> Recorder.error with detail."""
        mapping = load_text(TWO_REQUESTS, "test")
        profile = MappingRegistry([mapping]).resolve(AllCapabilities(), config={})
        ex = MappingExecutor(
            profile,
            transport=ScriptedTransport([RoutingNack(0x18, 3), None]),
            on_error=lambda rid, exc: self.rec.error(
                rid, fault_kind(exc), str(exc), fault_detail(exc)
            ),
        )

        ex.execute_detailed(profile.requests)
        time.sleep(0.3)
        self.rec.close()

        rows = self._rows()
        self.assertEqual(rows[0][:2], ("first", "transport_nack"))
        self.assertEqual(json.loads(rows[0][3]), {"target": 0x18, "control": 3})

    def test_an_old_database_gains_the_column(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        con = sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE errors(run_id INTEGER, ts REAL, request_id TEXT,"
            " kind TEXT, message TEXT);"
        )
        con.commit()
        con.close()

        rec = live.Recorder(path)
        rec.open()
        rec.close()

        con = sqlite3.connect(path)
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(errors)")}
        finally:
            con.close()

        self.assertIn("detail", cols)

    def test_live_py_passes_the_detail_to_the_recorder(self):
        """Asserted structurally: reaching note_fault needs a car."""
        import ast

        tree = ast.parse(open(
            os.path.join(support.ROOT, "live.py"), encoding="utf-8"
        ).read())
        note = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "note_fault"
        )
        called = [
            n.func.id for n in ast.walk(note)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        ]

        self.assertIn("fault_kind", called)
        self.assertIn("fault_detail", called)


class Shipping(unittest.TestCase):
    def _db(self, path, with_detail=True):
        con = sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL,"
            " ended_at REAL, vin TEXT, gateway TEXT, ecu TEXT, ecu_addr INTEGER);"
        )
        con.execute("INSERT INTO runs(id,started_at,vin) VALUES(1,1.0,'V')")

        if with_detail:
            con.executescript(
                "CREATE TABLE errors(run_id INTEGER, ts REAL, request_id TEXT,"
                " kind TEXT, message TEXT, detail TEXT);"
            )
            con.execute(
                "INSERT INTO errors VALUES(1, 1.5, 'n47.d72.flow.4E18',"
                " 'negative_response', 'negative response to 0x22: NRC 0x31',"
                " '{\"nrc\":49,\"nrc_hex\":\"0x31\",\"service\":34}')"
            )
        else:
            con.executescript(
                "CREATE TABLE errors(run_id INTEGER, ts REAL, request_id TEXT,"
                " kind TEXT, message TEXT);"
            )
            con.execute(
                "INSERT INTO errors VALUES(1, 1.5, 'egs.selector.DA2E',"
                " 'transport_nack', 'will not route to 0x18')"
            )

        con.commit()
        con.close()

    def test_the_agent_ships_the_detail(self):
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        self._db(db)

        rows = sync_agent.read_errors(db, 0, 100)

        self.assertEqual(rows[0]["kind"], "negative_response")
        self.assertEqual(json.loads(rows[0]["detail"])["nrc"], 49)

    def test_a_database_without_the_column_ships_empty_detail(self):
        db = os.path.join(tempfile.mkdtemp(), "old.db")
        self._db(db, with_detail=False)

        rows = sync_agent.read_errors(db, 0, 100)

        self.assertEqual(rows[0]["kind"], "transport_nack")
        self.assertEqual(rows[0]["detail"], "")

    def test_the_ingest_builder_carries_it_to_the_lake(self):
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        self._db(db)
        rows = sync_agent.read_errors(db, 0, 100)
        batch = wire.decode(wire.encode(wire.columnar(
            "channel_errors",
            [{k: v for k, v in r.items() if k != "_rowid"} for r in rows],
        )))

        built = ingest_server.build_channel_errors(batch)

        self.assertEqual(built[0]["kind"], "negative_response")
        self.assertEqual(json.loads(built[0]["detail"])["nrc_hex"], "0x31")

    def test_the_lake_schema_and_migration_declare_the_column(self):
        schema = open(os.path.join(
            support.ROOT, "infra", "clickhouse", "init", "001_schema.sql"
        ), encoding="utf-8").read()
        table = schema[schema.index("telemetry.channel_errors"):]
        table = table[:table.index("ENGINE")]

        self.assertIn("detail", table)

        migrations = os.listdir(os.path.join(
            support.ROOT, "infra", "clickhouse", "migrations"
        ))
        self.assertTrue(
            any("channel_errors_detail" in m for m in migrations),
            "a deployed lake needs a migration for the new column",
        )


# ----------------------------------------------------------------------
# The acceptance criterion, pinned: no runtime or validation path reads
# exception text or class-name suffixes to decide what happened.
# ----------------------------------------------------------------------


class NothingParsesProse(unittest.TestCase):
    FILES = (
        "live.py",
        "bmwdiag/mapping/execute.py",
        "bmwdiag/protocol/errors.py",
        "bmwdiag/variant.py",
        "tools/validate_candidate.py",
        "tools/egs.py",
    )
    FORBIDDEN = (
        '"NRC" in str(',
        "'NRC' in str(",
        '"closed" in str(',
        '"closed" not in str(',
        '__name__.endswith(',
        '__class__.__name__.endswith(',
    )

    def test_no_string_or_class_name_inspection(self):
        for rel in self.FILES:
            text = open(os.path.join(support.ROOT, rel), encoding="utf-8").read()

            for needle in self.FORBIDDEN:
                with self.subTest(file=rel, needle=needle):
                    self.assertNotIn(needle, text)

    def test_the_old_alias_is_gone(self):
        """`HsfzNack` was the class the suffix match existed for."""
        self.assertFalse(hasattr(live, "HsfzNack"))

    def test_every_category_is_a_diagnostic_error(self):
        for cls in (LinkError, RoutingNack, RequestTimeout,
                    NegativeResponse, ResponseMismatch, NoResponse, live.HsfzError):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(issubclass(cls, DiagnosticError))


if __name__ == "__main__":
    unittest.main()
