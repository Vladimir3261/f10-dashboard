"""
The odometer API (issue #49, docs/ODOMETER_API.md): the process-lifetime
accumulator, the bearer-token store, the two endpoints and the minting
tool.

No car and no network beyond a loopback socket. The endpoint cases
drive a real ThreadingHTTPServer with a stub Telemetry, exactly as
tests/test_share.py does, because the properties worth proving - what a
missing token gets, that a share token opens nothing here, that a burst
of samples becomes one event - live in the request handler.
"""

import importlib.util
import io
import json
import os
import select
import socket
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from http.server import ThreadingHTTPServer
from unittest import mock

from . import support
from .test_share import StubTelemetry

import live
from bmwdiag.mapping.decoder import Reading


def load_tool():
    spec = importlib.util.spec_from_file_location(
        "api_token", os.path.join(support.ROOT, "tools", "api_token.py")
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    return tool


def touch_forward(path: str, seconds: float = 5.0) -> None:
    """
    Push a file's mtime into the future so a rewrite inside the same
    filesystem timestamp tick still reads as a change. The store keys
    its re-read on (mtime, size); a test must not depend on the tick.
    """
    st = os.stat(path)
    os.utime(path, (st.st_atime + seconds, st.st_mtime + seconds))


#: The contract's body keys, docs/ODOMETER_API.md.
CONTRACT_KEYS = {
    "t", "epoch", "odometer_m", "odometer_t", "speed_kmh", "speed_t",
    "connected", "clock_synced", "resets", "source", "mapping_ver",
}


def cycle(raw=None, speed=None, at=100.0, quality="ok",
          speed_quality="ok"):
    """One poll cycle's (readings, stamps) as the executor hands them."""
    readings, stamps = {}, {}

    if raw is not None:
        readings[live.ODOMETER_SOURCE] = Reading(raw, quality)
        stamps[live.ODOMETER_SOURCE] = at

    if speed is not None:
        readings[live.ODOMETER_SPEED] = Reading(speed, speed_quality)
        stamps[live.ODOMETER_SPEED] = at + 0.01

    return readings, stamps


class TheAccumulator(unittest.TestCase):
    def test_nothing_is_known_before_the_first_sample(self):
        odo = live.Odometer()
        version, snap = odo.current()

        self.assertIsNone(snap["odometer_m"])
        self.assertIsNone(snap["odometer_t"])
        self.assertIsNone(snap["speed_kmh"])
        self.assertIsNone(snap["speed_t"])
        self.assertFalse(snap["connected"])
        self.assertIsNone(snap["clock_synced"])
        self.assertEqual(snap["resets"], 0)
        self.assertEqual(snap["source"], "n47d_odometer_m")
        self.assertIsNone(snap["mapping_ver"])
        self.assertEqual(set(snap) | {"t"}, CONTRACT_KEYS)

    def test_the_first_sample_is_zero_and_deltas_add_up(self):
        odo = live.Odometer()

        self.assertTrue(odo.feed(*cycle(raw=123456, at=10.0)))
        self.assertEqual(odo.current()[1]["odometer_m"], 0)
        self.assertEqual(odo.current()[1]["odometer_t"], 10.0)

        odo.feed(*cycle(raw=123956, at=11.0))
        self.assertEqual(odo.current()[1]["odometer_m"], 500)
        self.assertEqual(odo.current()[1]["odometer_t"], 11.0)

        #: Standing still: the same raw twice adds nothing.
        odo.feed(*cycle(raw=123956, at=12.0))
        self.assertEqual(odo.current()[1]["odometer_m"], 500)
        self.assertEqual(odo.current()[1]["resets"], 0)

    def test_a_regeneration_reset_is_bridged_not_subtracted(self):
        """
        The ECU counter goes 1500 -> 20: what it counts now was driven
        after the restart, so 20 is added and the reset is counted. The
        metres between the last sample and the reset are the one thing
        lost, and the total never goes backwards.
        """
        odo = live.Odometer()
        odo.feed(*cycle(raw=1000, at=1.0))
        odo.feed(*cycle(raw=1500, at=2.0))
        odo.feed(*cycle(raw=20, at=3.0))

        snap = odo.current()[1]
        self.assertEqual(snap["odometer_m"], 520)
        self.assertEqual(snap["resets"], 1)

        odo.feed(*cycle(raw=120, at=4.0))
        self.assertEqual(odo.current()[1]["odometer_m"], 620)
        self.assertEqual(odo.current()[1]["resets"], 1)

    def test_the_total_is_monotonic_through_a_random_walk_with_resets(self):
        odo = live.Odometer()
        raws = [5, 9, 9, 40, 2, 3, 3, 100, 0, 1, 250, 250, 7]
        last = -1

        for i, raw in enumerate(raws):
            odo.feed(*cycle(raw=raw, at=float(i)))
            now = odo.current()[1]["odometer_m"]
            self.assertGreaterEqual(now, last)
            last = now

        self.assertEqual(odo.current()[1]["resets"], 3)
        #: 4 + 31 (first run) + 2 + 1 + 97 + 0 + 1 + 249 + 0 + 7
        self.assertEqual(last, 392)

    def test_a_flagged_reading_is_ignored(self):
        odo = live.Odometer()
        odo.feed(*cycle(raw=1000, at=1.0))
        odo.feed(*cycle(raw=1100, at=2.0))
        version = odo.current()[0]

        self.assertFalse(odo.feed(*cycle(raw=0, at=3.0, quality="sentinel")))
        self.assertFalse(odo.feed(*cycle(raw=99, at=3.5, quality="saturated")))

        snap = odo.current()[1]
        self.assertEqual(snap["odometer_m"], 100)
        self.assertEqual(snap["odometer_t"], 2.0)
        self.assertEqual(snap["resets"], 0)
        #: Nothing landed, nothing else changed: no version bump.
        self.assertEqual(odo.current()[0], version)

        #: And the next good one carries on from the last good one -
        #: the flagged 0 did not become a reset.
        odo.feed(*cycle(raw=1200, at=4.0))
        self.assertEqual(odo.current()[1]["odometer_m"], 200)
        self.assertEqual(odo.current()[1]["resets"], 0)

    def test_a_flagged_speed_is_ignored_too(self):
        odo = live.Odometer()
        odo.feed(*cycle(speed=57, at=1.0))
        odo.feed(*cycle(speed=255, at=2.0, speed_quality="saturated"))

        snap = odo.current()[1]
        self.assertEqual(snap["speed_kmh"], 57)
        self.assertEqual(snap["speed_t"], 1.01)

    def test_speed_carries_its_own_acquisition_time(self):
        odo = live.Odometer()
        odo.feed(*cycle(raw=10, speed=88, at=5.0))

        snap = odo.current()[1]
        self.assertEqual(snap["speed_kmh"], 88)
        self.assertEqual(snap["speed_t"], 5.01)
        self.assertEqual(snap["odometer_t"], 5.0)

    def test_a_cycle_without_either_channel_changes_nothing(self):
        odo = live.Odometer()
        odo.feed(*cycle(raw=10, at=1.0))
        version, before = odo.current()

        self.assertFalse(odo.feed({"rpm": Reading(800.0)}, {"rpm": 2.0}))

        after_version, after = odo.current()
        self.assertEqual(after_version, version)
        self.assertEqual(after, before)

    def test_the_epoch_survives_a_reconnect_and_the_counter_carries_on(self):
        odo = live.Odometer()
        epoch = odo.epoch
        self.assertRegex(epoch, r"^[0-9a-f]{8}$")

        odo.feed(*cycle(raw=1000, at=1.0), connected=True, clock_synced=True)
        odo.feed(*cycle(raw=1300, at=2.0))
        odo.set_connected(False)

        snap = odo.current()[1]
        self.assertFalse(snap["connected"])
        self.assertEqual(snap["odometer_m"], 300)      # values stay
        self.assertEqual(snap["epoch"], epoch)

        #: The link is back; the ECU counted on meanwhile.
        odo.feed(*cycle(raw=1900, at=30.0), connected=True)
        snap = odo.current()[1]
        self.assertTrue(snap["connected"])
        self.assertEqual(snap["odometer_m"], 900)
        self.assertEqual(snap["resets"], 0)
        self.assertEqual(snap["epoch"], epoch)

        #: ...or it regenerated while the link was down: one reset.
        odo.set_connected(False)
        odo.feed(*cycle(raw=50, at=60.0), connected=True)
        snap = odo.current()[1]
        self.assertEqual(snap["odometer_m"], 950)
        self.assertEqual(snap["resets"], 1)
        self.assertEqual(snap["epoch"], epoch)

    def test_configure_does_not_move_the_epoch_either(self):
        odo = live.Odometer()
        epoch = odo.epoch
        odo.configure(True, 4)
        odo.configure(False, None)
        odo.set_connected(True, clock_synced=False)

        self.assertEqual(odo.epoch, epoch)

    def test_epochs_differ_between_processes(self):
        """Two instances stand for two process lifetimes."""
        self.assertNotEqual(live.Odometer().epoch, live.Odometer().epoch)

    def test_configure_publishes_the_mapping_version(self):
        odo = live.Odometer()
        odo.configure(True, 4)

        self.assertTrue(odo.loaded)
        self.assertEqual(odo.current()[1]["mapping_ver"], 4)

    def test_wait_wakes_on_a_sample_and_times_out_quietly(self):
        odo = live.Odometer()
        version, _ = odo.current()

        #: Nothing happens: the same version comes back at the timeout.
        started = time.monotonic()
        same, _ = odo.wait(version, 0.05)
        self.assertEqual(same, version)
        self.assertGreaterEqual(time.monotonic() - started, 0.04)

        def later():
            time.sleep(0.05)
            odo.feed(*cycle(raw=7, at=1.0))

        threading.Thread(target=later, daemon=True).start()
        started = time.monotonic()
        moved, snap = odo.wait(version, 5.0)
        self.assertGreater(moved, version)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(snap["odometer_m"], 0)

    def test_the_snapshot_names_no_vehicle_and_no_session(self):
        odo = live.Odometer()
        odo.configure(True, 4)
        odo.feed(*cycle(raw=10, speed=3, at=1.0), connected=True,
                 clock_synced=True)
        text = json.dumps(odo.current()[1]).lower()

        for forbidden in ("vin", "run_id", "session", "gateway", "ecu"):
            self.assertNotIn(forbidden, text, forbidden)


class TheTokenStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "api-tokens.json")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, True))

    def write(self, tokens):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"tokens": [
                {"name": f"t{i}", "token": t, "created": "2026-09-10T00:00:00Z"}
                for i, t in enumerate(tokens)
            ]}, fh)

        #: Each rewrite lands strictly later than the one before, even
        #: inside one filesystem tick and at the same size.
        self.writes = getattr(self, "writes", 0) + 1
        touch_forward(self.path, 5.0 * self.writes)

    def test_a_missing_file_means_no_token_is_valid(self):
        store = live.ApiTokens(self.path)

        self.assertEqual(store.count(), 0)
        self.assertFalse(store.validate("Bearer anything"))

    def test_no_path_means_no_token_is_valid(self):
        store = live.ApiTokens(None)

        self.assertEqual(store.count(), 0)
        self.assertFalse(store.validate("Bearer anything"))

    def test_only_a_bearer_header_with_a_listed_token_passes(self):
        self.write(["alpha-token", "beta-token"])
        store = live.ApiTokens(self.path)

        self.assertEqual(store.count(), 2)
        self.assertTrue(store.validate("Bearer alpha-token"))
        self.assertTrue(store.validate("bearer beta-token"))
        self.assertTrue(store.validate("  Bearer   beta-token  "))

        for bad in (None, "", "Bearer", "Bearer ", "Bearer alpha-token-x",
                    "Bearer alpha", "Basic alpha-token", "alpha-token",
                    "Token alpha-token", "Bearer ALPHA-TOKEN"):
            self.assertFalse(store.validate(bad), repr(bad))

    def test_a_rewrite_is_picked_up_without_a_restart(self):
        self.write(["alpha-token"])
        store = live.ApiTokens(self.path)
        self.assertTrue(store.validate("Bearer alpha-token"))

        self.write(["gamma-token"])          # alpha revoked, gamma minted
        self.assertFalse(store.validate("Bearer alpha-token"))
        self.assertTrue(store.validate("Bearer gamma-token"))
        self.assertEqual(store.count(), 1)

    def test_a_deleted_file_revokes_everything(self):
        self.write(["alpha-token"])
        store = live.ApiTokens(self.path)
        self.assertTrue(store.validate("Bearer alpha-token"))

        os.unlink(self.path)
        self.assertFalse(store.validate("Bearer alpha-token"))
        self.assertEqual(store.count(), 0)

    def test_a_broken_file_fails_closed(self):
        self.write(["alpha-token"])
        store = live.ApiTokens(self.path)
        self.assertTrue(store.validate("Bearer alpha-token"))

        with open(self.path, "w") as fh:
            fh.write("{not json")

        touch_forward(self.path)
        self.assertFalse(store.validate("Bearer alpha-token"))

        with open(self.path, "w") as fh:
            json.dump({"tokens": "alpha-token"}, fh)

        touch_forward(self.path, 10.0)
        self.assertFalse(store.validate("Bearer alpha-token"))

    def test_an_entry_without_a_token_is_not_a_wildcard(self):
        with open(self.path, "w") as fh:
            json.dump({"tokens": [{"name": "blank", "token": ""},
                                  {"name": "none"}, "junk", None]}, fh)

        store = live.ApiTokens(self.path)
        self.assertEqual(store.count(), 0)
        self.assertFalse(store.validate("Bearer "))
        self.assertFalse(store.validate("Bearer None"))


