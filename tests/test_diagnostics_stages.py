"""
Issue #16: the request diagnostics reflect the wire, not the scheduler.

"sent 590" used to mean any of "the plan scheduled it", "a frame went
out", "the ECU answered" or "a value was stored", and the four differed
by more than the view could explain: an OBD batch put six requests on
the wire as one frame, a retired PID kept counting as sent and
unanswered on every cycle it was never asked, an F303 read was three
frames counted as one, and a positive response whose every signal was
a sentinel looked like a clean `ok`.

Every test here is offline and synthetic: a scripted HSFZ-shaped client
or a fake transport, no car. Timing is measured against a fake clock
where it matters, so nothing depends on how fast the test host is.
"""

import json
import os
import sqlite3
import tempfile
import time
import unittest

from tests import support  # noqa: F401

import live
from bmwdiag.mapping import MappingExecutor, fault_kind, load_file, load_text
from bmwdiag.mapping.decoder import OK, SENTINEL
from bmwdiag.mapping.execute import Retired
from bmwdiag.mapping.polling import PollingPlan, resolve_classes
from bmwdiag.mapping.registry import AllCapabilities, MappingRegistry
from bmwdiag.obd import ObdCapabilitySet
from bmwdiag.protocol import ObdExchange, ObdReadReport

SAMPLE = {
    0x0C: b"\x0c\x3c",      # 783 rpm
    0x0B: b"\x9e",          # 158 kPa
    0x0D: b"\x3c",          # 60 km/h
    0x05: b"\x83",          # 91 degC
    0x11: b"\x40",          # throttle
    0x33: b"\x63",          # baro
    0x2F: b"\xa0",          # fuel
}


class ScriptedClient:
    """
    An HSFZ-shaped client for `ObdSession`: answers Mode 01 from a PID
    table, raises a chosen exception for anything it does not have, and
    optionally refuses multi-PID frames. Remembers every frame.
    """

    def __init__(self, data, multi=True, missing=None):
        self.data = dict(data)
        self.multi = multi
        self.frames = []
        self.missing = missing or (lambda pid: live.HsfzTimeout(
            f"no answer for 0x{pid:02X}", elapsed=0.4
        ))
        self.last_answer_ambiguous = False

    def request(self, payload, timeout=None, dst=None, expect_src=None,
                expect=None):
        payload = bytes(payload)
        self.frames.append(payload)
        pids = list(payload[1:])

        if len(pids) > 1 and not self.multi:
            raise live.HsfzNegativeResponse(0x01, 0x12, b"\x7f\x01\x12")

        out = bytearray([0x41])

        for pid in pids:
            if pid not in self.data:
                raise self.missing(pid)

            out.append(pid)
            out.extend(self.data[pid])

        return bytes(out)

    def sent_to(self, pid):
        return [f for f in self.frames if pid in f[1:]]


def engine_profile(supported=None):
    registry = MappingRegistry([load_file(support.OBD_MAPPING)])
    caps = ObdCapabilitySet(supported) if supported else AllCapabilities()

    return registry.resolve(
        caps, config={"tank": 70.0}, targets={"discovered_engine": 0x12}
    )


def build(data=SAMPLE, multi=True, missing=None, on_error=None,
          supported=None):
    profile = engine_profile(supported)
    client = ScriptedClient(data, multi=multi, missing=missing)
    session = live.ObdSession(client, profile.obd_pid_lengths())
    executor = MappingExecutor(
        profile, transport=live.HsfzTransport(client), obd_reader=session,
        on_error=on_error,
    )

    return profile, client, session, executor


def requests(profile, *pids):
    return [profile.request(f"obd.mode01.{pid:02X}") for pid in pids]


