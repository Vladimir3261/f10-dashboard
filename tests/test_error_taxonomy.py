"""
The diagnostic error taxonomy (issue #11): every class boundary, and the
executor's policy per class.

Three layers, each pinned here:

  wire -> exception   HsfzClient turns bytes (a `7F 22 31`, a NACK, a
                      0x78 then silence, a closed socket) into a typed
                      exception carrying structured fields;
  exception -> policy the executor decides skip / count / reconnect by
                      `isinstance` on `bmwdiag.errors`, never by class
                      name or message text;
  exception -> record `fault_kind` / `fault_detail` -> Recorder -> the
                      `errors.detail` column, as JSON with the NRC as a
                      number.

Everything below is synthetic: a scripted socket and a fake clock. No
car, no network, no BMW data.
"""

import json
import os
import sqlite3
import struct
import tempfile
import time
import unittest

from tests import support  # noqa: F401
from tests.test_hsfz_correlation import DDE, EGS, diag, make_client
from tests.test_transport_faults import (
    FIRST,
    SECOND,
    RestingCase,
    executor as build_executor,
    run,
)
from bmwdiag.mapping.execute import REQUEST_FAULT_LIMIT, REQUEST_REST_MAX_SECONDS

import live
from bmwdiag.errors import (
    DecodeFailure,
    DiagnosticError,
    LinkError,
    NegativeResponse,
    PendingTimeout,
    RequestTimeout,
    ResponseMismatch,
    RoutingNack,
    TransportError,
    classify_exception,
    nrc_name,
)
from bmwdiag.mapping.errors import DecodeError, ResponseMismatchError
from bmwdiag.mapping.execute import (
    TRANSPORT_FAULT_BUDGET,
    _answered,
    _is_request_fault,
    fault_detail,
    fault_kind,
)


def nack(target: int, control: int = 0x0003) -> bytes:
    """A gateway NACK frame for `target` (HSFZ control 0x0003)."""
    payload = bytes([live.TESTER_ADDR, target])

    return struct.pack(">IH", len(payload), control) + payload


# ------------------------------------------------------------ boundaries