class TheMintingTool(unittest.TestCase):
    def setUp(self):
        self.tool = load_tool()
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "sub", "api-tokens.json")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, True))

    def run_tool(self, *argv):
        out, err = io.StringIO(), io.StringIO()

        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = self.tool.main(["--file", self.path, *argv])
            except SystemExit as exc:
                code = exc.code

        return code, out.getvalue(), err.getvalue()

    def test_mint_prints_the_token_once_and_the_store_is_private(self):
        code, out, err = self.run_tool("mint", "android-nav")
        token = out.strip()

        self.assertEqual(code, 0)
        self.assertGreaterEqual(len(token), 40)
        self.assertNotIn(token, err)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

        with open(self.path) as fh:
            doc = json.load(fh)

        self.assertEqual([e["name"] for e in doc["tokens"]], ["android-nav"])
        self.assertEqual(doc["tokens"][0]["token"], token)
        self.assertRegex(doc["tokens"][0]["created"],
                         r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

        #: And live.py accepts exactly that token.
        store = live.ApiTokens(self.path)
        self.assertTrue(store.validate("Bearer " + token))
        self.assertFalse(store.validate("Bearer " + token[:-1]))

    def test_list_never_prints_a_token(self):
        _, t1, _ = self.run_tool("mint", "android-nav")
        _, t2, _ = self.run_tool("mint", "laptop")
        code, out, err = self.run_tool("list")

        self.assertEqual(code, 0)
        self.assertIn("android-nav", out)
        self.assertIn("laptop", out)
        self.assertNotIn(t1.strip(), out + err)
        self.assertNotIn(t2.strip(), out + err)

        for row in self.tool.listing(self.path):
            self.assertEqual(set(row), {"name", "created", "length"})

    def test_a_name_is_minted_once(self):
        self.run_tool("mint", "android-nav")
        code, out, err = self.run_tool("mint", "android-nav")

        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("revoke", str(err) + str(code))
        self.assertEqual(len(self.tool.listing(self.path)), 1)

    def test_revoke_takes_effect_on_a_running_store(self):
        _, out, _ = self.run_tool("mint", "android-nav")
        token = out.strip()
        _, out2, _ = self.run_tool("mint", "laptop")
        other = out2.strip()
        store = live.ApiTokens(self.path)
        self.assertTrue(store.validate("Bearer " + token))

        touch_forward(self.path, -10.0)     # so the rewrite is newer
        code, out, err = self.run_tool("revoke", "android-nav")
        self.assertEqual(code, 0)
        self.assertFalse(store.validate("Bearer " + token))
        self.assertTrue(store.validate("Bearer " + other))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

        code, out, err = self.run_tool("revoke", "android-nav")
        self.assertEqual(code, 1)

    def test_the_default_store_is_under_the_gitignored_local_dir(self):
        self.assertEqual(
            self.tool.DEFAULT_FILE,
            os.path.join(support.ROOT, "local", "api-tokens.json"),
        )
        self.assertEqual(live.DEFAULT_API_TOKENS, self.tool.DEFAULT_FILE)

        with open(os.path.join(support.ROOT, ".gitignore")) as fh:
            ignored = fh.read().split()

        self.assertTrue(any(line.rstrip("/") in ("local", "/local")
                            for line in ignored), ignored)


class EndpointCase(unittest.TestCase):
    """live.py's handler over a real loopback server."""

    def setUp(self):
        self.tool = load_tool()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, True))
        self.token_path = os.path.join(self.dir, "api-tokens.json")
        self.token = self.tool.mint(self.token_path, "nav")
        self.api_tokens = live.ApiTokens(self.token_path)

        self.tel = StubTelemetry()
        self.shares = live.ShareTokens()
        self.odometer = live.Odometer()
        self.odometer.configure(True, 4)

        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            live.make_handler(self.tel, None, self.shares, "https://example.test",
                              odometer=self.odometer,
                              api_tokens=self.api_tokens),
        )
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def bearer(self, token=None):
        return {"Authorization": "Bearer " + (token or self.token)}

    def get(self, path, headers=None, method="GET"):
        request = urllib.request.Request(self.base + path,
                                         headers=headers or {}, method=method)

        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read().decode(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(), dict(exc.headers)

    def open_stream(self, headers):
        """
        A raw socket on the stream, for reading it frame by frame with
        a deadline: http.client's buffered reader is unusable after a
        socket timeout, so the response is parsed by hand.
        """
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(sock.close)
        lines = ["GET /api/odometer/stream HTTP/1.1", "Host: 127.0.0.1"]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        stream = _Stream(sock)
        stream.read_headers()

        return stream

    @staticmethod
    def read_frames(stream, seconds, limit=1000):
        """Every SSE frame (data or comment) seen within `seconds`."""
        return stream.frames(seconds, limit)


class _Stream:
    """A hand-parsed HTTP/1.1 response whose body is read by lines."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.status = None
        self.headers = {}

    def _fill(self, deadline):
        remaining = deadline - time.monotonic()

        if remaining <= 0:
            return False

        ready, _, _ = select.select([self.sock], [], [], remaining)

        if not ready:
            return False

        chunk = self.sock.recv(65536)

        if not chunk:
            return False

        self.buf += chunk

        return True

    def read_headers(self):
        deadline = time.monotonic() + 5.0

        while b"\r\n\r\n" not in self.buf:
            if not self._fill(deadline):
                raise AssertionError("no response headers")

        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode().split("\r\n")
        self.status = int(lines[0].split()[1])
        self.headers = {k.strip(): v.strip()
                        for k, v in (l.split(":", 1) for l in lines[1:])}

    def getheader(self, name, default=None):
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v

        return default

    def frames(self, seconds, limit=1000):
        frames = []
        deadline = time.monotonic() + seconds

        while len(frames) < limit:
            while b"\n" in self.buf and len(frames) < limit:
                line, self.buf = self.buf.split(b"\n", 1)

                if line:
                    frames.append(line.decode())

            if len(frames) >= limit or not self._fill(deadline):
                break

        return frames


class TheEndpoint(EndpointCase):
    def test_no_token_is_401_with_a_bearer_challenge_that_names_nothing(self):
        for path in ("/api/odometer", "/api/odometer/stream"):
            with self.subTest(path=path):
                code, body, headers = self.get(path)

                self.assertEqual(code, 401)
                self.assertEqual(headers.get("WWW-Authenticate"), "Bearer")
                self.assertEqual(headers.get("Cache-Control"), "no-store")
                self.assertEqual(json.loads(body), {"error": "unauthorized"})
                self.assertNotIn(self.token, body)
                self.assertNotIn("nav", body)

    def test_a_wrong_token_or_scheme_is_the_same_401(self):
        for header in ({"Authorization": "Bearer " + self.token[:-2] + "zz"},
                       {"Authorization": "Basic " + self.token},
                       {"Authorization": self.token},
                       {"Authorization": "Bearer "}):
            with self.subTest(header=header):
                code, body, headers = self.get("/api/odometer", header)

                self.assertEqual(code, 401)
                self.assertEqual(headers.get("WWW-Authenticate"), "Bearer")
                self.assertEqual(json.loads(body), {"error": "unauthorized"})

    def test_a_revoked_token_is_401_on_the_next_request(self):
        code, body, headers = self.get("/api/odometer", self.bearer())
        self.assertEqual(code, 200)

        touch_forward(self.token_path, -10.0)
        self.assertTrue(self.tool.revoke(self.token_path, "nav"))

        code, body, headers = self.get("/api/odometer", self.bearer())
        self.assertEqual(code, 401)

    def test_the_token_is_never_in_a_query_string(self):
        code, body, headers = self.get("/api/odometer?token=" + self.token)

        self.assertEqual(code, 401)

    def test_a_valid_token_gets_the_contract_body(self):
        code, body, headers = self.get("/api/odometer", self.bearer())
        doc = json.loads(body)

        self.assertEqual(code, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertTrue(headers.get("Content-Type", "").startswith(
            "application/json"))
        self.assertEqual(set(doc), CONTRACT_KEYS)
        self.assertAlmostEqual(doc["t"], time.time(), delta=5.0)
        self.assertEqual(doc["epoch"], self.odometer.epoch)
        self.assertIsNone(doc["odometer_m"])
        self.assertIsNone(doc["odometer_t"])
        self.assertIsNone(doc["speed_kmh"])
        self.assertIsNone(doc["speed_t"])
        self.assertFalse(doc["connected"])
        self.assertIsNone(doc["clock_synced"])
        self.assertEqual(doc["resets"], 0)
        self.assertEqual(doc["source"], "n47d_odometer_m")
        self.assertEqual(doc["mapping_ver"], 4)

    def test_the_body_follows_the_samples_and_the_link(self):
        self.odometer.feed(*cycle(raw=5000, speed=42, at=1000.0),
                           connected=True, clock_synced=True)
        self.odometer.feed(*cycle(raw=5750, speed=61, at=1001.0))

        doc = json.loads(self.get("/api/odometer", self.bearer())[1])
        self.assertEqual(doc["odometer_m"], 750)
        self.assertEqual(doc["odometer_t"], 1001.0)
        self.assertEqual(doc["speed_kmh"], 61)
        self.assertEqual(doc["speed_t"], 1001.01)
        self.assertTrue(doc["connected"])
        self.assertTrue(doc["clock_synced"])

        #: The link drops: the values stay, the flag says so.
        self.odometer.set_connected(False)
        doc = json.loads(self.get("/api/odometer", self.bearer())[1])
        self.assertFalse(doc["connected"])
        self.assertEqual(doc["odometer_m"], 750)
        self.assertEqual(doc["speed_kmh"], 61)

    def test_the_body_carries_nothing_of_the_vehicle_or_the_session(self):
        self.odometer.feed(*cycle(raw=5000, speed=42, at=1000.0),
                           connected=True, clock_synced=True)
        code, body, headers = self.get("/api/odometer", self.bearer())

        self.assertNotIn(self.tel.get()["vin"], body)
        self.assertNotIn(self.tel.get()["gateway"], body)

        for forbidden in ("vin", "run", "session", "gateway", "ecu"):
            self.assertNotIn(forbidden, body.lower(), forbidden)

    def test_without_the_channel_it_is_503_after_the_auth_check(self):
        self.odometer.configure(False, None)

        for path in ("/api/odometer", "/api/odometer/stream"):
            with self.subTest(path=path):
                code, body, headers = self.get(path, self.bearer())
                self.assertEqual(code, 503)
                self.assertEqual(json.loads(body),
                                 {"error": "odometer channel not loaded"})

                #: Auth first: an anonymous caller does not learn even that.
                code, body, headers = self.get(path)
                self.assertEqual(code, 401)

    def test_the_endpoint_is_not_on_the_share_surface(self):
        share = self.shares.mint(600)["token"]

        for path in ("/s/api/odometer", "/s/api/odometer/stream"):
            with self.subTest(path=path):
                code, body, headers = self.get(f"{path}?t={share}")
                self.assertEqual(code, 404)

                code, body, headers = self.get(f"{path}?t={share}",
                                               self.bearer())
                self.assertEqual(code, 404)

        #: The share allowlist itself, so a future edit is deliberate.
        self.assertFalse({"/api/odometer", "/api/odometer/stream"}
                         & set(live.SHARE_ALLOWED))

        #: And the share token opens nothing on the real path either.
        code, body, headers = self.get("/api/odometer?t=" + share)
        self.assertEqual(code, 401)

        #: Without a valid share token the prefix serves its denial page
        #: (pre-existing behaviour) - and no odometer data with it.
        self.odometer.feed(*cycle(raw=10, speed=3, at=1.0), connected=True)
        code, body, headers = self.get("/s/api/odometer?t=nope", self.bearer())
        self.assertNotIn("odometer_m", body)
        self.assertNotIn(self.odometer.epoch, body)

    def test_a_post_is_not_a_thing(self):
        request = urllib.request.Request(
            self.base + "/api/odometer", data=b"{}", method="POST",
            headers={**self.bearer(), "Content-Type": "application/json"},
        )

        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)

        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()

    def test_without_a_store_at_all_nothing_is_in(self):
        """A handler built with no ApiTokens: fail closed."""
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            live.make_handler(self.tel, None, self.shares, "",
                              odometer=self.odometer),
        )
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = urllib.request.Request(
            "http://127.0.0.1:%d/api/odometer" % server.server_address[1],
            headers=self.bearer(),
        )

        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)

        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()


class TheStream(EndpointCase):
    def test_the_first_event_is_the_current_state_at_once(self):
        self.odometer.feed(*cycle(raw=100, speed=9, at=1.0), connected=True)
        response = self.open_stream(self.bearer())

        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type", "").startswith(
            "text/event-stream"))

        frames = self.read_frames(response, 1.0, limit=1)
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0].startswith("data: "))
        doc = json.loads(frames[0][len("data: "):])
        self.assertEqual(set(doc), CONTRACT_KEYS)
        self.assertEqual(doc["odometer_m"], 0)
        self.assertEqual(doc["speed_kmh"], 9)

    def test_one_event_per_sample_when_they_are_slow(self):
        response = self.open_stream(self.bearer())
        self.read_frames(response, 0.5, limit=1)          # the opening state

        for i in range(3):
            time.sleep(0.25)
            self.odometer.feed(*cycle(raw=100 * i, at=float(i)))

        frames = [f for f in self.read_frames(response, 0.5)
                  if f.startswith("data: ")]
        self.assertEqual(len(frames), 3)
        totals = [json.loads(f[6:])["odometer_m"] for f in frames]
        self.assertEqual(totals, [0, 100, 200])

    def test_a_burst_is_coalesced_to_at_most_ten_events_a_second(self):
        response = self.open_stream(self.bearer())
        self.read_frames(response, 0.5, limit=1)

        #: 200 samples in ~0.5 s: far above the cap.
        for i in range(200):
            self.odometer.feed(*cycle(raw=i, at=float(i)))
            time.sleep(0.0025)

        frames = [f for f in self.read_frames(response, 1.0)
                  if f.startswith("data: ")]
        #: About 0.5 s of burst plus the drain: never near 200, and the
        #: cap is 10/s.
        self.assertGreaterEqual(len(frames), 2)
        self.assertLessEqual(len(frames), 12, frames)
        #: The last event carries the latest state - nothing is lost by
        #: coalescing except the intermediate frames.
        self.assertEqual(json.loads(frames[-1][6:])["odometer_m"], 199)

    def test_a_keepalive_comment_marks_a_quiet_link(self):
        with mock.patch.object(live, "ODOMETER_KEEPALIVE_S", 0.1):
            response = self.open_stream(self.bearer())
            frames = self.read_frames(response, 0.6)

        comments = [f for f in frames if f.startswith(":")]
        self.assertGreaterEqual(len(comments), 2, frames)
        self.assertEqual(len([f for f in frames if f.startswith("data:")]), 1)

    def test_the_link_state_alone_is_an_event(self):
        response = self.open_stream(self.bearer())
        self.read_frames(response, 0.5, limit=1)
        self.odometer.set_connected(True, clock_synced=True)

        frames = [f for f in self.read_frames(response, 0.5)
                  if f.startswith("data: ")]
        self.assertEqual(len(frames), 1)
        self.assertTrue(json.loads(frames[0][6:])["connected"])


class TheDiagnosticsView(unittest.TestCase):
    def test_the_token_store_is_reported_as_a_count_only(self):
        tool = load_tool()
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        path = os.path.join(d, "t.json")
        token = tool.mint(path, "nav")
        tool.mint(path, "laptop")

        diag = live.Diagnostics()
        self.assertEqual(diag.loaded()["api_tokens"], 0)

        diag.publish(api_tokens=live.ApiTokens(path))
        loaded = diag.loaded()
        self.assertEqual(loaded["api_tokens"], 2)
        text = json.dumps(loaded)
        self.assertNotIn(token, text)
        self.assertNotIn("nav", text)
        self.assertNotIn(path, text)

        #: A cleared session (link down) still knows the store.
        diag.clear("link down")
        self.assertEqual(diag.loaded()["api_tokens"], 2)


class TheWiring(unittest.TestCase):
    """The poll loop feeds the accumulator; a static check, no car."""

    def setUp(self):
        import inspect
        self.poll_loop = inspect.getsource(live.poll_loop)
        self.main = inspect.getsource(live.main)

    def test_the_poll_loop_feeds_every_cycle_and_reports_the_link(self):
        self.assertIn("odometer.feed(readings, stamps", self.poll_loop)
        self.assertIn("odometer.configure(", self.poll_loop)
        self.assertIn("odometer.set_connected(False)", self.poll_loop)

    def test_main_builds_one_odometer_and_one_store_and_hands_them_over(self):
        self.assertEqual(self.main.count("Odometer()"), 1)
        self.assertIn("ApiTokens(args.api_tokens)", self.main)
        self.assertIn("diag.publish(api_tokens=api_tokens)", self.main)

    def test_the_source_is_a_channel_the_run_car_set_declares(self):
        from bmwdiag.mapping.loader import load_file

        mapping = load_file(os.path.join(
            support.ROOT, "mappings", "candidates", "bmw", "dde", "n47",
            "d72n47a0_dpf_egr.yaml",
        ))
        keys = {s.key for r in mapping.requests for s in r.signals}
        self.assertIn(live.ODOMETER_SOURCE, keys)
        request = next(r for r in mapping.requests
                       if any(s.key == live.ODOMETER_SOURCE for s in r.signals))
        self.assertEqual(request.polling_class, "odometer")
        self.assertEqual(mapping.version, 4)

        with open(os.path.join(support.ROOT, "run_car.sh")) as fh:
            self.assertIn("d72n47a0_dpf_egr.yaml", fh.read())


if __name__ == "__main__":
    unittest.main()
