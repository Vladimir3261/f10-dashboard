"""
The Pi panel as the front door: the reverse proxy to live.py, the auth
boundary around it, and the listener list.

Everything runs against a real panel on the loopback talking to a FAKE
live.py - a small HTTP server that records what reached it and answers
with markers, so a test can tell "the panel answered" from "the runtime
answered" without ambiguity. No car, no network, no systemd.

What matters most here is what the proxy does NOT do: forward the
panel's credentials, let the share prefix reach anything management-
shaped, or buffer a stream.
"""

import base64
import contextlib
import http.client
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests import support  # noqa: F401

sys.path.insert(
    0, os.path.join(support.ROOT, "hardware", "raspberry-pi", "admin")
)
import server as admin                                   # noqa: E402


USER, PASSWORD = "f10", "correct-horse-battery-staple"


def auth_header(user=USER, password=PASSWORD):
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()

    return {"Authorization": "Basic " + raw}


def closed_port():
    """A loopback port nothing listens on (bound once, then released)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))

        return sock.getsockname()[1]


class FakeRuntime:
    """
    Stands in for live.py.

    Records every request (method, path, headers, body) and answers with
    bodies that name it, so a response can be attributed. The stream
    endpoint is gated: the first event goes out at once, the second only
    when the test releases it, and after that it writes heartbeats until
    the client is gone - which is how a test proves the panel relays
    chunk-by-chunk and closes the upstream when the phone leaves.
    """

    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()
        self.release = threading.Event()
        self.client_gone = threading.Event()
        self.stream_started = threading.Event()
        self.slow_started = threading.Event()
        self.slow_release = threading.Event()
        #: Connections open right now - what the panel is holding.
        self.open_connections = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()

                with fake.lock:
                    fake.open_connections += 1

            def finish(self):
                with fake.lock:
                    fake.open_connections -= 1

                super().finish()

            def _record(self, body=b""):
                with fake.lock:
                    fake.requests.append({
                        "method": self.command,
                        "path": self.path,
                        "headers": {k: v for k, v in self.headers.items()},
                        #: Every copy of a header, in order - a dict
                        #: would hide a duplicate, and duplicates are
                        #: the point of the X-Forwarded-* tests.
                        "all": {k: self.headers.get_all(k)
                                for k in set(self.headers.keys())},
                        "body": body,
                    })

            def _json(self, code, payload):
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Fake-Runtime", "yes")
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._record()
                path = self.path.split("?")[0]

                if path == "/api/stream":
                    self._stream()
                    return

                if path == "/api/runs":
                    #: A non-200 the panel must relay as-is.
                    raw = b"no runs here\n"
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return

                if self.path == "/api/modes?hop=1":
                    #: Every hop-by-hop header, plus the two the panel
                    #: sets itself, on an otherwise ordinary answer (an
                    #: owner path - the panel proxies no other).
                    raw = b'{"answered_by": "runtime"}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Keep-Alive", "timeout=5, max=100")
                    self.send_header("Upgrade", "h2c")
                    self.send_header("Proxy-Authenticate", "Basic")
                    self.send_header("Trailer", "Expires")
                    self.send_header("Server", "fake-runtime/0")
                    self.send_header("X-Fake-Runtime", "yes")
                    self.end_headers()
                    self.wfile.write(raw)
                    return

                if self.path == "/api/sync?slow=1":
                    #: A runtime that accepted the connection and is
                    #: stuck: nothing comes back until released.
                    fake.slow_started.set()
                    fake.slow_release.wait(10.0)
                    self._json(200, {"answered_by": "runtime", "late": True})
                    return

                if path == "/api/meta":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("ETag", '"meta-1"')
                    raw = json.dumps({"answered_by": "runtime",
                                      "meta": []}).encode()
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return

                #: The share surface and anything else: named as such.
                self._json(200, {"answered_by": "runtime", "path": self.path})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                self._record(body)
                path = self.path.split("?")[0]

                if path.startswith("/s/"):
                    #: live.py refuses every POST under the prefix.
                    self._json(404, {"answered_by": "runtime",
                                     "refused": self.path})
                    return

                if path == "/api/mode":
                    try:
                        wanted = json.loads(body.decode()).get("mode")
                    except ValueError:
                        wanted = None

                    if wanted == "bogus":
                        self._json(409, {"error": "unknown mode"})
                        return

                self._json(200, {"answered_by": "runtime", "path": self.path,
                                 "echo": body.decode("utf-8", "replace")})

            def _stream(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                fake.stream_started.set()

                try:
                    self.wfile.write(b"data: one\n\n")
                    fake.release.wait(5.0)
                    self.wfile.write(b"data: two\n\n")

                    while True:
                        time.sleep(0.02)
                        self.wfile.write(b": heartbeat\n\n")
                except OSError:
                    fake.client_gone.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def last(self, path_prefix):
        with self.lock:
            hits = [r for r in self.requests if r["path"].startswith(path_prefix)]

        return hits[-1] if hits else None


class ProxyCase(unittest.TestCase):
    """A panel on the loopback in front of a FakeRuntime."""

    #: Extra config per subclass.
    config = {}
    runtime_up = True

    def setUp(self):
        self.calls = []
        self._real_run = admin.run
        admin.run = self._fake_run

        if self.runtime_up:
            self.runtime = FakeRuntime()
            self.addCleanup(self.runtime.close)
            upstream = self.runtime.url
        else:
            self.runtime = None
            upstream = self.down_upstream()

        cfg = dict(admin.DEFAULTS)
        cfg.update({
            "username": USER,
            "password": PASSWORD,
            "repo_dir": support.ROOT,
            "git_remote": "git@example.invalid:owner/repo.git",
            "sync_status_url": "http://127.0.0.1:9/nope",
            "dashboard_status_url": upstream + "/api/snapshot",
            "dashboard_url": upstream,
        })
        cfg.update(self.config)
        self.cfg = cfg

        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), admin.make_handler(cfg)
        )
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def tearDown(self):
        admin.run = self._real_run

    def _fake_run(self, argv, timeout=30.0):
        self.calls.append(list(argv))

        return 0, ""

    def down_upstream(self):
        """Where live.py is not: a closed port, unless a case says."""
        return f"http://127.0.0.1:{closed_port()}"

    # -- helpers ----------------------------------------------------

    def request(self, path, method="GET", body=None, headers=None,
                authed=True):
        head = dict(auth_header()) if authed else {}
        head.update(headers or {})
        head.setdefault("Connection", "close")
        data = None

        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            head.setdefault("Content-Type", "application/json")

        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, headers=head,
            method=method,
        )

        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def json(self, *args, **kwargs):
        code, headers, raw = self.request(*args, **kwargs)

        try:
            return code, headers, json.loads(raw)
        except ValueError:
            return code, headers, {"_raw": raw.decode("utf-8", "replace")}


class JsonPassthrough(ProxyCase):
    def test_an_owner_api_call_is_answered_by_the_runtime(self):
        code, headers, body = self.json("/api/meta")

        self.assertEqual(code, 200)
        self.assertEqual(body["answered_by"], "runtime")
        #: Its headers come through - the ETag the page revalidates on.
        self.assertEqual(headers.get("ETag"), '"meta-1"')

    def test_the_query_string_is_forwarded_verbatim(self):
        self.json("/api/history?run=7&secs=600&x=%2Fs%2F")

        self.assertEqual(self.runtime.last("/api/history")["path"],
                         "/api/history?run=7&secs=600&x=%2Fs%2F")

    def test_a_non_200_from_the_runtime_is_relayed_not_rewritten(self):
        code, headers, raw = self.request("/api/runs")

        self.assertEqual(code, 404)
        self.assertEqual(raw, b"no runs here\n")

    def test_a_post_carries_its_body_and_the_answer_comes_back(self):
        code, headers, body = self.json("/api/mode", method="POST",
                                        body={"mode": "long"})

        self.assertEqual(code, 200)
        self.assertEqual(body["echo"], '{"mode": "long"}')
        hit = self.runtime.last("/api/mode")
        self.assertEqual(hit["method"], "POST")
        self.assertEqual(hit["body"], b'{"mode": "long"}')

    def test_a_refused_post_is_relayed_with_its_status(self):
        code, headers, body = self.json("/api/mode", method="POST",
                                        body={"mode": "bogus"})

        self.assertEqual(code, 409)
        self.assertEqual(body["error"], "unknown mode")

    def test_every_listed_owner_path_reaches_the_runtime(self):
        #: The stream never ends by design; StreamRelay covers it.
        for path in sorted(admin.PROXY_GET - {"/api/stream"}):
            with self.subTest(path=path):
                self.request(path)
                self.assertIsNotNone(self.runtime.last(path), path)

        for path in sorted(admin.PROXY_POST):
            with self.subTest(path=path):
                self.request(path, method="POST", body={})
                hit = self.runtime.last(path)
                self.assertIsNotNone(hit, path)
                self.assertEqual(hit["method"], "POST")

    def test_the_share_prefix_goes_through_whole(self):
        for path in ("/s", "/s/", "/s/?t=abc", "/s/api/stream?t=abc",
                     "/s/style.css", "/s/app.js"):
            with self.subTest(path=path):
                code, headers, body = self.json(path, authed=False)

                self.assertEqual(code, 200)
                self.assertEqual(body["answered_by"], "runtime")
                self.assertEqual(body["path"], path)

    def test_a_post_under_the_share_prefix_is_the_runtimes_refusal(self):
        code, headers, body = self.json("/s/api/mode", method="POST",
                                        body={"mode": "off"}, authed=False)

        self.assertEqual(code, 404)
        self.assertEqual(body["answered_by"], "runtime")


class HeadersAcrossTheProxy(ProxyCase):
    def test_the_panels_credentials_never_reach_the_runtime(self):
        self.request("/api/meta")
        forwarded = self.runtime.last("/api/meta")["headers"]

        self.assertNotIn("Authorization", forwarded)
        self.assertNotIn("Proxy-Authorization", forwarded)

    def test_forwarded_for_proto_and_host_are_set(self):
        self.request("/api/meta")
        forwarded = self.runtime.last("/api/meta")["headers"]

        self.assertEqual(forwarded["X-Forwarded-For"], "127.0.0.1")
        self.assertEqual(forwarded["X-Forwarded-Proto"], "http")
        #: The Host the phone used, not the runtime's loopback address:
        #: live.py builds share links from it.
        self.assertEqual(forwarded["Host"], f"127.0.0.1:{self.port}")

    def test_a_clients_forwarded_headers_are_replaced_not_prepended(self):
        """
        live.py takes the FIRST X-Forwarded-Proto / -Host and builds
        the share link it mints from them. The panel is the edge here:
        what the client claims about the hop before it is nothing, and
        is replaced - not forwarded ahead of the panel's own copy.
        """
        self.request("/api/meta", headers={
            "X-Forwarded-For": "1.2.3.4",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "spoof.example",
        })
        forwarded = self.runtime.last("/api/meta")["all"]

        self.assertEqual(forwarded.get("X-Forwarded-For"), ["127.0.0.1"])
        self.assertEqual(forwarded.get("X-Forwarded-Proto"), ["http"])
        self.assertIsNone(forwarded.get("X-Forwarded-Host"))

    def test_the_same_holds_for_a_share_viewer(self):
        """The unauthenticated surface is where it matters most: a
        viewer must not be able to have `Secure` put on their cookie."""
        self.request("/s/api/snapshot", authed=False,
                     headers={"X-Forwarded-Proto": "https"})
        forwarded = self.runtime.last("/s/api/snapshot")["all"]

        self.assertEqual(forwarded.get("X-Forwarded-Proto"), ["http"])

    def test_the_response_hop_by_hop_and_identity_headers_are_stripped(self):
        code, headers, body = self.json("/api/modes?hop=1")
        lower = {k.lower(): v for k, v in headers.items()}

        self.assertEqual(code, 200)
        self.assertEqual(body["answered_by"], "runtime")
        #: The runtime's own header proves this is the ?hop=1 answer.
        self.assertEqual(lower.get("x-fake-runtime"), "yes")

        for name in ("keep-alive", "upgrade", "proxy-authenticate",
                     "trailer", "transfer-encoding"):
            self.assertNotIn(name, lower, name)

        #: The panel's Server, not the runtime's - and no interpreter
        #: version on it: the share prefix is public once published.
        self.assertEqual(lower.get("server"), "f10-admin")

    def test_the_share_cookie_is_forwarded(self):
        """A share viewer's token cookie is live.py's to check."""
        self.request("/s/api/snapshot", authed=False,
                     headers={"Cookie": "f10share=tok123"})

        self.assertEqual(
            self.runtime.last("/s/api/snapshot")["headers"].get("Cookie"),
            "f10share=tok123",
        )

    def test_no_hop_by_hop_header_is_relayed(self):
        self.request("/api/meta", headers={"Connection": "keep-alive, TE",
                                           "TE": "trailers"})
        forwarded = self.runtime.last("/api/meta")["headers"]

        self.assertNotIn("TE", forwarded)
        self.assertEqual(forwarded.get("Connection"), "close")

    def test_a_relayed_answer_is_not_framed_as_a_panel_page(self):
        """
        The panel's own CSP would break the /s/ page (its inline script)
        and means nothing on JSON. Relayed answers carry the runtime's
        headers plus nosniff, and nothing of the panel's.
        """
        code, headers, body = self.request("/api/meta")

        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertNotIn("Content-Security-Policy", headers)
        self.assertNotIn("X-Frame-Options", headers)
        #: One Server and one Date, the panel's - not the runtime's too.
        self.assertEqual(
            sum(1 for k in headers if k.lower() == "server"), 1
        )

    def test_a_proxied_post_needs_a_json_content_type(self):
        """
        CSRF on the runtime's controls. The browser attaches the panel's
        cached credentials to any POST here; a cross-origin form cannot
        send application/json, and a fetch that does is preflighted.
        """
        code, headers, body = self.json(
            "/api/mode", method="POST", body=b'{"mode":"off"}',
            headers={"Content-Type": "text/plain"},
        )

        self.assertEqual(code, 403)
        self.assertIsNone(self.runtime.last("/api/mode"))

    def test_a_body_over_the_cap_is_refused_before_the_runtime_sees_it(self):
        big = b'{"mode": "' + b"x" * admin.MAX_PROXY_BODY + b'"}'
        code, headers, body = self.json("/api/mode", method="POST", body=big)

        self.assertEqual(code, 413)
        self.assertIsNone(self.runtime.last("/api/mode"))

        #: One byte under the cap goes through.
        fits = b'{"mode": "' + b"x" * (admin.MAX_PROXY_BODY - 13) + b'"}'
        self.assertLessEqual(len(fits), admin.MAX_PROXY_BODY)
        code, headers, body = self.json("/api/mode", method="POST", body=fits)

        self.assertEqual(code, 200)
        self.assertEqual(len(self.runtime.last("/api/mode")["body"]), len(fits))


class BehindATrustedProxy(ProxyCase):
    """
    #41 puts nginx in front of the panel. Its X-Forwarded-* are the
    truth about the hop before it - kept, for a request that arrived
    from its address; the panel only fills in what it did not set.
    """

    config = {"trusted_proxies": ["127.0.0.1"]}

    def test_the_proxys_headers_pass_through(self):
        self.request("/api/meta", headers={
            "X-Forwarded-For": "203.0.113.9",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "f10.example",
        })
        forwarded = self.runtime.last("/api/meta")["all"]

        self.assertEqual(forwarded.get("X-Forwarded-For"), ["203.0.113.9"])
        self.assertEqual(forwarded.get("X-Forwarded-Proto"), ["https"])
        self.assertEqual(forwarded.get("X-Forwarded-Host"), ["f10.example"])

    def test_what_the_proxy_did_not_set_the_panel_sets(self):
        self.request("/api/meta")
        forwarded = self.runtime.last("/api/meta")["all"]

        self.assertEqual(forwarded.get("X-Forwarded-For"), ["127.0.0.1"])
        self.assertEqual(forwarded.get("X-Forwarded-Proto"), ["http"])

    def test_the_default_trusts_nobody(self):
        self.assertEqual(admin.DEFAULTS["trusted_proxies"], [])


class ReadDeadline(ProxyCase):
    """
    A runtime that accepts and never answers (the process is there, its
    loop is stuck) must cost a panel thread for seconds, not for ever -
    except on a stream, where silence is the car being quiet.
    """

    def test_a_wedged_runtime_is_503_at_the_deadline(self):
        with mock.patch.object(admin, "READ_TIMEOUT_S", 0.3):
            started = time.monotonic()
            code, headers, body = self.json("/api/sync?slow=1")
            elapsed = time.monotonic() - started

        self.assertTrue(self.runtime.slow_started.is_set())
        self.assertEqual(code, 503)
        self.assertEqual(body["error"], "runtime not running")
        self.assertIn("timed out", body["detail"])
        self.assertLess(elapsed, 3.0)
        self.runtime.slow_release.set()

    def test_a_phone_that_left_before_the_deadline_leaves_no_traceback(self):
        """
        The 503 is written at the deadline, long after a browser gives
        up. A phone that reset its connection by then must not put a
        BrokenPipeError traceback in the journal for every abandoned
        refresh - the write is guarded, the handler just ends.
        """
        err = io.StringIO()

        with mock.patch.object(admin, "READ_TIMEOUT_S", 0.3), \
                contextlib.redirect_stderr(err):
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            head = (f"GET /api/sync?slow=1 HTTP/1.1\r\nHost: x\r\n"
                    f"Authorization: {auth_header()['Authorization']}\r\n\r\n")
            sock.sendall(head.encode())
            self.assertTrue(self.runtime.slow_started.wait(5.0))
            #: Reset, not close: the panel's next write fails outright.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                            struct.pack("ii", 1, 0))
            sock.close()
            time.sleep(1.0)

        self.runtime.slow_release.set()
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn("BrokenPipe", err.getvalue())
        self.assertNotIn("ConnectionReset", err.getvalue())

    def test_the_stream_has_no_deadline(self):
        """Patched to well under the quiet gap the stream test waits
        through: a deadline applied to streams would end it."""
        with mock.patch.object(admin, "READ_TIMEOUT_S", 0.1):
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            self.addCleanup(sock.close)
            head = (f"GET /api/stream HTTP/1.1\r\nHost: x\r\n"
                    f"Authorization: {auth_header()['Authorization']}\r\n\r\n")
            sock.sendall(head.encode())
            reader = StreamRelay.Lines(sock)

            while reader.readline() not in (b"\r\n", b""):
                pass

            self.assertEqual(reader.readline(), b"data: one\n")
            self.assertEqual(reader.readline(), b"\n")
            time.sleep(0.5)
            self.runtime.release.set()

            #: Still open, still relaying, half a second after the
            #: patched deadline would have cut it.
            self.assertEqual(reader.readline(), b"data: two\n")


class StalledConnect(ProxyCase):
    """
    The connect timeout is what bounds "not at all": an upstream whose
    accept queue is full never completes the handshake, and without
    the timeout the request would wait for the kernel's SYN retries
    (minutes).
    """

    runtime_up = False

    def down_upstream(self):
        #: listen(0) and never accept: the first connection fills the
        #: queue, every later SYN is dropped on the floor.
        self.plug = socket.socket()
        self.plug.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.plug.bind(("127.0.0.1", 0))
        self.plug.listen(0)
        self.addCleanup(self.plug.close)
        self.filler = socket.create_connection(self.plug.getsockname(),
                                               timeout=1)
        self.addCleanup(self.filler.close)

        return "http://127.0.0.1:%d" % self.plug.getsockname()[1]

    def test_a_stalled_connect_is_503_at_the_connect_timeout(self):
        with mock.patch.object(admin, "CONNECT_TIMEOUT_S", 0.3):
            started = time.monotonic()
            code, headers, body = self.json("/api/meta")
            elapsed = time.monotonic() - started

        self.assertEqual(code, 503)
        self.assertEqual(body["error"], "runtime not running")
        self.assertIn("timed out", body["detail"])
        self.assertLess(elapsed, 3.0)


class StreamRelay(ProxyCase):
    """
    /api/stream is a server-sent event stream that is open for a whole
    drive. It has to reach the phone event by event, with no read
    deadline that ends it while the car is quiet, and close both sides
    when either goes.

    On a raw socket: http.client hands a `Connection: close` response
    its socket and forgets it, so closing the connection object would
    close nothing - and what is asserted here is precisely who closes.
    """

    class Lines:
        """A line reader on a socket that survives a read timeout
        (socket.makefile does not - it is "timed out" for good)."""

        def __init__(self, sock):
            self.sock = sock
            self.buf = b""

        def readline(self):
            while b"\n" not in self.buf:
                chunk = self.sock.recv(4096)
                if not chunk:
                    line, self.buf = self.buf, b""
                    return line
                self.buf += chunk

            line, _, self.buf = self.buf.partition(b"\n")
            return line + b"\n"

    def _open_stream(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(sock.close)
        head = (f"GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                f"Authorization: {auth_header()['Authorization']}\r\n"
                "Accept: text/event-stream\r\n\r\n")
        sock.sendall(head.encode())
        reader = self.Lines(sock)
        status = reader.readline()
        headers = {}

        while True:
            line = reader.readline()
            if line in (b"\r\n", b""):
                break
            name, _, value = line.decode().partition(":")
            headers[name.strip().lower()] = value.strip()

        return sock, reader, status, headers

    def test_events_arrive_one_at_a_time_not_when_the_buffer_fills(self):
        sock, reader, status, headers = self._open_stream()

        self.assertTrue(status.startswith(b"HTTP/1.1 200"), status)
        self.assertEqual(headers.get("content-type"), "text/event-stream")
        self.assertNotIn("content-length", headers)
        self.assertTrue(self.runtime.stream_started.wait(5.0))

        #: The first event is here before the runtime has written the
        #: second - so nothing upstream was held back waiting for more.
        self.assertEqual(reader.readline(), b"data: one\n")
        self.assertEqual(reader.readline(), b"\n")

        #: And nothing more is here, because nothing more was sent: a
        #: bounded wait sees the panel holding the connection open, not
        #: closing it on a read timeout.
        sock.settimeout(0.5)

        with self.assertRaises(socket.timeout):
            reader.readline()

        sock.settimeout(5)
        self.runtime.release.set()

        self.assertEqual(reader.readline(), b"data: two\n")

    def test_the_runtime_is_told_when_the_phone_leaves(self):
        sock, reader, status, headers = self._open_stream()
        self.assertTrue(self.runtime.stream_started.wait(5.0))
        self.runtime.release.set()
        self.assertEqual(reader.readline(), b"data: one\n")

        #: The phone goes away.
        sock.close()

        #: The runtime finds out on its next write - not never.
        self.assertTrue(
            self.runtime.client_gone.wait(5.0),
            "the panel kept the upstream stream open after its client left",
        )

    def test_every_aborted_stream_releases_its_upstream_connection(self):
        """
        The guarantee the docstring sells: the upstream connection is
        closed as soon as the phone goes. Twenty streams opened and
        dropped must leave the runtime with none of them.
        """
        self.runtime.release.set()
        socks = []

        for _ in range(20):
            sock, reader, status, headers = self._open_stream()
            self.assertEqual(reader.readline(), b"data: one\n")
            socks.append(sock)

        with self.runtime.lock:
            held = self.runtime.open_connections

        self.assertGreaterEqual(held, 20)

        for sock in socks:
            sock.close()

        deadline = time.monotonic() + 5.0

        while time.monotonic() < deadline:
            with self.runtime.lock:
                if self.runtime.open_connections == 0:
                    break

            time.sleep(0.02)

        with self.runtime.lock:
            left = self.runtime.open_connections

        self.assertEqual(left, 0, "upstream connections still open after "
                                  "every client left")

    def test_the_stream_is_owner_only(self):
        code, headers, body = self.json("/api/stream", authed=False)

        self.assertEqual(code, 401)
        self.assertIsNone(self.runtime.last("/api/stream"))


class RuntimeDown(ProxyCase):
    """live.py is not running. The panel must be exactly as useful as
    before, and say what is wrong in a shape the page understands."""

    runtime_up = False

    def test_an_owner_api_call_is_503_with_a_readable_body(self):
        code, headers, body = self.json("/api/meta")

        self.assertEqual(code, 503)
        self.assertEqual(body["error"], "runtime not running")
        self.assertFalse(body["ready"])
        self.assertIn("not answering", body["detail"])
        self.assertIn(self.cfg["dashboard_url"], body["detail"])
        self.assertEqual(headers.get("Retry-After"), "5")

    def test_the_stream_is_503_too_so_the_page_retries(self):
        code, headers, body = self.json("/api/stream")

        self.assertEqual(code, 503)

    def test_a_share_link_is_503_not_a_login_prompt(self):
        code, headers, body = self.json("/s/?t=abc", authed=False)

        self.assertEqual(code, 503)
        self.assertEqual(body["error"], "runtime not running")

    def test_the_page_and_the_telemetry_files_still_load(self):
        for path in ("/", "/dashboard/", "/dashboard/style.css",
                     "/dashboard/app.js"):
            with self.subTest(path=path):
                code, headers, raw = self.request(path)
                self.assertEqual(code, 200)
                self.assertTrue(raw)

    def test_status_says_the_runtime_is_down(self):
        code, headers, body = self.json("/api/status")

        self.assertEqual(code, 200)
        self.assertFalse(body["recording"]["up"])
        self.assertIsNone(body["recording"]["link"])

    def test_a_restart_still_works(self):
        """The whole point of a panel that outlives the runtime."""
        code, headers, body = self.json(
            "/api/action/restart", method="POST",
            body={"unit": "f10-dashboard", "confirm": True},
            headers={admin.CSRF_HEADER: "1"},
        )

        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(any("restart" in c and "f10-dashboard" in c
                            for c in self.calls))

    def test_the_diagnostics_tab_gets_a_body_it_understands(self):
        code, headers, body = self.json("/api/diagnostics")

        self.assertEqual(code, 503)
        self.assertFalse(body["ready"])
        self.assertIn("not answering", body["detail"])


class AuthBoundary(ProxyCase):
    """Basic auth on everything except /healthz and the share prefix."""

    def test_owner_paths_need_credentials(self):
        for path in sorted(admin.PROXY_GET) + ["/", "/api/status",
                                                "/dashboard/",
                                                "/dashboard/app.js"]:
            with self.subTest(path=path):
                code, headers, raw = self.request(path, authed=False)
                self.assertEqual(code, 401)
                self.assertIn("WWW-Authenticate", headers)

        for path in sorted(admin.PROXY_POST):
            with self.subTest(path=path):
                code, headers, raw = self.request(path, method="POST",
                                                  body={}, authed=False)
                self.assertEqual(code, 401)

        #: And none of those reached the runtime.
        for path in sorted(admin.PROXY_GET | admin.PROXY_POST):
            self.assertIsNone(self.runtime.last(path), path)

    def test_healthz_and_the_share_prefix_do_not(self):
        code, headers, raw = self.request("/healthz", authed=False)
        self.assertEqual(code, 200)

        code, headers, body = self.json("/s/?t=abc", authed=False)
        self.assertEqual(code, 200)
        self.assertEqual(body["answered_by"], "runtime")
        self.assertNotIn("WWW-Authenticate", headers)

    def test_wrong_credentials_on_a_proxied_path_stop_at_the_panel(self):
        code, headers, raw = self.request(
            "/api/meta", authed=False, headers=auth_header(password="no")
        )

        self.assertEqual(code, 401)
        self.assertIsNone(self.runtime.last("/api/meta"))


class ThePrefixIsAPathSegment(ProxyCase):
    """`/s` and `/s/...` are the share surface; `/sx` is not."""

    def test_under_share_is_a_segment_match(self):
        for path, expected in (("/s", True), ("/s/", True),
                               ("/s/api/stream", True), ("/s?t=1", False),
                               ("/sx", False), ("/sx/", False),
                               ("/share", False), ("/ss/api/status", False),
                               ("/", False), ("", False)):
            with self.subTest(path=path):
                self.assertIs(admin.under_share(path), expected)

    def test_a_lookalike_path_is_not_open(self):
        for path in ("/sx", "/sx/api/snapshot", "/share/api/snapshot"):
            with self.subTest(path=path):
                code, headers, body = self.request(path, authed=False)

                self.assertEqual(code, 401)
                self.assertIsNone(self.runtime.last(path))


class NothingOfThePanelUnderTheSharePrefix(ProxyCase):
    """
    Every route this panel serves itself, asked for under /s/. Each
    must be answered by the runtime (whatever it says), never by the
    panel: no status, no diagnostics, no action, no page - and no
    command run.
    """

    def _panel_get_routes(self):
        return ["/", "/healthz", "/api/status", "/api/diagnostics",
                *sorted(admin.TELEMETRY_FILES)]

    def _panel_post_routes(self):
        return [f"/api/action/{name}" for name in sorted(admin.ACTIONS)]

    def test_the_panel_route_list_is_what_the_code_serves(self):
        """
        The enumeration above is a hand-written list; this keeps it
        honest against the handler. Every panel GET answers 200 with
        credentials, and the action names are the ACTIONS table.
        """
        for path in self._panel_get_routes():
            with self.subTest(path=path):
                code, headers, raw = self.request(path)
                self.assertEqual(code, 200, path)

        self.assertEqual(set(admin.ACTIONS), {
            "logs", "restart", "service", "sync", "claude",
            "delete_session", "pull", "reboot", "shutdown",
        })

    def test_no_panel_get_route_answers_under_the_prefix(self):
        for path in self._panel_get_routes():
            with self.subTest(path=path):
                shared = "/s" + path
                code, headers, body = self.json(shared, authed=False)

                self.assertEqual(body.get("answered_by"), "runtime",
                                 f"{shared} was answered by the panel")
                self.assertEqual(body.get("path"), shared)
                #: The runtime saw it - the panel dispatched nothing.
                self.assertIsNotNone(self.runtime.last(shared))

    def test_no_action_runs_under_the_prefix_even_with_every_header(self):
        for path in self._panel_post_routes():
            with self.subTest(path=path):
                shared = "/s" + path
                code, headers, body = self.json(
                    shared, method="POST", body={"confirm": True,
                                                 "unit": "f10-dashboard",
                                                 "verb": "restart"},
                    headers={admin.CSRF_HEADER: "1"},
                    authed=True,
                )

                self.assertEqual(code, 404)
                self.assertEqual(body.get("answered_by"), "runtime")
                self.assertNotIn("ok", body)

        self.assertEqual(self.calls, [], "a command ran through /s/")

    def test_the_claude_controls_are_unreachable_under_the_prefix(self):
        """
        The Claude tab is /api/status (its section) plus the claude
        action. Named on their own because they are the box's most
        sensitive surface after pull.
        """
        code, headers, body = self.json("/s/api/status", authed=False)
        self.assertNotIn("claude", body)
        self.assertNotIn("services", body)

        code, headers, body = self.json(
            "/s/api/action/claude", method="POST",
            body={"verb": "stop", "confirm": True},
            headers={admin.CSRF_HEADER: "1"},
        )
        self.assertEqual(code, 404)
        self.assertEqual(self.calls, [])

    def test_the_proxy_allowlists_never_name_a_panel_route(self):
        panel = set(self._panel_get_routes()) - {"/api/diagnostics"}

        self.assertFalse(panel & admin.PROXY_GET)
        self.assertFalse(
            {p for p in admin.PROXY_POST if p.startswith("/api/action/")}
        )


class TheTelemetryUi(ProxyCase):
    """The Drive / Detail / All-data tabs are dashboard/, unchanged."""

    def _file(self, name):
        with open(os.path.join(support.ROOT, "dashboard", name), "rb") as fh:
            return fh.read()

    def test_the_frame_document_is_dashboard_index_html_byte_for_byte(self):
        code, headers, raw = self.request("/dashboard/")

        self.assertEqual(code, 200)
        self.assertEqual(raw, self._file("index.html"))
        self.assertTrue(headers["Content-Type"].startswith("text/html"))

    def test_the_assets_are_the_same_files_live_py_serves(self):
        for path, name, ctype in (
            ("/dashboard/style.css", "style.css", "text/css"),
            ("/dashboard/app.js", "app.js", "text/javascript"),
        ):
            with self.subTest(path=path):
                code, headers, raw = self.request(path)
                self.assertEqual(raw, self._file(name))
                self.assertTrue(headers["Content-Type"].startswith(ctype))

    def test_the_frame_document_may_be_framed_by_this_origin_only(self):
        code, headers, raw = self.request("/dashboard/")

        self.assertEqual(headers.get("X-Frame-Options"), "SAMEORIGIN")
        self.assertIn("frame-ancestors 'self'",
                      headers.get("Content-Security-Policy", ""))
        #: The page that frames it says so too, and nothing else may.
        code, headers, raw = self.request("/")
        self.assertIn("frame-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")

    def test_the_page_frames_it_and_carries_all_six_tabs(self):
        code, headers, raw = self.request("/")
        page = raw.decode("utf-8")

        for tab in ("drive", "detail", "table", "system", "car", "claude"):
            self.assertIn(f'data-tab="{tab}"', page)

        #: Loaded when a telemetry tab opens, from this origin.
        self.assertIn('tele.src = "/dashboard/"', page)
        #: The panel's own copy of the telemetry UI does not exist.
        self.assertNotIn("EventSource(", page)
        self.assertNotIn("herogauge", page)

    def test_the_page_understands_the_runtime_down_body(self):
        code, headers, raw = self.request("/")
        page = raw.decode("utf-8")

        self.assertIn("runtime not running", page)
        self.assertIn("recording.up", page)


class ListenerList(unittest.TestCase):
    """`bind` is a list now; a string still works; wildcards never do."""

    def test_the_forms_a_config_may_carry(self):
        self.assertEqual(admin.listen_addresses("192.168.1.50"),
                         ["192.168.1.50"])
        self.assertEqual(admin.listen_addresses(["192.168.1.50", "10.77.0.10"]),
                         ["192.168.1.50", "10.77.0.10"])
        #: The environment override is always a string.
        self.assertEqual(admin.listen_addresses(" 192.168.1.50, 10.77.0.10 "),
                         ["192.168.1.50", "10.77.0.10"])
        self.assertEqual(admin.listen_addresses(""), [])
        self.assertEqual(admin.listen_addresses([]), [])

    def test_a_string_bind_in_the_file_still_starts(self):
        """Backwards compatibility with every config written so far."""
        cfg = admin.load_config(None)
        cfg["bind"] = "127.0.0.1"

        self.assertEqual(admin.listen_addresses(cfg["bind"]), ["127.0.0.1"])

    def test_the_environment_override_is_a_list_too(self):
        os.environ["F10_ADMIN_BIND"] = "127.0.0.1,127.0.0.2"
        self.addCleanup(os.environ.pop, "F10_ADMIN_BIND", None)

        cfg = admin.load_config(None)

        self.assertEqual(admin.listen_addresses(cfg["bind"]),
                         ["127.0.0.1", "127.0.0.2"])

    def _main_with(self, bind):
        """
        main() with serve() stubbed: a bind that is NOT refused reaches
        the stub, which closes the listeners and returns 0 - so a
        regression in the refusal fails the assertion on the return
        code instead of serving for ever and hanging the suite.
        """
        path = os.path.join(tempfile.mkdtemp(), "config.json")

        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"bind": bind, "port": 0, "username": USER,
                       "password": PASSWORD}, fh)

        err = io.StringIO()
        reached = []

        def stub_serve(servers, pending, port, handler, **kwargs):
            reached.append([s.server_address for s in servers])

            for server in servers:
                server.server_close()

            return 0

        with mock.patch.object(admin, "serve", stub_serve), \
                contextlib.redirect_stderr(err):
            code = admin.main(["--config", path])

        return code, err.getvalue(), reached

    def test_a_wildcard_anywhere_in_the_list_is_refused(self):
        for bind in (["0.0.0.0"], ["127.0.0.1", "0.0.0.0"],
                     ["::", "127.0.0.1"], "127.0.0.1,0.0.0.0"):
            with self.subTest(bind=bind):
                code, err, reached = self._main_with(bind)
                self.assertEqual(code, 2)
                self.assertIn("refusing to bind", err)
                self.assertEqual(reached, [])

    def test_every_spelling_of_every_interface_is_refused(self):
        """
        The kernel binds INADDR_ANY for all of these; a string match on
        "0.0.0.0" / "::" saw none of them. `[::]` is the other failure:
        it never binds, and would have sat in the retry loop for ever.
        """
        for bind in ("0", "0.0", "00.0.0.0", "::0", "0::0",
                     "::ffff:0.0.0.0", "[::]", "127.0.0.1, 0",
                     ["127.0.0.1", "::0"], "0000::", "::ffff:0:0",
                     "localhost", "any"):
            with self.subTest(bind=bind):
                code, err, reached = self._main_with(bind)
                self.assertEqual(code, 2, err)
                self.assertIn("refusing to bind", err)
                self.assertEqual(reached, [])

    def test_a_named_address_is_bound_and_served(self):
        """The control for the stub: a loopback literal gets through
        to serve() with one listener on it."""
        for bind in ("127.0.0.1", ["127.0.0.1", "::1"]):
            with self.subTest(bind=bind):
                code, err, reached = self._main_with(bind)

                if code != 0 and "::1" in str(bind) and "cannot listen" in err:
                    self.skipTest("no IPv6 loopback on this host")

                self.assertEqual(code, 0, err)
                self.assertEqual(len(reached), 1)
                self.assertEqual(len(reached[0]),
                                 len(admin.listen_addresses(bind)))

    def test_bind_refusal_is_semantic(self):
        self.assertIsNone(admin.bind_refusal("127.0.0.1"))
        self.assertIsNone(admin.bind_refusal("10.77.0.10"))
        self.assertIsNone(admin.bind_refusal("::1"))
        self.assertIsNone(admin.bind_refusal("::ffff:192.168.1.50"))
        self.assertIn("wildcard", admin.bind_refusal("0.0.0.0"))
        self.assertIn("wildcard", admin.bind_refusal("::"))
        self.assertIn("wildcard", admin.bind_refusal("::ffff:0.0.0.0"))
        self.assertIn("not an IP", admin.bind_refusal("[::]"))
        self.assertIn("not an IP", admin.bind_refusal("pi.local"))

    def test_an_empty_list_is_refused(self):
        code, err, reached = self._main_with([])

        self.assertEqual(code, 2)
        self.assertIn("empty", err)

    def test_one_listener_per_address(self):
        handler = admin.make_handler(dict(admin.DEFAULTS, username=USER,
                                          password=PASSWORD))
        servers, failed = admin.bind_all(["127.0.0.1", "127.0.0.1"], 0, handler)

        for server in servers:
            self.addCleanup(server.server_close)

        self.assertEqual(failed, [])
        self.assertEqual(len(servers), 2)
        self.assertNotEqual(servers[0].server_address[1],
                            servers[1].server_address[1])
        self.assertTrue(servers[0].url.startswith("http://127.0.0.1:"))

    def test_an_address_that_cannot_be_bound_does_not_take_the_rest_down(self):
        handler = admin.make_handler(dict(admin.DEFAULTS, username=USER,
                                          password=PASSWORD))
        #: 192.0.2.1 is TEST-NET-1: no host has it.
        servers, failed = admin.bind_all(["127.0.0.1", "192.0.2.1"], 0, handler)

        for server in servers:
            self.addCleanup(server.server_close)

        self.assertEqual(len(servers), 1)
        self.assertEqual([address for address, _ in failed], ["192.0.2.1"])

    def test_a_late_address_is_bound_on_retry(self):
        """
        wg0 comes up after the panel. The address it will have cannot
        be bound at start; serve() keeps trying and adds the listener
        when it can, without restarting the LAN one.
        """
        handler = admin.make_handler(dict(admin.DEFAULTS, username=USER,
                                          password=PASSWORD))
        #: Occupy a port so the first attempt fails the way an absent
        #: interface does: with an OSError from bind().
        blocker = admin.Listener(("127.0.0.1", 0), handler)
        port = blocker.server_address[1]
        servers = []
        pending = ["127.0.0.1"]
        stop = threading.Event()
        out = io.StringIO()

        def run():
            with contextlib.redirect_stdout(out):
                admin.serve(servers, pending, port, handler, retry_s=0.05,
                            stop=stop)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        time.sleep(0.2)
        self.assertEqual(servers, [])
        self.assertEqual(pending, ["127.0.0.1"])

        blocker.server_close()
        deadline = time.time() + 5

        while pending and time.time() < deadline:
            time.sleep(0.05)

        self.assertEqual(pending, [])
        self.assertEqual(len(servers), 1)

        #: And it answers.
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/healthz", timeout=5
        ) as response:
            self.assertEqual(response.read(), b"ok\n")

        stop.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())


class DeploymentShape(unittest.TestCase):
    ADMIN = os.path.join(support.ROOT, "hardware", "raspberry-pi", "admin")
    F10PI = os.path.join(support.ROOT, "hardware", "raspberry-pi", "f10pi")

    def _read(self, *parts):
        with open(os.path.join(*parts), encoding="utf-8") as fh:
            return fh.read()

    def test_the_runtime_unit_holds_live_py_on_the_loopback(self):
        """
        The panel is the front door; the runtime's port is not a second
        one. run_car.sh forwards its arguments, so the unit passes the
        host through it; live.py's own default stays 0.0.0.0 for a
        laptop run.
        """
        unit = self._read(self.F10PI, "systemd", "f10-dashboard.service")
        exec_line = [l for l in unit.splitlines() if l.startswith("ExecStart=")]

        self.assertEqual(len(exec_line), 1)
        self.assertIn("run_car.sh --host 127.0.0.1", exec_line[0])

        script = self._read(support.ROOT, "run_car.sh")
        self.assertIn('"$@"', script)

        live = self._read(support.ROOT, "live.py")
        self.assertIn('ap.add_argument("--host", default="0.0.0.0")', live)

    def test_the_example_config_documents_the_listener_list(self):
        example = json.loads(self._read(self.ADMIN, "config.example.json"))

        self.assertIsInstance(example["bind"], list)
        self.assertGreaterEqual(len(example["bind"]), 2)

        for address in example["bind"]:
            self.assertNotIn(address, ("0.0.0.0", "::"))

        self.assertEqual(example["dashboard_url"], "http://127.0.0.1:8080")
        self.assertNotIn("diagnostics_url", example)
        self.assertEqual(example["trusted_proxies"], [])

    def test_the_installer_detects_both_addresses(self):
        script = self._read(self.ADMIN, "install.sh")

        self.assertIn("wg0", script)
        self.assertIn("dashboard_url", script)

    def test_no_new_action_and_the_sudoers_grant_is_unchanged(self):
        """#40 adds a proxy, not a privilege."""
        granted = [
            line.split("NOPASSWD:", 1)[1].strip()
            for line in self._read(self.ADMIN, "f10-admin.sudoers").splitlines()
            if line.strip().startswith("@PI_USER@") and "NOPASSWD:" in line
        ]

        #: Six systemctl verbs on two units, reboot, poweroff - as before.
        self.assertEqual(len(granted), 8)
        self.assertTrue(all(g.startswith(("/usr/bin/systemctl ", "/sbin/"))
                            for g in granted))
        self.assertEqual(len(admin.ACTIONS), 9)

    def test_the_readme_states_the_exposure(self):
        readme = self._read(self.ADMIN, "README.md")

        self.assertIn("/s/", readme)
        self.assertIn("dashboard_url", readme)
        self.assertIn("#41", readme)


if __name__ == "__main__":
    unittest.main()
