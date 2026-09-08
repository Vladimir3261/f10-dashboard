"""
The telemetry UI as files under dashboard/ (issue #39), served by live.py.

What is pinned:

  * the page and every asset it references are served under "/" AND
    under the share prefix - and the list of assets the server knows is
    exactly the list the page references (enumerated from index.html,
    so a new <link>/<script> cannot be added without being served, and
    nothing can be served that the page does not use);
  * under /s/ the assets are token-gated like the page, and NOTHING
    beyond SHARE_ALLOWED + those assets answers there;
  * assets carry `Cache-Control: no-cache` + an ETag (a git pull on the
    Pi must never leave a stale app.js against a new API), the page
    keeps `no-store` as before;
  * the files resolve relative to live.py, never the working directory;
  * the served page is the old inline page with the two blocks lifted
    out: one stylesheet link where <style> was, one script tag where
    <script> was, no other inline code.

No car, no network beyond loopback.
"""

import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests import support  # noqa: F401
from tests.test_share import StubTelemetry

import live


def referenced_assets(html: str):
    """Relative hrefs/srcs the page pulls in, as URL paths under "/"."""
    refs = re.findall(r'<link[^>]+href="([^"]+)"', html)
    refs += re.findall(r'<script[^>]+src="([^"]+)"', html)

    for ref in refs:
        assert not ref.startswith(("/", "http:", "https:", "//")), ref

    return frozenset("/" + ref for ref in refs)


class TheFilesAreTheSourceOfTruth(unittest.TestCase):
    def test_assets_live_beside_live_py_not_in_the_cwd(self):
        expected = Path(live.__file__).resolve().parent / "dashboard"

        self.assertEqual(live.DASHBOARD_DIR, expected)
        self.assertTrue(live.DASHBOARD_DIR.is_absolute())

        for name in ("index.html", "style.css", "app.js"):
            self.assertTrue((live.DASHBOARD_DIR / name).is_file(), name)

    def test_reading_does_not_depend_on_the_working_directory(self):
        before = os.getcwd()

        with tempfile.TemporaryDirectory() as empty:
            os.chdir(empty)

            try:
                page = live.read_dashboard_asset("index.html")
            finally:
                os.chdir(before)

        self.assertEqual(page.decode(), live.PAGE)

    def test_asset_names_are_bare_file_names(self):
        for bad in ("../live.py", "sub/x.js", ".env", "/etc/passwd"):
            with self.subTest(name=bad), self.assertRaises(ValueError):
                live.read_dashboard_asset(bad)

    def test_the_server_knows_exactly_what_the_page_references(self):
        self.assertEqual(referenced_assets(live.PAGE),
                         frozenset(live.DASHBOARD_ASSETS))
        self.assertEqual(frozenset(live.ASSETS), frozenset(live.DASHBOARD_ASSETS))

    def test_the_page_has_no_inline_code_left(self):
        """One link, one script tag, in the places the blocks were."""
        self.assertEqual(live.PAGE.count("<style"), 0)
        self.assertEqual(live.PAGE.count("<script"), 1)
        self.assertIn('<script src="app.js"></script>\n</body>', live.PAGE)
        self.assertIn('<link rel="stylesheet" href="style.css">\n</head>', live.PAGE)
        self.assertTrue(live.PAGE.startswith("<!doctype html>"))

    def test_share_page_is_the_page_plus_the_flag_before_the_assets_run(self):
        flag = '<script>window.__F10_API__ = "/s"; window.__F10_SHARE__ = true;</script>'

        self.assertEqual(live.SHARE_PAGE.replace(flag + "\n", "", 1), live.PAGE)
        self.assertLess(live.SHARE_PAGE.index(flag),
                        live.SHARE_PAGE.index('<script src="app.js">'))

    def test_the_assets_are_the_files_verbatim(self):
        self.assertEqual(live.ASSETS["/style.css"],
                         (live.DASHBOARD_DIR / "style.css").read_bytes())
        self.assertEqual(live.ASSETS["/app.js"],
                         (live.DASHBOARD_DIR / "app.js").read_bytes())
        self.assertIn(b"const API = window.__F10_API__", live.ASSETS["/app.js"])


