"""
Temporary public share links for the live dashboard.

No car and no network beyond a loopback socket: the HTTP cases drive a
real ThreadingHTTPServer over 127.0.0.1 with a stub Telemetry, because
the security properties worth testing (what the /s/ prefix will and will
not serve, and that the VIN never leaves it) live in the request
handler, not in the token store alone.
"""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from . import support

import live


FULL_VIN = "WBAXX00000XX00000"


class StubTelemetry:
    """The handful of Telemetry members the handler touches."""

    def __init__(self, snap=None):
        self.lock = threading.Lock()
        self.meta = {"rpm": {"label": "RPM"}}
        self.meta_version = 1
        self._snap = snap or {
            "status": "live", "connected": True, "vin": FULL_VIN,
            "gateway": "169.254.65.67", "ecu": "0x12", "ecus": ["0x12", "0x18"],
            "values": {"rpm": 820.0}, "rows": 12, "ts": 1.0,
        }

    def get(self):
        return dict(self._snap)

    def wait(self, seen, timeout=2.0):
        return seen + 1, self.get()


class ShareTokenStoreTest(unittest.TestCase):
    def test_mint_produces_a_valid_opaque_token(self):
        shares = live.ShareTokens()
        entry = shares.mint(60)

        self.assertTrue(shares.validate(entry["token"]))
        self.assertGreaterEqual(len(entry["token"]), 24)

    def test_unknown_and_empty_tokens_are_rejected(self):
        shares = live.ShareTokens()
        shares.mint(60)

        for bad in (None, "", "nope", "x" * 32):
            self.assertFalse(shares.validate(bad), bad)

    def test_token_expires_on_its_own(self):
        shares = live.ShareTokens()
        entry = shares.mint(-1)          # already past

        self.assertFalse(shares.validate(entry["token"]))
        self.assertEqual(shares.active(), [])

    def test_revoke_and_revoke_all(self):
        shares = live.ShareTokens()
        a = shares.mint(60)["token"]
        b = shares.mint(60)["token"]

        self.assertTrue(shares.revoke(a))
        self.assertFalse(shares.revoke(a))       # only once
        self.assertFalse(shares.validate(a))
        self.assertTrue(shares.validate(b))

        self.assertEqual(shares.revoke_all(), 1)
        self.assertFalse(shares.validate(b))

    def test_active_is_bounded_by_dropping_the_oldest(self):
        shares = live.ShareTokens(max_active=3)
        tokens = [shares.mint(60)["token"] for _ in range(5)]

        self.assertEqual(len(shares.active()), 3)
        self.assertFalse(shares.validate(tokens[0]))
        self.assertTrue(shares.validate(tokens[-1]))

    def test_use_is_counted(self):
        shares = live.ShareTokens()
        token = shares.mint(60)["token"]
        shares.validate(token)
        shares.validate(token)

        self.assertEqual(shares.active()[0]["hits"], 2)


class ShareSnapshotTest(unittest.TestCase):
    def test_vin_is_masked_regardless_of_the_global_switch(self):
        self.assertFalse(live.REDACT_VIN, "default must stay off")

        out = live.share_snapshot({"vin": FULL_VIN})

        self.assertNotEqual(out["vin"], FULL_VIN)
        self.assertTrue(out["vin"].endswith(FULL_VIN[-4:]))
        self.assertNotIn(FULL_VIN[:9], json.dumps(out))

    def test_host_and_bus_detail_is_dropped(self):
        out = live.share_snapshot(
            {"vin": FULL_VIN, "gateway": "169.254.65.67", "ecus": ["0x12"]}
        )

        self.assertNotIn("gateway", out)
        self.assertNotIn("ecus", out)
        self.assertTrue(out["shared"])

    def test_the_caller_snapshot_is_not_mutated(self):
        snap = {"vin": FULL_VIN, "gateway": "169.254.65.67"}
        live.share_snapshot(snap)

        self.assertEqual(snap["vin"], FULL_VIN)
        self.assertIn("gateway", snap)