class TheClassBoundaries(unittest.TestCase):
    """Each live.py exception is-a taxonomy class with the right facts."""

    def test_the_kinds_are_the_historical_strings(self):
        """These are `telemetry.channel_errors.kind`; renaming splits data."""
        self.assertEqual(LinkError().kind, "transport_link")
        self.assertEqual(RoutingNack().kind, "transport_nack")
        self.assertEqual(RequestTimeout().kind, "transport_timeout")
        self.assertEqual(PendingTimeout().kind, "pending_timeout")
        self.assertEqual(NegativeResponse(0x22, 0x31).kind, "negative_response")
        self.assertEqual(DecodeFailure().kind, "decode")
        self.assertEqual(ResponseMismatch().kind, "decode")
        self.assertEqual(DiagnosticError().kind, "other")

    def test_scope_and_answered_per_class(self):
        cases = [
            # (exception, scope, answered)
            (live.HsfzLinkError("closed", "closed"), "link", False),
            (live.HsfzNack(EGS), "request", True),
            (live.HsfzTimeout(), "request", False),
            (live.HsfzPendingTimeout(pending=3), "request", True),
            (live.HsfzNegativeResponse(0x22, 0x31), "request", True),
            (live.HsfzUnexpectedReply("wrong shape"), "request", True),
            (live.HsfzError("unclassified"), "link", False),
        ]

        for exc, scope, answered in cases:
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(exc.scope, scope)
                self.assertEqual(exc.answered, answered)
                self.assertEqual(
                    classify_exception(exc), (exc.kind, scope, answered)
                )

    def test_every_live_exception_is_an_hsfz_error_and_its_category(self):
        pairs = [
            (live.HsfzLinkError("x", "closed"), (LinkError, ConnectionError)),
            (live.HsfzNack(EGS), (RoutingNack, TransportError)),
            (live.HsfzTimeout(), (RequestTimeout, TimeoutError)),
            (live.HsfzPendingTimeout(), (PendingTimeout, RequestTimeout,
                                         live.HsfzTimeout, TimeoutError)),
            (live.HsfzNegativeResponse(0x22, 0x31), (NegativeResponse,)),
            (live.HsfzUnexpectedReply("x"), (ResponseMismatch, DecodeFailure)),
        ]

        for exc, bases in pairs:
            with self.subTest(exc=type(exc).__name__):
                self.assertIsInstance(exc, live.HsfzError)
                self.assertIsInstance(exc, DiagnosticError)
                for base in bases:
                    self.assertIsInstance(exc, base)

    def test_a_pending_timeout_is_a_timeout_first_but_its_own_kind(self):
        """
        MRO: `HsfzPendingTimeout(HsfzTimeout, PendingTimeout)`. The kind
        must come from PendingTimeout, not from HsfzTimeout's base
        RequestTimeout - which is what the test guards.
        """
        exc = live.HsfzPendingTimeout("no final answer", elapsed=5.2, pending=4)

        self.assertEqual(fault_kind(exc), "pending_timeout")
        self.assertTrue(_is_request_fault(exc))
        self.assertTrue(_answered(exc))
        self.assertEqual(fault_detail(exc), {"pending": 4, "elapsed_ms": 5200})

    def test_a_negative_response_carries_its_fields(self):
        exc = live.HsfzNegativeResponse(0x22, 0x31, raw=bytes.fromhex("7F 22 31"))

        self.assertEqual(exc.detail(), {
            "service": 0x22, "nrc": 0x31, "nrc_name": "requestOutOfRange",
            "raw": "7f 22 31",
        })
        self.assertEqual(fault_detail(exc), exc.detail())
        self.assertEqual(str(exc), "negative response to 0x22: NRC 0x31")
        self.assertFalse(exc.pending)
        self.assertTrue(NegativeResponse(0x22, 0x78).pending)

    def test_nrc_names_are_the_specifications_and_unknown_stays_unknown(self):
        self.assertEqual(nrc_name(0x31), "requestOutOfRange")
        self.assertEqual(nrc_name(0x78), "requestCorrectlyReceivedResponsePending")
        self.assertEqual(nrc_name(0x22), "conditionsNotCorrect")
        self.assertEqual(nrc_name(0xE3), "unknown")
        self.assertEqual(NegativeResponse(0x22, 0xE3).nrc_name, "unknown")

    def test_decode_errors_are_decode_failures_not_transport(self):
        """A mapping problem never reaches the link policy."""
        for exc in (DecodeError("short"), ResponseMismatchError("prefix")):
            with self.subTest(exc=type(exc).__name__):
                self.assertIsInstance(exc, DecodeFailure)
                self.assertNotIsInstance(exc, TransportError)
                self.assertEqual(fault_kind(exc), "decode")
                self.assertTrue(_is_request_fault(exc))
                self.assertTrue(_answered(exc))

        self.assertIsInstance(ResponseMismatchError("prefix"), ResponseMismatch)
        self.assertEqual(
            fault_detail(ResponseMismatchError("prefix")),
            {"category": "response_mismatch"},
        )

    def test_bare_stdlib_exceptions_classify_by_type(self):
        """What a raw socket raises before any transport classified it."""
        self.assertEqual(
            classify_exception(ConnectionResetError("reset")),
            ("transport_link", "link", False),
        )
        self.assertEqual(
            classify_exception(BrokenPipeError("pipe")),
            ("transport_link", "link", False),
        )
        self.assertEqual(
            classify_exception(TimeoutError("t")),
            ("transport_timeout", "request", False),
        )
        self.assertEqual(
            classify_exception(ValueError("?")), ("other", "link", False)
        )
        self.assertEqual(fault_detail(TimeoutError("t")), {})

    def test_the_message_text_is_not_what_decides(self):
        """
        The words "negative response ... NRC 0x31" in a message must not
        make a bare error a negative response, and a NegativeResponse
        with an unrelated message is still one.
        """
        prose = live.HsfzError("negative response to 0x22: NRC 0x31")
        self.assertEqual(fault_kind(prose), "other")
        self.assertFalse(_is_request_fault(prose))

        typed = live.HsfzNegativeResponse(0x22, 0x31)
        typed.args = ("something else entirely",)
        self.assertEqual(fault_kind(typed), "negative_response")
        self.assertEqual(fault_detail(typed)["nrc"], 0x31)

    def test_bmwdiag_errors_imports_nothing_from_the_package(self):
        """The layering that keeps `live.py` out of the engine."""
        import bmwdiag.errors as errors

        path = errors.__file__
        with open(path, encoding="utf-8") as fh:
            source = fh.read()

        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                self.assertNotIn("bmwdiag", stripped, line)
                self.assertNotIn("live", stripped.split()[1], line)
                self.assertNotIn("from .", stripped, line)


