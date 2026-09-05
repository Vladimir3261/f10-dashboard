"""
Per-request faults, recorded rather than discarded.

Before this, a request that timed out or was refused left no trace at all -
`on_error` was never wired to anything. That made a failing channel and an
unasked one indistinguishable: both simply have no rows in `samples`, so
"how often does this channel actually fail?" had no answer.

Covers the whole path offline: classification -> SQLite -> sync agent ->
ingest builder.
"""

import os
import sqlite3
import sys
import tempfile
import time
import unittest

from tests import support  # noqa: F401

import live
from bmwdiag.mapping import fault_kind
from bmwdiag.mapping.execute import TRANSPORT_FAULT_BUDGET
from bmwdiag.mapping.errors import DecodeError
from bmwdiag.protocol import NegativeResponse

sys.path.insert(0, os.path.join(support.ROOT, "infra"))
from sync import agent as sync_agent          # noqa: E402
from ingest import server as ingest_server    # noqa: E402
from common import wire                       # noqa: E402


class HsfzError(Exception):
    pass


class HsfzNack(HsfzError):
    pass


class Classification(unittest.TestCase):
    """A kind is data you can GROUP BY; a message is prose that changes."""

    def test_each_fault_gets_a_stable_kind(self):
        cases = [
            (HsfzNack("will not route to 0x18"), "transport_nack"),
            (TimeoutError("HSFZ read timeout"), "transport_timeout"),
            (ConnectionResetError("reset"), "transport_link"),
            (BrokenPipeError("pipe"), "transport_link"),
            (DecodeError("bad", "file", "path"), "decode"),
            #: the ECU answered and refused - since 2026-09-05 its own
            #: kind, not "other", because "it does not do this" and "a
            #: bug" are different things to group by
            (NegativeResponse(0x22, 0x31), "negative_response"),
            (ValueError("something else"), "other"),
        ]

        for exc, expected in cases:
            self.assertEqual(fault_kind(exc), expected, exc)


class Recording(unittest.TestCase):
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
                "SELECT request_id, kind, message FROM errors ORDER BY rowid"
            ).fetchall()
        finally:
            con.close()

    def test_a_fault_is_stored_with_its_request_and_kind(self):
        self.rec.error("egs.selector.DA2E", "transport_nack", "will not route")
        time.sleep(0.3)
        self.rec.close()

        self.assertEqual(
            self._rows(), [("egs.selector.DA2E", "transport_nack", "will not route")]
        )

    def test_a_long_message_is_truncated(self):
        """A fault storm must not be able to bloat the database."""
        self.rec.error("r", "other", "x" * 5000)
        time.sleep(0.3)
        self.rec.close()

        self.assertLessEqual(len(self._rows()[0][2]), 500)