class OneBatchIsOneExchange(unittest.TestCase):
    """N logical requests, one frame on the wire, attributed to each."""

    def test_six_pids_in_one_frame_count_once_at_the_wire(self):
        profile, client, _, executor = build()

        executor.execute(requests(profile, 0x0C, 0x0B, 0x0D, 0x05, 0x11, 0x33))

        self.assertEqual(len(client.frames), 1)
        wire = executor.wire_stats()
        self.assertEqual(wire["exchanges"], 1)
        self.assertEqual(wire["tx_frames"], 1)
        self.assertEqual(wire["rx_frames"], 1)
        self.assertEqual(wire["obd_batched_pids"], 6)

        stats = executor.stats()

        for pid in (0x0C, 0x0B, 0x0D, 0x05, 0x11, 0x33):
            st = stats[f"obd.mode01.{pid:02X}"]
            #: attributed: the shared frame counts once for EACH member
            self.assertEqual(st["scheduled"], 1)
            self.assertEqual(st["submitted"], 1)
            self.assertEqual(st["sent"], 1)              # historical alias
            self.assertEqual(st["exchanges"], 1)
            self.assertEqual(st["tx_frames"], 1)
            self.assertEqual(st["rx_frames"], 1)
            self.assertEqual(st["positive_response"], 1)
            self.assertEqual(st["ok"], 1)
            self.assertEqual(st["state"], "active")

    def test_seven_pids_are_two_frames(self):
        profile, client, _, executor = build()

        executor.execute(requests(profile, 0x0C, 0x0B, 0x0D, 0x05, 0x11,
                                  0x33, 0x2F))

        self.assertEqual(len(client.frames), 2)
        self.assertEqual(executor.wire_stats()["exchanges"], 2)
        self.assertEqual(executor.stats()["obd.mode01.2F"]["exchanges"], 1)

    def test_a_frame_the_ecu_will_not_batch_falls_back_and_is_counted(self):
        """
        Batch refused (NRC) -> the refusal is one exchange with its own
        kind on every member, then one frame per PID. The batch answer
        is not swallowed into `no_response` any more.
        """
        profile, client, session, executor = build(multi=False)

        executor.execute(requests(profile, 0x0C, 0x0B))

        self.assertFalse(session.multi_ok)
        self.assertEqual(len(client.frames), 3)          # 1 refused + 2 singles
        self.assertEqual(executor.wire_stats()["exchanges"], 3)

        st = executor.stats()["obd.mode01.0C"]
        self.assertEqual(st["submitted"], 1)
        self.assertEqual(st["exchanges"], 2)
        self.assertEqual(st["negative_response"], 1)
        self.assertEqual(st["positive_response"], 1)
        self.assertEqual(st["ok"], 1)
        self.assertEqual(st["kinds"], {"negative_response": 1})

    def test_a_positive_batch_missing_a_pid_keeps_what_it_returned(self):
        """
        The ECU answered the frame without one PID: that PID alone is
        `no_response`, the others are positive, and only the missing
        one is re-read singly - not the whole batch again.
        """
        class Partial(ScriptedClient):
            def request(self, payload, *a, **k):
                self.frames.append(bytes(payload))
                pids = [p for p in payload[1:] if p != 0x0B]
                if pids:
                    out = bytearray([0x41])
                    for pid in pids:
                        out.append(pid)
                        out.extend(self.data[pid])
                    return bytes(out)
                raise live.HsfzTimeout("no answer", elapsed=0.4)

        profile = engine_profile()
        client = Partial(SAMPLE)
        session = live.ObdSession(client, profile.obd_pid_lengths())
        executor = MappingExecutor(profile, obd_reader=session)

        got = executor.execute(requests(profile, 0x0C, 0x0B, 0x0D))

        self.assertEqual(set(got), {"rpm", "speed"})
        #: one batch, then one single re-read of the missing PID
        self.assertEqual(len(client.frames), 2)
        self.assertEqual(client.frames[1], b"\x01\x0b")
        stats = executor.stats()
        self.assertEqual(stats["obd.mode01.0C"]["positive_response"], 1)
        self.assertEqual(stats["obd.mode01.0C"]["failed"], 0)
        #: the batch was answered without it (no_response), then the
        #: single read timed out (timeout): two exchanges, two kinds
        self.assertEqual(stats["obd.mode01.0B"]["exchanges"], 2)
        self.assertEqual(stats["obd.mode01.0B"]["no_response"], 1)
        self.assertEqual(stats["obd.mode01.0B"]["timeout"], 1)