class Served(unittest.TestCase):
    """Both prefixes over a real loopback server."""

    def setUp(self):
        self.shares = live.ShareTokens()
        self.token = self.shares.mint(600)["token"]
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            live.make_handler(StubTelemetry(), None, self.shares, ""),
        )
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path, headers=None):
        request = urllib.request.Request(self.base + path, headers=headers or {})

        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def shared(self, path):
        return self.get(path, {"Cookie": "%s=%s" % (live.SHARE_COOKIE, self.token)})

    # -- the page ---------------------------------------------------

    def test_root_serves_the_page_uncached(self):
        code, body, headers = self.get("/")

        self.assertEqual(code, 200)
        self.assertEqual(body.decode(), live.PAGE)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_share_root_serves_the_share_page(self):
        code, body, _ = self.get("/s/?t=" + self.token)

        self.assertEqual(code, 200)
        self.assertEqual(body.decode(), live.SHARE_PAGE)

    # -- every asset, both prefixes ----------------------------------

    def test_every_referenced_asset_is_served_under_both_prefixes(self):
        for path in referenced_assets(live.PAGE):
            expected = (live.DASHBOARD_DIR / path.lstrip("/")).read_bytes()

            for label, fetch in (("owner", self.get), ("share", self.shared)):
                with self.subTest(asset=path, prefix=label):
                    url = path if label == "owner" else live.SHARE_PREFIX + path
                    code, body, headers = fetch(url)

                    self.assertEqual(code, 200)
                    self.assertEqual(body, expected)
                    self.assertEqual(headers["Content-Type"],
                                     live.DASHBOARD_ASSETS[path])
                    self.assertEqual(headers["Cache-Control"], "no-cache")
                    self.assertTrue(headers["ETag"].startswith('"'))

    def test_a_query_token_works_for_the_assets_too(self):
        """The first load has only the ?t= link; the cookie comes with it."""
        code, body, _ = self.get("/s/app.js?t=" + self.token)

        self.assertEqual(code, 200)
        self.assertEqual(body, live.ASSETS["/app.js"])

    def test_an_unchanged_asset_revalidates_to_304(self):
        _, _, headers = self.get("/app.js")
        code, body, again = self.get("/app.js", {"If-None-Match": headers["ETag"]})

        self.assertEqual(code, 304)
        self.assertEqual(body, b"")
        self.assertEqual(again["ETag"], headers["ETag"])
        self.assertEqual(again["Cache-Control"], "no-cache")

    def test_only_one_cache_control_header_is_sent(self):
        for path in ("/", "/app.js", "/api/meta"):
            with self.subTest(path=path):
                request = urllib.request.Request(self.base + path)

                with urllib.request.urlopen(request, timeout=5) as response:
                    values = response.headers.get_all("Cache-Control")

                self.assertEqual(len(values), 1, values)

    # -- the share prefix stays closed ------------------------------

    def test_share_assets_are_token_gated(self):
        for path in live.DASHBOARD_ASSETS:
            with self.subTest(asset=path):
                code, body, _ = self.get(live.SHARE_PREFIX + path)

                self.assertEqual(code, 200)
                self.assertIn("no longer valid", body.decode())
                self.assertNotEqual(body, live.ASSETS[path])

    def test_nothing_else_answers_under_the_share_prefix(self):
        """SHARE_ALLOWED + the assets, and not one path more."""
        reachable = set(live.SHARE_ALLOWED) | set(live.DASHBOARD_ASSETS)
        self.assertEqual(
            reachable,
            {"/", "/api/snapshot", "/api/stream", "/api/meta",
             "/style.css", "/app.js"},
        )

        # ("/app.js/" is not listed: the share handler has always folded a
        # trailing slash - "/api/snapshot/" == "/api/snapshot" - and the
        # same asset under a second spelling is not a second asset.)
        for path in ("/index.html", "/dashboard/app.js",
                     "/../app.js", "/APP.JS", "/api/runs", "/api/history",
                     "/api/sync", "/api/diagnostics", "/api/share",
                     "/style.css.map", "/live.py", "/favicon.ico"):
            with self.subTest(path=path):
                code, _, _ = self.shared(live.SHARE_PREFIX + path)
                self.assertEqual(code, 404)

    def test_share_allowed_itself_is_unchanged(self):
        self.assertEqual(live.SHARE_ALLOWED,
                         frozenset({"/", "/api/snapshot", "/api/stream", "/api/meta"}))
        self.assertNotIn("/style.css", live.SHARE_ALLOWED)

    def test_owner_prefix_does_not_serve_the_directory_or_the_source(self):
        for path in ("/index.html", "/dashboard/", "/dashboard/app.js",
                     "/live.py", "/app.js/"):
            with self.subTest(path=path):
                code, _, _ = self.get(path)
                self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