# ---------------------------------------------------------- wire -> type


class FromTheWire(unittest.TestCase):
    """HsfzClient raises the typed exception for each thing the wire does."""

    def test_a_7f_frame_is_a_negative_response_with_fields(self):
        client, sock, clock = make_client()
        sock.on_send = lambda body: [(0.02, diag(DDE, bytes.fromhex("7F 22 31")))]

        with self.assertRaises(live.HsfzNegativeResponse) as ctx:
            client.request(bytes.fromhex("22 F3 03"), dst=DDE)

        exc = ctx.exception
        self.assertEqual((exc.service, exc.nrc), (0x22, 0x31))
        self.assertEqual(exc.raw, bytes.fromhex("7F 22 31"))
        self.assertEqual(fault_kind(exc), "negative_response")
        self.assertEqual(fault_detail(exc)["nrc_name"], "requestOutOfRange")

    def test_a_gateway_nack_names_the_target(self):
        client, sock, clock = make_client()
        sock.on_send = lambda body: [(0.01, nack(EGS))]

        with self.assertRaises(live.HsfzNack) as ctx:
            client.request(bytes.fromhex("22 DA 2E"), dst=EGS)

        self.assertEqual(ctx.exception.target, EGS)
        self.assertEqual(ctx.exception.control, 0x0003)
        self.assertEqual(fault_detail(ctx.exception), {"target": EGS, "control": 3})
        self.assertEqual(fault_kind(ctx.exception), "transport_nack")
        self.assertIn("0x18", str(ctx.exception))

    def test_silence_is_a_plain_timeout(self):
        client, sock, clock = make_client()

        with self.assertRaises(live.HsfzTimeout) as ctx:
            client.request(bytes.fromhex("22 F3 03"), dst=DDE, timeout=1.0)

        exc = ctx.exception
        self.assertNotIsInstance(exc, live.HsfzPendingTimeout)
        self.assertEqual(fault_kind(exc), "transport_timeout")
        self.assertEqual(exc.pending, 0)
        self.assertAlmostEqual(exc.elapsed, 1.0, places=2)
        self.assertEqual(fault_detail(exc)["pending"], 0)
        self.assertIn("62 f3 03", fault_detail(exc)["expected"])

    def test_a_0x78_then_silence_is_a_pending_timeout(self):
        client, sock, clock = make_client()
        sock.on_send = lambda body: [
            (0.05, diag(DDE, bytes.fromhex("7F 22 78"))),
            (0.05, diag(DDE, bytes.fromhex("7F 22 78"))),
        ]

        with self.assertRaises(live.HsfzPendingTimeout) as ctx:
            client.request(bytes.fromhex("22 F3 03"), dst=DDE, timeout=1.0)

        exc = ctx.exception
        self.assertEqual(exc.pending, 2)
        self.assertEqual(fault_kind(exc), "pending_timeout")
        self.assertTrue(exc.answered)
        self.assertGreaterEqual(exc.elapsed, 1.0)
        self.assertEqual(fault_detail(exc)["pending"], 2)

    def test_a_closed_socket_is_a_link_error(self):
        client, sock, clock = make_client()
        sock.on_send = lambda body: [(0.0, b"")]    # recv() returns nothing

        with self.assertRaises(live.HsfzLinkError) as ctx:
            client.request(bytes.fromhex("22 F3 03"), dst=DDE)

        self.assertIsInstance(ctx.exception, ConnectionError)
        self.assertEqual(ctx.exception.reason, "closed")
        self.assertEqual(fault_kind(ctx.exception), "transport_link")
        self.assertFalse(_is_request_fault(ctx.exception))

    def test_not_connected_is_a_link_error_too(self):
        client, sock, clock = make_client()
        client.sock = None

        with self.assertRaises(live.HsfzLinkError) as ctx:
            client.request(bytes.fromhex("22 F3 03"), dst=DDE)

        self.assertEqual(ctx.exception.reason, "not_connected")

    def test_a_fin_queued_before_the_request_is_a_link_error_and_sends_nothing(self):
        """
        Review defect on #34: the pre-request drain in `_discard_queued`
        raises HsfzLinkError("closed") when recv() returns b"" - and
        that raise sat inside `except (BlockingIOError, OSError): pass`.
        Once HsfzLinkError became a ConnectionError (an OSError) the
        clause swallowed it: the request went out on a dead socket, the
        failure surfaced later as an HsfzTimeout, and an outstanding
        record was left dangling. The link error must win, before any
        byte is sent, with nothing left outstanding.
        """
        client, sock, clock = make_client()
        sock.inbox.append((0.0, b""))          # FIN already queued

        with self.assertRaises(live.HsfzLinkError) as ctx:
            client.request(bytes.fromhex("22 F3 03"), timeout=0.4, dst=DDE)

        self.assertEqual(ctx.exception.reason, "closed")
        self.assertIsInstance(ctx.exception, ConnectionError)
        self.assertEqual(sock.bodies_sent(), [])
        self.assertEqual(client.link_stats()["outstanding"], [])
        self.assertEqual(fault_kind(ctx.exception), "transport_link")