class RetirementMeansNotAsked(unittest.TestCase):
    """
    A retired PID is scheduled (the plan does not know) but never
    submitted: no frame, no fault, no phantom `no_response` - and the
    state says `retired` rather than leaving a frozen counter to be
    inferred from.
    """

    def run_cycles(self, n, **kw):
        seen = []
        profile, client, session, executor = build(
            data={0x0C: SAMPLE[0x0C]}, multi=False,
            on_error=lambda rid, exc: seen.append((rid, exc)), **kw,
        )

        for _ in range(n):
            executor.execute(requests(profile, 0x0C, 0x0B))

        return profile, client, session, executor, seen

    def test_three_strikes_retire_and_the_fourth_cycle_sends_nothing(self):
        _, client, session, executor, seen = self.run_cycles(5)

        self.assertEqual(session.retired, {0x0B})
        #: the refused batch, three single-PID attempts, then silence
        self.assertEqual(len(client.sent_to(0x0B)), 4)

        st = executor.stats()["obd.mode01.0B"]
        self.assertEqual(st["scheduled"], 5)
        self.assertEqual(st["submitted"], 3)
        self.assertEqual(st["skipped_retired"], 2)
        self.assertEqual(st["exchanges"], 4)
        self.assertEqual(st["failed"], 4)
        self.assertEqual(st["negative_response"], 1)
        self.assertEqual(st["timeout"], 3)
        self.assertEqual(st["no_response"], 0)
        self.assertEqual(st["state"], "retired")
        #: the healthy request beside it is unaffected
        self.assertEqual(executor.stats()["obd.mode01.0C"]["ok"], 5)

    def test_the_faults_keep_their_own_kind_and_retirement_is_reported_once(self):
        _, _, _, _, seen = self.run_cycles(5)
        for_0b = [(rid, fault_kind(exc)) for rid, exc in seen
                  if rid == "obd.mode01.0B"]

        #: the batch refusal on cycle one is a negative response on this
        #: member too; then three timeouts, then the retirement, once
        self.assertEqual(
            for_0b,
            [("obd.mode01.0B", "negative_response")]
            + [("obd.mode01.0B", "transport_timeout")] * 3
            + [("obd.mode01.0B", "retired")],
        )
        retired = [exc for _, exc in seen if isinstance(exc, Retired)][0]
        self.assertEqual(retired.detail(), {"pid": 0x0B, "strikes": 3})
        #: the batch refusal on the first cycle is a negative response
        #: on BOTH members, not something the OBD path absorbed
        self.assertIn(
            ("obd.mode01.0C", "negative_response"),
            [(rid, fault_kind(exc)) for rid, exc in seen],
        )

    def test_a_dead_link_propagates_instead_of_retiring_the_pid(self):
        """
        Before #16 the OBD path caught every HsfzError, link errors
        included: a dead socket was retried PID by PID, each failure was
        a strike, and the whole OBD set retired against a link that
        needed reconnecting.
        """
        profile, client, session, executor = build(
            data={}, missing=lambda pid: live.HsfzLinkError(
                "connection closed", "closed"
            ),
        )

        with self.assertRaises(live.HsfzLinkError):
            executor.execute(requests(profile, 0x0C, 0x0B))

        self.assertEqual(session.retired, set())
        self.assertEqual(session.fails, {})


