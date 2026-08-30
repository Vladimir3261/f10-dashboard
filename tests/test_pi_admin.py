"""
The Pi admin panel.

This is the most privileged surface in the repository: it can reboot the
host and make it fetch and run new code. So most of what is tested here
is what it REFUSES, and those tests matter more than the features.

Runs against a real HTTP server on loopback. No Pi, no systemd, no
network - every command the panel would run is replaced with a recorder,
so a test can assert exactly what argv would have been executed without
anything actually happening.
"""

import base64
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from tests import support  # noqa: F401

sys.path.insert(
    0, os.path.join(support.ROOT, "hardware", "raspberry-pi", "admin")
)
import server as admin                                   # noqa: E402


USER, PASSWORD = "f10", "correct-horse-battery-staple"


def auth_header(user=USER, password=PASSWORD):
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()

    return {"Authorization": "Basic " + raw}


class FakeRun:
    """Records argv instead of running it."""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}

    def __call__(self, argv, timeout=30.0):
        self.calls.append(list(argv))

        for key, value in self.replies.items():
            if key in " ".join(argv):
                return value

        return 0, ""

    def ran(self, *fragment):
        joined = [" ".join(c) for c in self.calls]

        return any(all(f in c for f in fragment) for c in joined)


class AdminCase(unittest.TestCase):
    config = {}

    def setUp(self):
        self.fake = FakeRun(getattr(self, "replies", None))
        self._real_run = admin.run
        admin.run = self.fake

        cfg = dict(admin.DEFAULTS)
        cfg.update({
            "username": USER,
            "password": PASSWORD,
            "repo_dir": support.ROOT,
            "git_remote": "git@example.invalid:owner/repo.git",
            "services": ["f10-dashboard", "f10-sync"],
            #: Unroutable, so a stray status call cannot hang on a real
            #: socket if the test host happens to run something on 8091.
            "sync_status_url": "http://127.0.0.1:9/nope",
        })
        cfg.update(self.config)
        self.cfg = cfg

        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), admin.make_handler(cfg)
        )
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        #: serve_forever polls at 0.5s by default, and shutdown() waits
        #: for the next poll - which made teardown, not the tests, most
        #: of this module's runtime.
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()

    def tearDown(self):
        admin.run = self._real_run
        self.server.shutdown()
        self.server.server_close()

    # -- helpers ----------------------------------------------------

    def get(self, path, headers=None):
        head = dict(headers if headers is not None else auth_header())
        #: The server speaks HTTP/1.1, so without this each request
        #: leaves a keep-alive socket that tearDown then waits on -
        #: ~0.4s per test, which is most of the suite's runtime.
        head["Connection"] = "close"

        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", headers=head
        )

        return urllib.request.urlopen(request, timeout=5)

    def post(self, action, body=None, headers=None, csrf=True):
        head = dict(headers if headers is not None else auth_header())
        head["Content-Type"] = "application/json"
        head["Connection"] = "close"

        if csrf:
            head[admin.CSRF_HEADER] = "1"

        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/action/{action}",
            data=json.dumps(body or {}).encode(),
            headers=head,
            method="POST",
        )

        return urllib.request.urlopen(request, timeout=5)

    def assert_status(self, code, call, *args, **kwargs):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            call(*args, **kwargs)

        self.assertEqual(caught.exception.code, code)

        #: 401 is text/plain (it is a browser-facing challenge); the
        #: action errors are JSON. Tolerate both.
        raw = caught.exception.read() or b"{}"

        try:
            return json.loads(raw)
        except ValueError:
            return {"error": raw.decode("utf-8", "replace")}