class ShareHttpTest(unittest.TestCase):
    """The /s/ prefix over a real loopback server."""

    def setUp(self):
        self.tel = StubTelemetry()
        self.shares = live.ShareTokens()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            live.make_handler(self.tel, None, self.shares, "https://example.test"),
        )
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.addCleanup(self.server.shutdown)

    def get(self, path, headers=None):
        request = urllib.request.Request(self.base + path, headers=headers or {})

        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read().decode(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(), dict(exc.headers)

    def post(self, path, obj):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    # -- minting ----------------------------------------------------

    def test_mint_returns_a_link_on_the_configured_public_origin(self):
        code, body = self.post("/api/share", {"ttl": 900})

        self.assertEqual(code, 200)
        self.assertTrue(body["url"].startswith("https://example.test/s/?t="))
        self.assertTrue(self.shares.validate(body["token"]))

    def test_mint_clamps_an_absurd_ttl(self):
        _, body = self.post("/api/share", {"ttl": 10 ** 9})

        self.assertLessEqual(
            body["expires"] - time.time(), max(live.SHARE_TTL_CHOICES) + 5
        )

    def test_revoke_kills_the_link(self):
        _, body = self.post("/api/share", {"ttl": 900})
        token = body["token"]

        self.post("/api/share/revoke", {"token": token})

        self.assertFalse(self.shares.validate(token))

    # -- the gate ---------------------------------------------------

    def test_share_root_without_a_token_is_denied(self):
        code, body, _ = self.get("/s/")

        self.assertEqual(code, 200)          # a page, not a bare error
        self.assertIn("no longer valid", body)
        self.assertNotIn(FULL_VIN, body)

    def test_share_root_with_a_bad_token_is_denied(self):
        code, body, _ = self.get("/s/?t=not-a-real-token")

        self.assertIn("no longer valid", body)

    def test_valid_token_serves_the_page_and_sets_the_cookie(self):
        token = self.shares.mint(900)["token"]
        code, body, headers = self.get("/s/?t=" + token)

        self.assertEqual(code, 200)
        self.assertIn("window.__F10_SHARE__", body)
        self.assertIn(live.SHARE_COOKIE, headers.get("Set-Cookie", ""))
        self.assertIn("HttpOnly", headers.get("Set-Cookie", ""))

    def test_cookie_alone_is_enough_for_the_api(self):
        token = self.shares.mint(900)["token"]
        code, body, _ = self.get(
            "/s/api/snapshot",
            {"Cookie": "%s=%s" % (live.SHARE_COOKIE, token)},
        )

        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["values"]["rpm"], 820.0)

    def test_snapshot_never_carries_the_vin(self):
        token = self.shares.mint(900)["token"]
        _, body, _ = self.get("/s/api/snapshot?t=" + token)

        self.assertNotIn(FULL_VIN, body)
        self.assertNotIn("169.254.65.67", body)
        self.assertTrue(json.loads(body)["shared"])

    def test_owner_snapshot_still_carries_the_vin(self):
        """The share path must not have changed the owner's own view."""
        _, body, _ = self.get("/api/snapshot")

        self.assertIn(FULL_VIN, body)

    # -- what the prefix refuses to serve ---------------------------

    def test_history_and_runs_and_sync_are_not_reachable_when_shared(self):
        token = self.shares.mint(900)["token"]

        for path in ("/api/runs", "/api/history", "/api/sync"):
            code, _, _ = self.get("/s" + path + "?t=" + token)
            self.assertEqual(code, 404, path)

    def test_a_share_token_cannot_mint_or_revoke(self):
        token = self.shares.mint(900)["token"]

        for path in ("/s/api/share", "/s/api/share/revoke"):
            code, _ = self.post(path, {"ttl": 900})
            self.assertEqual(code, 404, path)

    def test_the_prefix_cannot_be_escaped_with_a_trailing_slash(self):
        token = self.shares.mint(900)["token"]
        code, _, _ = self.get("/s/api/sync/?t=" + token)

        self.assertEqual(code, 404)

    def test_expired_token_stops_working_mid_session(self):
        entry = self.shares.mint(900)
        token = entry["token"]

        self.assertEqual(self.get("/s/api/snapshot?t=" + token)[0], 200)

        self.shares.revoke(token)
        _, body, _ = self.get("/s/?t=" + token)

        self.assertIn("no longer valid", body)


class ShareDisabledTest(unittest.TestCase):
    def test_no_share_makes_the_prefix_and_the_endpoints_disappear(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), live.make_handler(StubTelemetry(), None, None, "")
        )
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:%d" % server.server_address[1]

        try:
            with urllib.request.urlopen(base + "/s/", timeout=5) as response:
                code = response.status
        except urllib.error.HTTPError as exc:
            code = exc.code

        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