class SetupFramesAreNotThePoll(unittest.TestCase):
    """An F303 read is two setup frames plus one poll: 3 on the wire."""

    def setUp(self):
        from tests.test_executor_correlation import DYNAMIC, RecordingTransport

        mapping = load_text(DYNAMIC, "test")
        self.profile = MappingRegistry([mapping]).resolve(
            AllCapabilities(), config={}
        )
        self.transport = RecordingTransport()
        self.executor = MappingExecutor(self.profile, transport=self.transport)

    def test_first_read_arms_then_polls(self):
        self.executor.execute([self.profile.request("oil")])

        self.assertEqual(len(self.transport.calls), 3)
        st = self.executor.stats()["oil"]
        self.assertEqual(st["submitted"], 1)
        self.assertEqual(st["setup_tx_frames"], 2)
        self.assertEqual(st["setup_rx_frames"], 2)
        self.assertEqual(st["tx_frames"], 1)
        self.assertEqual(st["rx_frames"], 1)
        self.assertEqual(st["exchanges"], 1)
        self.assertEqual(st["positive_response"], 1)

        wire = self.executor.wire_stats()
        self.assertEqual(wire["setup_tx_frames"], 2)
        self.assertEqual(wire["exchanges"], 1)
        self.assertEqual(wire["tx_frames"], 1)

    def test_a_repeated_read_polls_without_re_arming(self):
        for _ in range(3):
            self.executor.execute([self.profile.request("oil")])

        st = self.executor.stats()["oil"]
        self.assertEqual(st["setup_tx_frames"], 2)
        self.assertEqual(st["tx_frames"], 3)
        self.assertEqual(self.executor.wire_stats()["tx_frames"], 3)

    def test_a_fault_during_setup_is_marked_as_such(self):
        self.transport.fail_next = lambda payload: payload[0] == 0x2C

        self.executor.execute([self.profile.request("oil")])

        st = self.executor.stats()["oil"]
        self.assertEqual(st["setup_tx_frames"], 1)
        self.assertEqual(st["setup_rx_frames"], 0)
        self.assertEqual(st["setup_faults"], 1)
        #: the poll never went out
        self.assertEqual(st["tx_frames"], 0)
        self.assertEqual(st["timeout"], 1)
        self.assertEqual(st["failed"], 1)

    def test_a_negative_response_is_a_received_frame_with_a_latency(self):
        from bmwdiag.errors import NegativeResponse

        class Refusing:
            def request(self, payload, *, dst, timeout=None, expect=None):
                raise NegativeResponse(0x22, 0x31)

        executor = MappingExecutor(self.profile, transport=Refusing())
        executor.execute([self.profile.request("plain")])

        st = executor.stats()["plain"]
        self.assertEqual(st["tx_frames"], 1)
        self.assertEqual(st["rx_frames"], 1)
        self.assertEqual(st["negative_response"], 1)
        self.assertIsNotNone(st["last_rx"])
        self.assertEqual(st["latency_ms"]["n"], 1)

    def test_a_timeout_receives_nothing(self):
        self.transport.fail_next = lambda payload: payload[0] == 0x22
        self.executor.execute([self.profile.request("plain")])

        st = self.executor.stats()["plain"]
        self.assertEqual(st["tx_frames"], 1)
        self.assertEqual(st["rx_frames"], 0)
        self.assertEqual(st["timeout"], 1)
        self.assertIsNone(st["last_rx"])
        self.assertIsNone(st["latency_ms"])


class PositiveButNothingUsable(unittest.TestCase):
    """The case a request counter cannot show: answered, decoded, all
    rejected by quality."""

    def test_a_sentinel_answer_is_counted_as_all_rejected(self):
        from tests.test_mapping_requests import FakeObdReader

        profile = engine_profile()
        executor = MappingExecutor(
            profile, obd_reader=FakeObdReader({0x24: b"\xff\xff\x00\x00"})
        )
        readings = executor.execute_readings(requests(profile, 0x24))

        self.assertEqual(readings["lambda"].quality, SENTINEL)
        st = executor.stats()["obd.mode01.24"]
        self.assertEqual(st["positive_response"], 1)
        self.assertEqual(st["ok"], 1)
        self.assertEqual(st["failed"], 0)
        self.assertEqual(st["decoded_signals"], 1)
        self.assertEqual(st["accepted_signals"], 0)
        self.assertEqual(st["all_rejected"], 1)
        self.assertEqual(st["last_rejection"], [SENTINEL])

    def test_a_usable_answer_is_accepted(self):
        from tests.test_mapping_requests import FakeObdReader

        profile = engine_profile()
        executor = MappingExecutor(
            profile, obd_reader=FakeObdReader({0x24: b"\x80\x00\x00\x00"})
        )
        readings = executor.execute_readings(requests(profile, 0x24))

        self.assertEqual(readings["lambda"].quality, OK)
        st = executor.stats()["obd.mode01.24"]
        self.assertEqual(st["accepted_signals"], 1)
        self.assertEqual(st["all_rejected"], 0)
        self.assertIsNone(st["last_rejection"])