# ------------------------------------------------------- request_safe


class RequestSafeReconnectsByCategory(unittest.TestCase):
    """
    `request_safe` reconnects on a LinkError and on nothing else: a NACK,
    a negative response and a timeout all propagate untouched, because
    reconnecting on those would tear down a healthy link.
    """

    def _client(self, exc):
        client, sock, clock = make_client()
        calls = []

        def fake_request(data, timeout=None, dst=None, expect_src=None, expect=None):
            calls.append(bytes(data))
            if len(calls) == 1:
                raise exc
            return b"\x62\xf3\x03\x01"

        client.request = fake_request                     # type: ignore
        client.reconnect = lambda: calls.append(b"RECONNECT")   # type: ignore

        return client, calls

    def test_a_link_error_reconnects_and_retries_once(self):
        client, calls = self._client(live.HsfzLinkError("closed", "closed"))

        out = client.request_safe(bytes.fromhex("22 F3 03"), dst=DDE)

        self.assertEqual(out, b"\x62\xf3\x03\x01")
        self.assertEqual(calls, [b"\x22\xf3\x03", b"RECONNECT", b"\x22\xf3\x03"])

    def test_a_bare_socket_error_reconnects_too(self):
        client, calls = self._client(ConnectionResetError("reset"))

        client.request_safe(bytes.fromhex("22 F3 03"), dst=DDE)

        self.assertIn(b"RECONNECT", calls)

    def test_request_faults_propagate_without_a_reconnect(self):
        for exc in (
            live.HsfzNack(EGS),
            live.HsfzNegativeResponse(0x22, 0x31),
            live.HsfzTimeout(),
            live.HsfzPendingTimeout(pending=1),
            live.HsfzError("unclassified"),
        ):
            with self.subTest(exc=type(exc).__name__):
                client, calls = self._client(exc)

                with self.assertRaises(type(exc)):
                    client.request_safe(bytes.fromhex("22 F3 03"), dst=DDE)

                self.assertNotIn(b"RECONNECT", calls)
                self.assertEqual(len(calls), 1)