class TheWiring(unittest.TestCase):
    """
    Executor fault -> on_error -> Recorder.error -> SQLite, end to end.

    The gap f10pi found while verifying session 9: the tests below cover
    recorder -> SQLite -> agent -> ingest, and nothing covered the hop
    that was actually MISSING in the first place. `on_error` existed on
    the executor and `live.py` simply never passed anything to it, so
    every fault was discarded - and a test suite that starts at the
    recorder cannot see that.

    The errors table was also reported as "right shape, zero rows,
    untested rather than proven" after a 100%-healthy drive. A fault
    injected at the transport proves it offline, without waiting for the
    car to misbehave.
    """

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

    def _profile(self, target="0x18"):
        from bmwdiag.mapping import load_text, MappingRegistry
        from bmwdiag.mapping.registry import AllCapabilities

        mapping = load_text(
            "schema_version: 1\n"
            "mapping: {id: t, version: 1, production: false}\n"
            f"ecu: {{target: {target}}}\n"
            "requests:\n"
            "  probe:\n"
            "    protocol: uds\n"
            "    service: 0x22\n"
            "    did: 0xDA2E\n"
            "    response: {data_length: 2}\n"
            "    signals:\n"
            "      g: {label: G, unit: '', decode: {type: uint8}}\n",
            "test",
        )

        return MappingRegistry([mapping]).resolve(AllCapabilities())

    def _rows(self):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(
                "SELECT request_id, kind, message FROM errors ORDER BY rowid"
            ).fetchall()
        finally:
            con.close()

    def test_a_transport_fault_reaches_the_database(self):
        """This is the hop that was missing, and it is the whole point."""
        class Refusing:
            def request(self, payload, *, dst, timeout=None, expect=None):
                raise HsfzNack("gateway will not route to 0x18")

        profile = self._profile()
        executor = live.MappingExecutor(
            profile,
            transport=Refusing(),
            on_error=lambda rid, exc: self.rec.error(
                rid, live.fault_kind(exc), str(exc)
            ),
        )

        executor.execute(profile.requests)
        time.sleep(0.3)
        self.rec.close()

        rows = self._rows()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "probe")
        self.assertEqual(rows[0][1], "transport_nack")
        self.assertIn("will not route", rows[0][2])

    def test_a_timeout_is_recorded_under_its_own_kind(self):
        """
        Kinds must stay distinguishable: an ECU that did not answer and a
        gateway that refused to route are different diagnoses, and the
        whole reason for a `kind` column is to GROUP BY it later.
        """
        class Silent:
            def request(self, payload, *, dst, timeout=None, expect=None):
                raise TimeoutError("no answer in 0.4s")

        profile = self._profile()
        executor = live.MappingExecutor(
            profile,
            transport=Silent(),
            on_error=lambda rid, exc: self.rec.error(
                rid, live.fault_kind(exc), str(exc)
            ),
        )

        executor.execute(profile.requests)
        time.sleep(0.3)
        self.rec.close()

        self.assertEqual(self._rows()[0][1], "transport_timeout")

    def test_a_negative_response_is_recorded_skipped_and_not_fatal(self):
        """
        The ECU answering `7F 22 31` is the one fault that proves the link
        works: the request got there and the refusal came back. PR #32's
        first cut labelled it `negative_response` but left the executor
        treating anything unrecognised as a dead link - so an NRC tore the
        link down, split the run, and (re-raised before `_note`) recorded
        nothing. Skip it, record it, and never let it spend link budget.
        """
        class Refusing:
            calls = 0

            def request(self, payload, *, dst, timeout=None, expect=None):
                self.calls += 1
                raise live.HsfzNegativeResponse(0x22, 0x31)

        profile = self._profile()
        transport = Refusing()
        executor = live.MappingExecutor(
            profile,
            transport=transport,
            on_error=lambda rid, exc: self.rec.error(
                rid, live.fault_kind(exc), str(exc)
            ),
        )

        for _ in range(TRANSPORT_FAULT_BUDGET + 2):
            executor.execute(profile.requests)        # must not raise

        time.sleep(0.3)
        self.rec.close()

        rows = self._rows()
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "probe")
        self.assertEqual(rows[0][1], "negative_response")
        self.assertIn("NRC 0x31", rows[0][2])
        self.assertEqual(executor._transport_faults, 0)

    def test_a_healthy_exchange_records_nothing(self):
        """
        Zero rows must mean "nothing failed", not "nothing is wired". The
        two were indistinguishable before, which is exactly why a
        100%-healthy drive left this untested.
        """
        class Answering:
            def request(self, payload, *, dst, timeout=None, expect=None):
                return b"\x62\xda\x2e\x00\x03"

        profile = self._profile()
        executor = live.MappingExecutor(
            profile,
            transport=Answering(),
            on_error=lambda rid, exc: self.rec.error(
                rid, live.fault_kind(exc), str(exc)
            ),
        )

        executor.execute(profile.requests)
        time.sleep(0.3)
        self.rec.close()

        self.assertEqual(self._rows(), [])

    def test_live_py_actually_wires_it(self):
        """
        The executor is only asked for faults if someone passes on_error.
        It was constructed without one for weeks. Asserted structurally,
        the same way the metadata ordering is: reaching this line at
        runtime needs a gateway and an ECU scan.
        """
        import ast

        tree = ast.parse(open(
            os.path.join(support.ROOT, "live.py"), encoding="utf-8"
        ).read())
        loop = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "poll_loop"
        )
        built = [
            call for call in ast.walk(loop)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "MappingExecutor"
        ]

        self.assertTrue(built, "poll_loop builds no MappingExecutor")

        for call in built:
            self.assertIn(
                "on_error", [kw.arg for kw in call.keywords],
                "the executor is built without on_error, so every fault "
                "is discarded",
            )


class Shipping(unittest.TestCase):
    def _db_with_errors(self, path, include_table=True):
        con = sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL,"
            " ended_at REAL, vin TEXT, gateway TEXT, ecu TEXT, ecu_addr INTEGER);"
        )
        con.execute("INSERT INTO runs(id,started_at,vin) VALUES(1,1.0,'V')")

        if include_table:
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

    def test_the_agent_reads_faults(self):
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        self._db_with_errors(db)

        rows = sync_agent.read_errors(db, 0, 100)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["request_id"], "egs.selector.DA2E")
        self.assertEqual(rows[0]["kind"], "transport_nack")

    def test_a_database_without_the_table_is_empty_not_an_error(self):
        """Databases recorded before fault logging existed must still sync."""
        db = os.path.join(tempfile.mkdtemp(), "old.db")
        self._db_with_errors(db, include_table=False)

        self.assertEqual(sync_agent.read_errors(db, 0, 100), [])

    def test_the_ingest_server_builds_rows_for_clickhouse(self):
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        self._db_with_errors(db)
        rows = sync_agent.read_errors(db, 0, 100)

        batch = wire.columnar(
            "channel_errors",
            [{k: v for k, v in r.items() if k != "_rowid"} for r in rows],
            meta={"db": "t.db"},
        )
        built = ingest_server.build_channel_errors(batch)

        self.assertEqual(built[0]["request_id"], "egs.selector.DA2E")
        self.assertEqual(built[0]["kind"], "transport_nack")
        self.assertIn("channel_errors", ingest_server.BUILDERS)

    def test_the_batch_survives_the_wire_format(self):
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        self._db_with_errors(db)
        rows = sync_agent.read_errors(db, 0, 100)
        batch = wire.columnar(
            "channel_errors",
            [{k: v for k, v in r.items() if k != "_rowid"} for r in rows],
        )

        back = wire.decode(wire.encode(batch))

        self.assertEqual(
            wire.rows_of(back)[0]["request_id"], "egs.selector.DA2E"
        )


if __name__ == "__main__":
    unittest.main()