class FailedObdRequestsReachTheErrorStream(unittest.TestCase):
    """
    The same path as UDS faults: `on_error` -> `Recorder.error` ->
    the `errors` table -> `channel_errors` in the lake. With the real
    kind, and the retirement as its own row.
    """

    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "rec.db")
        self.rec = live.Recorder(self.db)
        self.rec.open()
        self.rec.set_metadata(engine_profile())
        self.rec.start_run("VINREDACTED", "gw", "DDE", 0x12)
        time.sleep(0.05)

    def tearDown(self):
        try:
            self.rec.close()
        except Exception:
            pass

    def rows(self):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(
                "SELECT request_id, kind, detail FROM errors ORDER BY rowid"
            ).fetchall()
        finally:
            con.close()

    def test_timeouts_and_the_retirement_are_stored_with_their_kind(self):
        def note_fault(request_id, exc):
            self.rec.error(
                request_id, fault_kind(exc), str(exc),
                live.fault_detail(exc),
            )

        profile, _, _, executor = build(
            data={0x0C: SAMPLE[0x0C]}, on_error=note_fault,
        )

        for _ in range(5):
            executor.execute(requests(profile, 0x0C, 0x0B))

        time.sleep(0.3)
        self.rec.close()
        rows = [r for r in self.rows() if r[0] == "obd.mode01.0B"]

        #: cycle 1: the batch times out on the missing PID (a fault on
        #: both members, no strike), then the single read times out
        #: (strike 1); cycles 2-3: strikes 2 and 3; then the retirement
        self.assertEqual(
            [k for _, k, _ in rows],
            ["transport_timeout"] * 4 + ["retired"],
        )
        self.assertEqual(json.loads(rows[-1][2]), {"pid": 11, "strikes": 3})
        #: and nothing for the two cycles it was not asked
        self.assertEqual(len(rows), 5)