# ------------------------------------------------------ executor policy


class ExecutorPolicyPerClass(unittest.TestCase):
    """
    What the executor does with each class, on the two-request fixture
    from test_transport_faults: skip the request and carry on, count
    toward the link-dead budget, or re-raise for the reconnect logic.
    """

    def _one_fault(self, exc):
        """Poll once with `first` raising `exc` and `second` answering."""
        ex, profile = build_executor([exc, None])
        result = run(ex, profile)
        stats = ex.stats()["first"]

        return ex, result, stats

    def test_a_nack_is_skipped_answered_and_not_budgeted(self):
        ex, result, stats = self._one_fault(live.HsfzNack(EGS))

        self.assertEqual([r.request_id for r in result], ["second"])
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["kinds"], {"transport_nack": 1})
        self.assertTrue(stats["last_error"].startswith("transport_nack: "))
        self.assertEqual(stats["last_detail"], {"target": EGS})
        self.assertEqual(ex._transport_faults, 0)

    def test_a_negative_response_is_skipped_and_its_nrc_kept(self):
        ex, result, stats = self._one_fault(
            live.HsfzNegativeResponse(0x22, 0x31, raw=bytes.fromhex("7F 22 31"))
        )

        self.assertEqual([r.request_id for r in result], ["second"])
        self.assertEqual(stats["kinds"], {"negative_response": 1})
        self.assertEqual(stats["last_detail"]["nrc"], 0x31)
        self.assertEqual(stats["last_detail"]["nrc_name"], "requestOutOfRange")
        self.assertEqual(ex._transport_faults, 0)

    def test_a_plain_timeout_is_skipped_and_budgeted(self):
        ex, result, stats = self._one_fault(live.HsfzTimeout(elapsed=1.0))

        self.assertEqual([r.request_id for r in result], ["second"])
        self.assertEqual(stats["kinds"], {"transport_timeout": 1})
        self.assertEqual(stats["last_detail"], {"pending": 0, "elapsed_ms": 1000})
        # `second` answered after it, which resets the budget; without
        # that answer the silence accumulates:
        ex2, profile2 = build_executor([live.HsfzTimeout(), live.HsfzTimeout()])
        run(ex2, profile2)
        self.assertEqual(ex2._transport_faults, 2)

    def test_a_pending_timeout_is_skipped_but_not_budgeted(self):
        """
        The policy change of #11: a 0x78 is the ECU replying, so a
        request that timed out AFTER a 0x78 is evidence the link is
        alive and no longer counts toward concluding it is dead.
        """
        ex, profile = build_executor(
            [live.HsfzPendingTimeout(pending=2)] * (TRANSPORT_FAULT_BUDGET * 3)
        )

        for _ in range(TRANSPORT_FAULT_BUDGET + 2):
            run(ex, profile)                    # never raises

        self.assertEqual(ex._transport_faults, 0)
        self.assertEqual(set(ex.stats()["first"]["kinds"]), {"pending_timeout"})
        self.assertEqual(ex.stats()["first"]["last_detail"]["pending"], 2)

    def test_a_link_error_propagates_immediately(self):
        ex, profile = build_executor([live.HsfzLinkError("closed", "closed"), None])

        with self.assertRaises(live.HsfzLinkError):
            run(ex, profile)

        # the link's fault, not the request's: nothing is noted against it
        self.assertEqual(ex.stats()["first"]["kinds"], {})

    def test_an_unclassified_hsfz_error_propagates_too(self):
        """Conservative: unknown means link, as it always did."""
        ex, profile = build_executor([live.HsfzError("what?"), None])

        with self.assertRaises(live.HsfzError):
            run(ex, profile)

    def test_an_unexpected_reply_is_a_decode_fault_not_a_link_one(self):
        ex, result, stats = self._one_fault(live.HsfzUnexpectedReply("shape"))

        self.assertEqual([r.request_id for r in result], ["second"])
        self.assertEqual(stats["kinds"], {"decode": 1})
        self.assertEqual(stats["last_detail"], {"category": "response_mismatch"})
        self.assertEqual(ex._transport_faults, 0)

    def test_the_faults_are_reported_with_kind_and_detail(self):
        seen = []
        ex, profile = build_executor([live.HsfzNegativeResponse(0x22, 0x31), None])
        ex.on_error = lambda rid, exc: seen.append(
            (rid, fault_kind(exc), fault_detail(exc))
        )

        run(ex, profile)

        self.assertEqual(seen, [(
            "first", "negative_response",
            {"service": 0x22, "nrc": 0x31, "nrc_name": "requestOutOfRange"},
        )])