class Authentication(AdminCase):
    def test_no_credentials_is_401(self):
        self.assert_status(401, self.get, "/", headers={})

    def test_wrong_password_is_401(self):
        self.assert_status(
            401, self.get, "/", headers=auth_header(password="wrong")
        )

    def test_wrong_user_is_401(self):
        self.assert_status(
            401, self.get, "/", headers=auth_header(user="root")
        )

    def test_garbage_authorization_header_is_401_not_a_crash(self):
        for header in ("Basic !!!!", "Basic", "Bearer abc", "Basic " + "A" * 9):
            with self.subTest(header=header):
                self.assert_status(
                    401, self.get, "/", headers={"Authorization": header}
                )

    def test_correct_credentials_get_the_page(self):
        body = self.get("/").read().decode()

        self.assertIn("<title>F10 Pi</title>", body)

    def test_the_401_offers_basic_so_a_browser_prompts(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/", headers={})

        self.assertIn(
            "Basic", caught.exception.headers.get("WWW-Authenticate", "")
        )

    def test_actions_need_credentials_too(self):
        self.assert_status(
            401, self.post, "reboot", {"confirm": True}, headers={}
        )
        self.assertEqual(self.fake.calls, [], "an action ran while unauthorised")

    def test_health_check_needs_none_and_leaks_nothing(self):
        """A watchdog should not have to hold the panel password."""
        body = self.get("/healthz", headers={}).read().decode()

        self.assertEqual(body.strip(), "ok")


class UnconfiguredFailsClosed(AdminCase):
    """A panel with no password must refuse everyone, not everyone in."""

    config = {"username": "", "password": ""}

    def test_no_credentials_configured_refuses_valid_looking_auth(self):
        self.assert_status(401, self.get, "/")

    def test_no_credentials_configured_refuses_empty_auth(self):
        self.assert_status(
            401, self.get, "/", headers=auth_header(user="", password="")
        )


class CsrfProtection(AdminCase):
    """
    Browsers attach cached Basic credentials automatically, so a page the
    phone has open could otherwise POST here cross-origin.
    """

    def test_a_mutating_request_without_the_header_is_refused(self):
        body = self.assert_status(
            403, self.post, "reboot", {"confirm": True}, csrf=False
        )

        self.assertIn(admin.CSRF_HEADER, body["error"])
        self.assertEqual(self.fake.calls, [])

    def test_reading_status_does_not_need_it(self):
        self.assertEqual(self.get("/api/status").status, 200)


class Confirmation(AdminCase):
    def test_destructive_actions_need_an_explicit_confirm(self):
        for action in sorted(admin.DESTRUCTIVE):
            with self.subTest(action=action):
                self.assert_status(400, self.post, action, {})

        self.assertEqual(
            self.fake.calls, [], "something ran without confirmation"
        )

    def test_every_action_that_changes_the_host_is_marked_destructive(self):
        """
        A new action must be classified deliberately. `logs` is the only
        read-only one; everything else restarts, reboots or runs code.
        """
        self.assertEqual(
            set(admin.ACTIONS) - admin.DESTRUCTIVE, {"logs"}
        )


class UnitAllowlist(AdminCase):
    """The request must never be able to name an arbitrary systemd unit."""

    def test_restarting_an_unlisted_unit_is_refused(self):
        body = self.assert_status(
            409, self.post, "restart", {"unit": "ssh", "confirm": True}
        )

        self.assertIn("unknown unit", body["error"])
        self.assertEqual(self.fake.calls, [])

    def test_a_listed_unit_restarts(self):
        self.post("restart", {"unit": "f10-dashboard", "confirm": True})

        self.assertTrue(
            self.fake.ran("sudo", "systemctl", "restart", "f10-dashboard")
        )

    def test_start_and_stop_are_the_only_verbs(self):
        for verb in ("mask", "disable", "kill", ""):
            with self.subTest(verb=verb):
                self.assert_status(
                    409, self.post, "service",
                    {"unit": "f10-sync", "verb": verb, "confirm": True},
                )

        self.assertEqual(self.fake.calls, [])

    def test_logs_are_restricted_to_listed_units_as_well(self):
        self.assert_status(
            409, self.post, "logs", {"unit": "sshd"}
        )

    def test_nothing_is_ever_run_through_a_shell(self):
        """
        Every command is a fixed argv list. If a value from the request
        ever reached a shell string, this is where it would show.
        """
        self.post("restart", {"unit": "f10-dashboard", "confirm": True})
        self.post("logs", {"unit": "f10-sync"})

        for call in self.fake.calls:
            self.assertIsInstance(call, list)

            for part in call:
                self.assertNotIn(";", part)
                self.assertNotIn("&&", part)
                self.assertNotIn("|", part)


class GitRemoteIsPinned(AdminCase):
    """
    `pull` makes the Pi execute new code. The remote it pulls from is the
    trust boundary, so it is verified on every pull rather than assumed.
    """

    replies = {
        "remote get-url": (0, "git@example.invalid:owner/repo.git"),
        "rev-parse --short": (0, "abc1234"),
        "merge --ff-only": (0, "Fast-forward"),
    }

    def test_a_matching_remote_pulls(self):
        body = json.loads(self.post("pull", {"confirm": True}).read())

        self.assertTrue(body["ok"])
        self.assertTrue(self.fake.ran("git", "fetch"))
        self.assertTrue(self.fake.ran("merge", "--ff-only"))

    def test_pull_does_not_restart_the_runtime_by_itself(self):
        """
        Two decisions, not one: you may want the code staged while the
        current drive keeps recording.
        """
        self.post("pull", {"confirm": True})

        self.assertFalse(self.fake.ran("systemctl", "restart"))

    def test_it_only_fast_forwards(self):
        """A diverged checkout is not something a phone should resolve."""
        self.post("pull", {"confirm": True})

        self.assertTrue(
            any("--ff-only" in c for c in (" ".join(x) for x in self.fake.calls))
        )
        self.assertFalse(self.fake.ran("reset", "--hard"))
        self.assertFalse(self.fake.ran("checkout"))


class GitRemoteMismatch(AdminCase):
    replies = {"remote get-url": (0, "git@evil.invalid:someone/else.git")}

    def test_a_repointed_remote_refuses_to_pull(self):
        body = self.assert_status(409, self.post, "pull", {"confirm": True})

        self.assertIn("refusing to pull", body["error"])
        self.assertFalse(self.fake.ran("git", "fetch"))
        self.assertFalse(self.fake.ran("merge"))


class GitRemoteUnset(AdminCase):
    config = {"git_remote": ""}

    def test_an_unpinned_panel_refuses_to_pull_at_all(self):
        body = self.assert_status(409, self.post, "pull", {"confirm": True})

        self.assertIn("not configured", body["error"])
        self.assertFalse(self.fake.ran("git", "fetch"))


class PowerActions(AdminCase):
    def test_reboot_and_shutdown_use_absolute_paths_via_sudo(self):
        """
        Absolute paths, because the sudoers allowlist names them that way
        and a relative name could resolve to something on PATH.
        """
        self.post("reboot", {"confirm": True})
        self.post("shutdown", {"confirm": True})

        self.assertTrue(self.fake.ran("sudo", "-n", "/sbin/reboot"))
        self.assertTrue(self.fake.ran("sudo", "-n", "/sbin/poweroff"))

    def test_sudo_never_prompts(self):
        """
        `-n` on every privileged call: a sudo waiting for a password on a
        headless box hangs the request until it times out.
        """
        self.post("reboot", {"confirm": True})

        for call in self.fake.calls:
            if call and call[0] == "sudo":
                self.assertEqual(call[1], "-n")


class Status(AdminCase):
    def test_status_reports_the_configured_services(self):
        body = json.loads(self.get("/api/status").read())

        self.assertEqual(
            [s["unit"] for s in body["services"]],
            ["f10-dashboard", "f10-sync"],
        )

    def test_status_survives_a_missing_sync_agent(self):
        """The agent being down is normal, not an error."""
        body = json.loads(self.get("/api/status").read())

        self.assertFalse(body["sync"]["reachable"])

    def test_status_does_not_hit_the_network_for_git(self):
        """
        A status poll runs every few seconds over a mobile link. Fetching
        there would make the page cost data and stall.
        """
        self.get("/api/status")

        self.assertFalse(self.fake.ran("git", "fetch"))

    def test_unknown_paths_are_404(self):
        self.assert_status(404, self.get, "/etc/passwd")
        self.assert_status(404, self.post, "nonsense", {"confirm": True})


class BindRefusesWildcard(unittest.TestCase):
    """
    The Pi joins hotspots and car-park APs. A wildcard bind would offer
    reboot-and-run-code to everyone on the segment, so it is refused at
    startup rather than warned about.
    """

    def _main(self, bind):
        import io
        import contextlib

        cfg = os.path.join(support.ROOT, "hardware", "raspberry-pi",
                           "admin", "config.example.json")
        err = io.StringIO()

        with contextlib.redirect_stderr(err):
            code = admin.main(["--config", cfg, "--bind", bind])

        return code, err.getvalue()

    def test_wildcard_ipv4_is_refused(self):
        code, err = self._main("0.0.0.0")

        self.assertEqual(code, 2)
        self.assertIn("refusing to bind", err)

    def test_wildcard_ipv6_is_refused(self):
        code, err = self._main("::")

        self.assertEqual(code, 2)

    def test_the_default_config_is_not_usable_as_shipped(self):
        """
        config.example.json must never be a working panel: it ships a
        placeholder password, and a Pi that ran it unedited would be
        protected by a password published on GitHub.
        """
        path = os.path.join(support.ROOT, "hardware", "raspberry-pi",
                            "admin", "config.example.json")

        with open(path, encoding="utf-8") as fh:
            example = json.load(fh)

        self.assertEqual(example["password"], "CHANGE-ME")
        self.assertNotIn(example["bind"], ("0.0.0.0", "::"))


class ActionsReportBack(AdminCase):
    """
    Every action has to say what it did.

    The first version put the result in a div at the bottom of the page,
    below the Power card - so on a phone every result landed off-screen
    and an action that worked perfectly looked like it did nothing.
    """

    replies = {
        "remote get-url": (0, "git@example.invalid:owner/repo.git"),
        "rev-parse --short": (0, "abc1234"),
        "log -1": (0, "Some commit subject"),
        "rev-list --count": (0, "3"),
        "merge --ff-only": (0, "Already up to date."),
    }

    def test_an_unchanged_pull_still_says_where_it_is(self):
        """
        "Already up to date" on its own is useless - up to date AT WHAT?
        The revision and subject are what make it a real answer.
        """
        body = json.loads(self.post("pull", {"confirm": True}).read())

        self.assertFalse(body["changed"])
        self.assertEqual(body["after"], "abc1234")
        self.assertEqual(body["subject"], "Some commit subject")
        self.assertEqual(body["commits"], 0)

    def test_every_action_returns_ok_and_its_own_name(self):
        """So the page can always render something specific."""
        for action, body in (
            ("logs", {"unit": "f10-dashboard"}),
            ("restart", {"unit": "f10-dashboard", "confirm": True}),
            ("service", {"unit": "f10-sync", "verb": "stop", "confirm": True}),
            ("pull", {"confirm": True}),
            ("reboot", {"confirm": True}),
            ("shutdown", {"confirm": True}),
        ):
            with self.subTest(action=action):
                result = json.loads(self.post(action, body).read())

                self.assertTrue(result["ok"])
                self.assertEqual(result["action"], action)

    def test_the_page_pins_the_message_to_the_viewport(self):
        """
        Regression on the original bug: the toast must not scroll away
        with the page.
        """
        page = self.get("/").read().decode()

        self.assertIn("#msg { position:fixed", page)


class ChangedPull(AdminCase):
    replies = {
        "remote get-url": (0, "git@example.invalid:owner/repo.git"),
        "rev-parse --short": (0, "abc1234"),
        "rev-list --count": (0, "3"),
        "log -1": (0, "New head subject"),
        "merge --ff-only": (0, "Fast-forward"),
    }

    def test_a_pull_that_moved_reports_how_far(self):
        """
        `before` and `after` come from the same faked command here, so
        this asserts the shape rather than the delta; the count query is
        what tells the user how much arrived.
        """
        body = json.loads(self.post("pull", {"confirm": True}).read())

        self.assertIn("commits", body)
        self.assertIn("subject", body)
        self.assertIn("note", body)


class SessionDeletion(AdminCase):
    """
    The only action that removes data, so the most heavily fenced.

    A session database that has not reached the lake exists in exactly
    one place. Deleting it loses that drive permanently.
    """

    def setUp(self):
        self.sessions = tempfile.mkdtemp()
        self.state = os.path.join(tempfile.mkdtemp(), "sync-state.json")
        self.config = dict(
            getattr(type(self), "config", {}),
            sessions_dir=self.sessions,
            sync_state_file=self.state,
            dashboard_status_url="http://127.0.0.1:9/nope",
        )
        super().setUp()

    def _session(self, name, rows=10, synced_rows=None, age=0):
        """A plausible session db, optionally marked synced."""
        path = os.path.join(self.sessions, name)
        con = sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE samples(run_id INT, ts REAL, param_id INT,"
            " value REAL);"
            "CREATE TABLE runs(id INTEGER PRIMARY KEY, started_at REAL,"
            " ended_at REAL, mode TEXT, clock_synced INT);"
        )
        con.executemany(
            "INSERT INTO samples VALUES(1, ?, 1, 1.0)",
            [(time.time(),) for _ in range(rows)],
        )
        con.commit()
        con.close()

        if age:
            past = time.time() - age
            os.utime(path, (past, past))

        if synced_rows is not None:
            marks = {}

            if os.path.exists(self.state):
                with open(self.state, encoding="utf-8") as fh:
                    marks = json.load(fh)

            marks[path] = {"samples_rowid": synced_rows}

            with open(self.state, "w", encoding="utf-8") as fh:
                json.dump(marks, fh)

        return path

    def test_a_synced_older_session_can_be_deleted(self):
        self._session("drive-new.db", rows=5)                    # active
        self._session("drive-old.db", rows=10, synced_rows=10, age=9000)

        body = json.loads(self.post(
            "delete_session", {"name": "drive-old.db", "confirm": True}
        ).read())

        self.assertEqual(body["deleted"], "drive-old.db")
        self.assertFalse(
            os.path.exists(os.path.join(self.sessions, "drive-old.db"))
        )

    def test_an_unshipped_session_is_refused(self):
        """It exists nowhere else. Losing it loses the drive."""
        self._session("drive-new.db", rows=5)
        self._session("drive-old.db", rows=10, synced_rows=3, age=9000)

        body = self.assert_status(
            409, self.post, "delete_session",
            {"name": "drive-old.db", "confirm": True},
        )

        self.assertIn("not confirmed synced", body["error"])
        self.assertTrue(
            os.path.exists(os.path.join(self.sessions, "drive-old.db"))
        )

    def test_a_session_with_no_watermark_is_refused(self):
        """Unknown is not permission."""
        self._session("drive-new.db", rows=5)
        self._session("drive-old.db", rows=10, age=9000)

        self.assert_status(
            409, self.post, "delete_session",
            {"name": "drive-old.db", "confirm": True},
        )

    def test_the_active_database_is_never_deletable(self):
        """The runtime is writing it."""
        self._session("drive-new.db", rows=5, synced_rows=5)

        body = self.assert_status(
            409, self.post, "delete_session",
            {"name": "drive-new.db", "confirm": True},
        )

        self.assertIn("newest", body["error"])

    def test_path_traversal_is_refused(self):
        outside = os.path.join(os.path.dirname(self.sessions), "keep.db")

        with open(outside, "w") as fh:
            fh.write("x")

        for name in ("../keep.db", "/etc/passwd", "sub/../../keep.db",
                     "..", ".", ""):
            with self.subTest(name=name):
                self.assert_status(
                    409, self.post, "delete_session",
                    {"name": name, "confirm": True},
                )

        self.assertTrue(os.path.exists(outside), "escaped the sessions dir")

    def test_only_db_files_can_be_deleted(self):
        other = os.path.join(self.sessions, "notes.txt")

        with open(other, "w") as fh:
            fh.write("x")

        self.assert_status(
            409, self.post, "delete_session",
            {"name": "notes.txt", "confirm": True},
        )
        self.assertTrue(os.path.exists(other))

    def test_deletion_needs_confirmation_like_everything_destructive(self):
        self._session("drive-new.db", rows=5)
        self._session("drive-old.db", rows=10, synced_rows=10, age=9000)

        self.assert_status(
            400, self.post, "delete_session", {"name": "drive-old.db"}
        )
        self.assertTrue(
            os.path.exists(os.path.join(self.sessions, "drive-old.db"))
        )

    def test_the_listing_marks_what_is_safe(self):
        self._session("drive-new.db", rows=5)
        self._session("drive-old.db", rows=10, synced_rows=10, age=9000)
        self._session("drive-part.db", rows=10, synced_rows=2, age=18000)

        rows = {
            r["name"]: r
            for r in json.loads(self.get("/api/status").read())["sessions"]
        }

        self.assertTrue(rows["drive-new.db"]["active"])
        self.assertTrue(rows["drive-old.db"]["synced"])
        self.assertFalse(rows["drive-part.db"]["synced"])


class RecordingTruth(AdminCase):
    """
    "The service is active" and "data is landing" are different claims.

    A green dot is equally green with the ENET cable out, so the panel
    counts rows that actually reached the database.
    """

    def setUp(self):
        self.sessions = tempfile.mkdtemp()
        self.config = dict(
            getattr(type(self), "config", {}),
            sessions_dir=self.sessions,
            sync_state_file="/nonexistent/sync-state.json",
            dashboard_status_url="http://127.0.0.1:9/nope",
            recording_window_s=60,
        )
        super().setUp()

    def _db(self, name, fresh=0, stale=0):
        path = os.path.join(self.sessions, name)
        con = sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE samples(run_id INT, ts REAL, param_id INT,"
            " value REAL);"
            "CREATE TABLE runs(id INTEGER PRIMARY KEY, started_at REAL,"
            " ended_at REAL, mode TEXT, clock_synced INT);"
        )
        con.execute(
            "INSERT INTO runs VALUES(7, ?, NULL, 'normal', 1)",
            (time.time() - 300,),
        )
        now = time.time()
        con.executemany(
            "INSERT INTO samples VALUES(7, ?, ?, 1.0)",
            [(now - 5, i % 4) for i in range(fresh)],
        )
        con.executemany(
            "INSERT INTO samples VALUES(7, ?, 1, 1.0)",
            [(now - 3600,) for _ in range(stale)],
        )
        con.commit()
        con.close()

    def test_recent_samples_are_counted(self):
        self._db("drive.db", fresh=120, stale=500)

        rec = json.loads(self.get("/api/status").read())["recording"]

        self.assertEqual(rec["samples"], 120)
        self.assertEqual(rec["channels"], 4)
        self.assertEqual(rec["run"], 7)

    def test_an_idle_recorder_reads_zero_not_missing(self):
        """
        Zero is the whole point: it is the difference between "up" and
        "working", and it must be a number rather than an absence.
        """
        self._db("drive.db", fresh=0, stale=500)

        rec = json.loads(self.get("/api/status").read())["recording"]

        self.assertEqual(rec["samples"], 0)

    def test_no_drive_file_is_reported_not_crashed(self):
        rec = json.loads(self.get("/api/status").read())["recording"]

        self.assertIsNone(rec["db"])
        self.assertIsNone(rec["samples"])

    def test_a_database_predating_mode_and_clock_still_reads(self):
        """
        `runs.mode` and `runs.clock_synced` arrived on 2026-08-30. Asking
        an older drive file for them fails the whole query, which would
        blank the recording panel for every historical file on the card.
        """
        path = os.path.join(self.sessions, "old.db")
        con = sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE samples(run_id INT, ts REAL, param_id INT,"
            " value REAL);"
            "CREATE TABLE runs(id INTEGER PRIMARY KEY, started_at REAL,"
            " ended_at REAL);"
        )
        con.execute("INSERT INTO runs VALUES(3, 1.0, NULL)")
        con.execute(
            "INSERT INTO samples VALUES(3, ?, 1, 1.0)", (time.time(),)
        )
        con.commit()
        con.close()

        rec = json.loads(self.get("/api/status").read())["recording"]

        self.assertNotIn("error", rec)
        self.assertEqual(rec["run"], 3)
        self.assertEqual(rec["samples"], 1)

    def test_an_unreachable_runtime_is_reported(self):
        self._db("drive.db", fresh=10)

        rec = json.loads(self.get("/api/status").read())["recording"]

        self.assertIsNone(rec["link"])
        self.assertIn("not answering", rec["status"])


class BootScopedLogs(AdminCase):
    def test_the_previous_boot_can_be_read(self):
        self.post("logs", {"unit": "f10-dashboard", "boot": -1})

        self.assertTrue(self.fake.ran("journalctl", "-b", "-1"))

    def test_the_current_boot_is_the_default(self):
        self.post("logs", {"unit": "f10-dashboard"})

        self.assertTrue(self.fake.ran("journalctl", "-b", "0"))

    def test_the_boot_argument_is_bounded_and_typed(self):
        """
        It reaches a journalctl command line, so it must never be an
        arbitrary string or an unbounded number.
        """
        for boot in ("-1", 1, -99, 2.5, True, None):
            with self.subTest(boot=boot):
                self.assert_status(
                    409, self.post, "logs",
                    {"unit": "f10-dashboard", "boot": boot},
                )

        self.assertEqual(self.fake.calls, [])


class SyncControl(AdminCase):
    def test_only_pause_and_resume_are_accepted(self):
        for verb in ("stop", "flush", "", None):
            with self.subTest(verb=verb):
                self.assert_status(
                    409, self.post, "sync", {"verb": verb, "confirm": True}
                )

    def test_an_unreachable_agent_is_an_error_not_a_crash(self):
        body = self.assert_status(
            409, self.post, "sync", {"verb": "pause", "confirm": True}
        )

        self.assertIn("did not answer", body["error"])


class DeploymentFiles(unittest.TestCase):
    ADMIN = os.path.join(support.ROOT, "hardware", "raspberry-pi", "admin")

    def _read(self, name):
        with open(os.path.join(self.ADMIN, name), encoding="utf-8") as fh:
            return fh.read()

    def test_the_config_is_gitignored(self):
        """It holds the panel password."""
        with open(os.path.join(support.ROOT, ".gitignore"),
                  encoding="utf-8") as fh:
            ignored = fh.read()

        self.assertIn("hardware/raspberry-pi/admin/config.json", ignored)

    def test_no_real_config_is_committed(self):
        self.assertFalse(
            os.path.exists(os.path.join(self.ADMIN, "config.json")),
            "config.json holds a password and must not be in the repo",
        )

    def test_sudoers_has_no_wildcards(self):
        """
        A wildcard on systemctl would let any unit be started, and a unit
        can run anything. Every command is named in full.
        """
        for line in self._read("f10-admin.sudoers").splitlines():
            line = line.strip()

            if not line.startswith("@PI_USER@"):
                continue

            command = line.split("NOPASSWD:", 1)[1].strip()

            self.assertNotIn("*", command, line)
            self.assertTrue(command.startswith("/"), line)

    def test_sudoers_grants_only_the_commands_the_code_runs(self):
        granted = {
            line.split("NOPASSWD:", 1)[1].strip()
            for line in self._read("f10-admin.sudoers").splitlines()
            if line.strip().startswith("@PI_USER@") and "NOPASSWD:" in line
        }

        self.assertIn("/sbin/reboot", granted)
        self.assertIn("/sbin/poweroff", granted)

        #
        # Nothing that changes what runs at boot, and no shell. Compared
        # word by word: a substring check would match "sh" inside
        # "systemctl" and pass or fail for the wrong reason.
        #
        forbidden = {
            "daemon-reload", "enable", "disable", "mask", "apt", "apt-get",
            "sh", "bash", "su", "chmod", "chown",
        }

        for command in granted:
            words = command.replace("/", " ").split()

            for word in words:
                self.assertNotIn(word, forbidden, command)

    def test_the_installer_validates_sudoers_before_installing_it(self):
        """
        An invalid file in /etc/sudoers.d breaks sudo entirely. On a
        headless Pi in a car that means a reinstall, so `visudo -c` has
        to run before `install`.
        """
        script = self._read("install.sh")
        check = script.index("visudo -cf")
        install = script.index("install -m 0440")

        self.assertLess(check, install)

    def test_the_admin_unit_is_independent_of_the_runtime(self):
        """
        It is what you reach for when the runtime is broken, so it must
        not be ordered after it or bound to it.
        """
        #
        # Directives only - the file's comments mention f10-dashboard,
        # which is the point being explained rather than a dependency.
        #
        directives = [
            line.strip() for line in self._read("f10-admin.service").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

        for line in directives:
            if line.split("=")[0] in ("After", "Wants", "Requires",
                                      "BindsTo", "PartOf", "Before"):
                self.assertNotIn("f10-dashboard", line, line)


if __name__ == "__main__":
    unittest.main()