class EffectiveRefreshIsMeasured(unittest.TestCase):
    """
    The declared period of a staggered class is the gap between firings
    of the class; a member refreshes every period x members. The report
    now MEASURES it from the acquisition clock instead of deriving it.
    """

    def setUp(self):
        from tests.test_polling_stagger import STAGGERED

        self.mapping = load_text(STAGGERED, source="<stagger>")
        self.profile = MappingRegistry([self.mapping]).resolve(
            AllCapabilities(), config={}
        )
        self.plan = PollingPlan(
            self.profile.requests,
            resolve_classes(self.mapping.polling_classes),
        )

        class Answering:
            def request(self, payload, *, dst, timeout=None, expect=None):
                return bytes([0x62, payload[1], payload[2], 0x00, 0x2A])

        self.executor = MappingExecutor(self.profile, transport=Answering())

    def drive(self, cycles, rate=0.1):
        import bmwdiag.mapping.execute as execute

        class Clock:
            now = 1000.0

            @staticmethod
            def monotonic():
                return Clock.now

            @staticmethod
            def time():
                return Clock.now + 1.7e9

        real = execute.time
        execute.time = Clock

        try:
            for cycle in range(cycles):
                Clock.now = 1000.0 + cycle * rate
                self.executor.execute(self.plan.due(cycle, Clock.now))
        finally:
            execute.time = real

    def test_a_staggered_member_refreshes_every_period_times_members(self):
        self.drive(60)
        stats = self.executor.stats()

        #: grp declares 0.2 s with three members: each refreshes every
        #: 0.6 s, and the measured interval says so
        for rid in ("a", "b", "c"):
            self.assertAlmostEqual(stats[rid]["refresh_s"]["median"], 0.6)
            self.assertAlmostEqual(stats[rid]["refresh_s"]["avg"], 0.6)

        self.assertAlmostEqual(stats["fastone"]["refresh_s"]["median"], 0.1)

    def test_the_report_carries_measured_beside_declared(self):
        self.drive(60)
        diag = live.Diagnostics()
        diag.publish(profile=self.profile, executor=self.executor,
                     plan=self.plan)
        rows = {r["id"]: r for r in diag.report()["requests"]}

        self.assertAlmostEqual(rows["a"]["period_s"], 0.6)
        self.assertAlmostEqual(rows["a"]["refresh_s"]["median"], 0.6)
        self.assertEqual(rows["a"]["state"], "active")

    def test_a_pause_shows_up_as_one_long_interval_not_a_shifted_median(self):
        """
        `sampling` mode goes quiet for minutes at a time. The median of
        the last window is what the channel does while it is polling;
        `last` is the pause. Neither is hidden by the other.
        """
        self.drive(30)
        import bmwdiag.mapping.execute as execute

        class Later:
            @staticmethod
            def monotonic():
                return 1000.0 + 30 * 0.1 + 600.0

            @staticmethod
            def time():
                return 2.0e9

        real = execute.time
        execute.time = Later
        try:
            self.executor.execute([self.profile.request("fastone")])
        finally:
            execute.time = real

        refresh = self.executor.stats()["fastone"]["refresh_s"]
        self.assertAlmostEqual(refresh["median"], 0.1)
        self.assertGreater(refresh["last"], 599.0)


class LatencyIsCheapAndBounded(unittest.TestCase):
    def test_p95_is_over_the_recent_window_and_avg_over_the_session(self):
        from bmwdiag.mapping.execute import LATENCY_WINDOW, _Series

        series = _Series(LATENCY_WINDOW)

        for _ in range(100):
            series.push(1.0)

        for _ in range(LATENCY_WINDOW):
            series.push(10.0)

        summary = series.summary()
        self.assertEqual(summary["n"], 100 + LATENCY_WINDOW)
        self.assertEqual(summary["window"], LATENCY_WINDOW)
        self.assertEqual(summary["p95"], 10.0)
        self.assertEqual(summary["median"], 10.0)
        self.assertLess(summary["avg"], 4.0)

    def test_p95_of_a_short_window(self):
        from bmwdiag.mapping.execute import _Series

        series = _Series(32)
        for value in (5.0, 1.0, 3.0, 2.0, 4.0):
            series.push(value)

        self.assertEqual(series.summary()["p95"], 5.0)
        self.assertEqual(series.summary()["median"], 3.0)
        self.assertIsNone(_Series(4).summary())