class PendingTimeoutsAndTheBudget(RestingCase):
    """
    The budget policy, over many turns with the rests expiring: a request
    that keeps getting 0x78-then-silence never triggers a reconnect on
    its own, and does not hide a request that is getting silence.
    """

    def test_pending_timeouts_alone_never_trigger_a_reconnect(self):
        self.build({
            FIRST: lambda: live.HsfzPendingTimeout(pending=1),
            SECOND: lambda: live.HsfzPendingTimeout(pending=3),
        })

        for _ in range(TRANSPORT_FAULT_BUDGET * 4):
            self.turns(REQUEST_FAULT_LIMIT)
            self.clock.advance(REQUEST_REST_MAX_SECONDS + 1)

        self.assertEqual(self.ex._transport_faults, 0)
        self.assertEqual(
            set(self.ex.stats()["first"]["kinds"]), {"pending_timeout"}
        )

    def test_a_pending_timeout_does_not_mask_silence(self):
        self.build({
            FIRST: lambda: live.HsfzPendingTimeout(pending=1),
            SECOND: lambda: live.HsfzTimeout(),
        })

        with self.assertRaises(live.HsfzTimeout):
            for _ in range(TRANSPORT_FAULT_BUDGET * 3):
                self.turns(1)
                self.clock.advance(REQUEST_REST_MAX_SECONDS + 1)


# ---------------------------------------------------------- recording


class TheRecorderStoresTheDetail(unittest.TestCase):
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

    def test_the_detail_lands_as_json_with_the_nrc_as_a_number(self):
        exc = live.HsfzNegativeResponse(0x22, 0x31, raw=bytes.fromhex("7F 22 31"))
        self.rec.error("n47.d72.dyn.4517", fault_kind(exc), str(exc), fault_detail(exc))
        self.rec.error("egs.selector.DA2E", "transport_timeout", "silence")
        time.sleep(0.3)
        self.rec.close()

        rows = self._rows()
        self.assertEqual(rows[0][:3], (
            "n47.d72.dyn.4517", "negative_response",
            "negative response to 0x22: NRC 0x31",
        ))
        self.assertEqual(json.loads(rows[0][3]), {
            "nrc": 0x31, "nrc_name": "requestOutOfRange",
            "raw": "7f 22 31", "service": 0x22,
        })
        self.assertIsNone(rows[1][3])            # no detail: NULL, not "{}"

    def test_an_unserialisable_detail_drops_the_detail_not_the_row(self):
        self.rec.error("r", "other", "msg", {"bad": object()})
        time.sleep(0.3)
        self.rec.close()

        self.assertEqual(self._rows(), [("r", "other", "msg", None)])

    def test_an_old_database_gains_the_column_on_open(self):
        """A file recorded before #11 is migrated in place."""
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        con = sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL,"
            " ended_at REAL, vin TEXT, gateway TEXT, ecu TEXT, ecu_addr INTEGER);"
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
            columns = [r[1] for r in con.execute("PRAGMA table_info(errors)")]
        finally:
            con.close()

        self.assertIn("detail", columns)


if __name__ == "__main__":
    unittest.main()