class TheReportSaysWhereEveryRequestStands(unittest.TestCase):
    def test_stages_state_and_wire_totals(self):
        seen = []
        profile, _, _, executor = build(
            data={0x0C: SAMPLE[0x0C]}, multi=False,
            on_error=lambda rid, exc: seen.append(rid),
        )

        for _ in range(4):
            executor.execute(requests(profile, 0x0C, 0x0B))

        diag = live.Diagnostics()
        diag.publish(profile=profile, executor=executor)
        report = diag.report()
        rows = {r["id"]: r for r in report["requests"]}

        dead = rows["obd.mode01.0B"]
        self.assertEqual(dead["state"], "retired")
        self.assertEqual(dead["stages"]["scheduled"], 4)
        self.assertEqual(dead["stages"]["submitted"], 3)
        self.assertEqual(dead["stages"]["skipped_retired"], 1)
        self.assertEqual(dead["stages"]["timeout"], 3)
        self.assertEqual(dead["stages"]["wire"]["tx_frames"], 4)   # 1 batch + 3
        self.assertEqual(dead["stages"]["wire"]["rx_frames"], 1)   # the NRC
        self.assertIsNone(dead["stages"]["persisted_signals"])     # not recording
        self.assertIsNotNone(dead["last_tx_age"])
        self.assertIsNotNone(dead["last_rx_age"])                  # the NRC

        live_row = rows["obd.mode01.0C"]
        self.assertEqual(live_row["state"], "active")
        self.assertEqual(live_row["stages"]["positive_response"], 4)
        self.assertEqual(live_row["stages"]["accepted_signals"], 4)
        self.assertIsNotNone(live_row["latency_ms"])

        #: physical: 1 refused batch + 4 singles for 0x0C + 3 for 0x0B
        self.assertEqual(report["totals"]["wire"]["exchanges"], 8)
        self.assertEqual(report["totals"]["scheduled"], 8)
        self.assertEqual(report["totals"]["submitted"], 7)
        self.assertEqual(report["totals"]["skipped_retired"], 1)
        self.assertIsNone(report["totals"]["recorder"])

        #: an unpolled request is idle, not failing
        self.assertEqual(rows["obd.mode01.0D"]["state"], "idle")

    def test_persisted_counts_come_from_the_recorder(self):
        from tests.test_mapping_requests import FakeObdReader

        db = os.path.join(tempfile.mkdtemp(), "rec.db")
        rec = live.Recorder(db)
        rec.open()
        profile = engine_profile()
        rec.set_metadata(profile)
        rec.start_run("VINREDACTED", "gw", "DDE", 0x12)
        executor = MappingExecutor(
            profile, obd_reader=FakeObdReader({0x0C: SAMPLE[0x0C]})
        )

        try:
            for _ in range(3):
                readings, stamps = executor.execute_readings_at(
                    requests(profile, 0x0C)
                )
                rec.write(
                    time.time(),
                    {k: r.value for k, r in readings.items()},
                    {k: r.quality for k, r in readings.items()},
                    stamps,
                )

            deadline = time.time() + 5.0

            while rec.persisted().get("rpm", 0) < 3 and time.time() < deadline:
                time.sleep(0.05)

            diag = live.Diagnostics()
            diag.publish(profile=profile, executor=executor, recorder=rec)
            report = diag.report()
        finally:
            rec.close()

        row = next(r for r in report["requests"] if r["id"] == "obd.mode01.0C")
        self.assertEqual(row["stages"]["decoded_signals"], 3)
        self.assertEqual(row["stages"]["accepted_signals"], 3)
        self.assertEqual(row["stages"]["persisted_signals"], 3)
        rpm = next(c for c in report["channels"] if c["key"] == "rpm")
        self.assertEqual(rpm["persisted"], 3)
        self.assertEqual(report["totals"]["recorder"]["rows"], 3)
        self.assertEqual(report["totals"]["persisted_signals"], 3)


class AReaderWithoutAReportStillCounts(unittest.TestCase):
    """The test fakes and any minimal reader: one answered exchange per
    read, carrying everything asked."""

    def test_missing_pids_are_no_response_and_the_read_is_one_exchange(self):
        from tests.test_mapping_requests import FakeObdReader

        profile = engine_profile()
        executor = MappingExecutor(
            profile, obd_reader=FakeObdReader({0x0C: SAMPLE[0x0C]})
        )
        executor.execute(requests(profile, 0x0C, 0x0B))

        self.assertEqual(executor.wire_stats()["exchanges"], 1)
        st = executor.stats()["obd.mode01.0B"]
        self.assertEqual(st["no_response"], 1)
        self.assertEqual(st["rx_frames"], 1)

    def test_the_report_types_are_plain_data(self):
        report = ObdReadReport(exchanges=[ObdExchange((0x0C,), 1.0, 1.1)])

        self.assertTrue(report.exchanges[0].answered)
        self.assertEqual(report.retired, set())


if __name__ == "__main__":
    unittest.main()
