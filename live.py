#!/usr/bin/env python3
"""
live.py - real-time engine telemetry for a BMW F10 over ENET.

Read-only. It speaks HSFZ (BMW's diagnostic-over-IP framing) to the
central gateway on TCP 6801, routes standard OBD-2 service 01 requests
to the DDE, and serves a live HTML dashboard over SSE.

    python3 live.py                 # discover car, serve on :8080
    python3 live.py --ip 169.254.x.x
    python3 live.py --demo          # no car needed, simulated data

Nothing is written to the vehicle: only service 0x01 (current data)
requests are sent, plus HSFZ alive-check replies.

Which channels exist, how their bytes decode and how often they are read
is not in this file: it comes from the versioned mapping files under
mappings/, loaded through bmwdiag.mapping. See
docs/MAPPING_ARCHITECTURE.md.
"""

import argparse
import json
import math
import os
import queue
import re
import signal
import socket
import sqlite3
import struct
import subprocess
import urllib.parse
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from bmwdiag.mapping import (
    MappingError,
    MappingExecutor,
    MappingRegistry,
    PollingPlan,
    ResolvedProfile,
)
from bmwdiag.mapping.model import PollingClassDef
from bmwdiag.mapping.polling import resolve_classes
from bmwdiag.mapping.registry import AllCapabilities
from bmwdiag.obd import (
    OBD_SUPPORT_PIDS,
    ObdCapabilitySet,
    walk_supported_pids,
)
from bmwdiag.obd.capability import ENGINE_PID


ENET_DISCOVERY_PORT = 6811
ENET_DIAGNOSTIC_PORT = 6801

DISCOVERY_PACKET = bytes.fromhex("00 00 00 00 00 11")

HSFZ_DIAG_REQ = 0x0001
HSFZ_DIAG_ACK = 0x0002
HSFZ_DIAG_NACK = 0x0003
#
# The F10 ZGW answers an unroutable address with 0x0043, not 0x0003.
# Observed on the development car; treat both as 'nobody home'.
#
HSFZ_NACK_CONTROLS = (0x0003, 0x0043, 0x00FF)
HSFZ_ALIVE_REQ = 0x0040
HSFZ_ALIVE_RESP = 0x0041

TESTER_ADDR = 0xF4
DDE_ADDR = 0x12          # engine ECU on F-series
GATEWAY_ADDR = 0x10      # ZGW

#
# OBD functional/broadcast addresses. A request here should make every
# engine-capable ECU answer at once, which is the cheapest discovery.
#
FUNCTIONAL_ADDRS = [0xDF, 0x33]

#
# Probed before falling back to a full 0x00-0xFF sweep. Order is just an
# optimisation - the address is confirmed by capability, never assumed.
#
LIKELY_ADDRS = [0x12, 0x10, 0x13, 0x01, 0x40, 0x60, 0x18, 0x22, 0x29, 0x63, 0x78]

#
# socket.timeout only became an alias of TimeoutError in 3.10.
#
TIMEOUTS = (TimeoutError, socket.timeout)

#
# --demo invents its data, so it invents its VIN too. Real VINs identify
# a real car and are kept out of the repository; see local/VEHICLES.md.
#
DEMO_VIN = "DEMO0000000000000"


# --------------------------------------------------------------- mappings


#
# Diagnostic knowledge - which request produces which channel, how its
# bytes decode, how often it is polled and where it came from - lives in
# these files, not in this module.
#
DEFAULT_MAPPING_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "mappings"
)


def load_registry(path: str) -> MappingRegistry:
    """
    Load the production mapping set.

    Files that mark themselves `production: false` - the synthetic
    examples - are skipped here. They are still validated by
    `python3 -m bmwdiag.mapping validate mappings/` and by the tests, so
    a broken fixture cannot go unnoticed, but no invented identifier ever
    reaches the vehicle.
    """
    return MappingRegistry.from_tree(path, production_only=True)


def polling_classes(registry: MappingRegistry, args) -> Dict[str, PollingClassDef]:
    """
    Resolve polling classes, letting the CLI have the last word.

    `--slow-every` is defined in poll-loop cycles, so `slow` stays a
    cycle-based class. Mappings may declare hz- or seconds-based classes
    for requests that should not be tied to the loop cadence.
    """
    return resolve_classes(
        registry.polling_classes(),
        {"slow": PollingClassDef("slow", "cycles", float(args.slow_every), 1)},
    )


def numeric_only(values: Dict[str, Any]) -> Dict[str, float]:
    """
    Samples the recorder can store.

    `samples.value` is REAL, so a future enum or ASCII channel is shown on
    the dashboard but not logged. No production mapping has one today.
    """
    return {
        key: value for key, value in values.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


# ------------------------------------------------------------------- HSFZ


class HsfzError(Exception):
    pass


class HsfzNack(HsfzError):
    """Gateway refused to route to that address - nobody home."""


class HsfzClient:
    """Minimal HSFZ client: 4-byte length, 2-byte control, payload."""

    def __init__(
        self,
        ip: str,
        local_ip: Optional[str] = None,
        src: int = TESTER_ADDR,
        dst: int = DDE_ADDR,
        timeout: float = 3.0,
    ):
        self.ip = ip
        self.local_ip = local_ip
        self.src = src
        self.dst = dst
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self.buf = bytearray()

    def connect(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)

        if self.local_ip:
            sock.bind((self.local_ip, 0))

        sock.connect((self.ip, ENET_DIAGNOSTIC_PORT))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        self.sock = sock
        self.buf.clear()

    def reconnect(self) -> None:
        self.close()
        self.connect()

    def request_safe(
        self,
        data: bytes,
        timeout: Optional[float] = None,
        dst: Optional[int] = None,
    ) -> bytes:
        """
        request(), but transparently reconnects if the gateway hangs up.

        The F10 ZGW drops the TCP session outright for some addresses
        (0x0D on the development car). Without this, every probe after such
        an address fails with BrokenPipeError and a scan silently reports
        an empty bus.
        """
        try:
            return self.request(data, timeout, dst)
        except socket.timeout:
            raise
        except (ConnectionResetError, BrokenPipeError, HsfzError) as exc:
            if isinstance(exc, HsfzError) and not isinstance(exc, ConnectionError):
                if "closed" not in str(exc):
                    raise

            self.reconnect()

            return self.request(data, timeout, dst)
        except OSError:
            self.reconnect()

            return self.request(data, timeout, dst)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    # -- framing ----------------------------------------------------

    def _fill(self, deadline: float) -> None:
        assert self.sock is not None

        remaining = deadline - time.monotonic()

        if remaining <= 0:
            raise TimeoutError("HSFZ read timeout")

        self.sock.settimeout(remaining)

        chunk = self.sock.recv(8192)

        if not chunk:
            raise HsfzError("gateway closed the connection")

        self.buf.extend(chunk)

    def _read_frame(self, deadline: float) -> Tuple[int, bytes]:
        while True:
            if len(self.buf) >= 6:
                length, control = struct.unpack(">IH", self.buf[:6])

                if length > 0x00100000:
                    raise HsfzError(f"absurd HSFZ length {length}")

                if len(self.buf) >= 6 + length:
                    payload = bytes(self.buf[6:6 + length])
                    del self.buf[:6 + length]
                    return control, payload

            self._fill(deadline)

    def _drain(self) -> None:
        """Throw away buffered frames left over from a previous request."""
        self.buf.clear()

        if self.sock is None:
            return

        self.sock.setblocking(False)

        try:
            while True:
                if not self.sock.recv(8192):
                    break
        except (BlockingIOError, OSError):
            pass
        finally:
            self.sock.setblocking(True)

    def _send(self, control: int, payload: bytes) -> None:
        assert self.sock is not None
        self.sock.sendall(struct.pack(">IH", len(payload), control) + payload)

    # -- request/response -------------------------------------------

    def request(
        self,
        data: bytes,
        timeout: Optional[float] = None,
        dst: Optional[int] = None,
        expect_src: Optional[int] = None,
    ) -> bytes:
        """Send a UDS/OBD payload, return the ECU's response bytes."""
        if self.sock is None:
            raise HsfzError("not connected")

        target = self.dst if dst is None else dst
        want_sid = data[0] + 0x40
        want_src = target if expect_src is None else expect_src

        #
        # Discard anything still queued from an earlier exchange, so a
        # straggler cannot be mistaken for this request's answer.
        #
        self._drain()

        self._send(HSFZ_DIAG_REQ, bytes([self.src, target]) + data)

        deadline = time.monotonic() + (timeout or self.timeout)

        while True:
            control, payload = self._read_frame(deadline)

            if control == HSFZ_ALIVE_REQ:
                self._send(HSFZ_ALIVE_RESP, struct.pack(">H", self.src))
                continue

            if control in HSFZ_NACK_CONTROLS:
                #
                # The gateway could not route to that address. Definitive,
                # and far cheaper than waiting out the timeout.
                #
                raise HsfzNack(f"gateway will not route to 0x{target:02X}")

            if control == HSFZ_DIAG_ACK:
                #
                # Transport-level ack of our request, not the answer.
                #
                continue

            if control != HSFZ_DIAG_REQ or len(payload) < 3:
                continue

            if payload[1] != self.src:
                continue

            if payload[0] != want_src:
                continue

            body = payload[2:]

            #
            # Correlate the reply with this request. Without this a frame
            # that arrived late from a previous request is happily
            # returned as the answer to this one.
            #
            if body[0] != want_sid and body[0] != 0x7F:
                continue

            if body[0] == 0x7F and (len(body) < 2 or body[1] != data[0]):
                continue

            if body[0] == 0x7F and len(body) >= 3:
                nrc = body[2]

                if nrc == 0x78:
                    #
                    # responsePending - the ECU asked for more time.
                    #
                    deadline = time.monotonic() + 2.0
                    continue

                raise HsfzError(
                    f"negative response to 0x{body[1]:02X}: NRC 0x{nrc:02X}"
                )

            return body

    def collect(self, data: bytes, dst: int, window: float) -> List[Tuple[int, bytes]]:
        """Broadcast a payload and gather every ECU that answers."""
        if self.sock is None:
            raise HsfzError("not connected")

        self._send(HSFZ_DIAG_REQ, bytes([self.src, dst]) + data)

        deadline = time.monotonic() + window
        seen: List[Tuple[int, bytes]] = []

        while time.monotonic() < deadline:
            try:
                control, payload = self._read_frame(deadline)
            except TIMEOUTS:
                break
            except HsfzError:
                break

            if control == HSFZ_ALIVE_REQ:
                self._send(HSFZ_ALIVE_RESP, struct.pack(">H", self.src))
                continue

            if control != HSFZ_DIAG_REQ or len(payload) < 3:
                continue

            if payload[1] != self.src:
                continue

            body = payload[2:]

            if body[0] == 0x41 and not any(a == payload[0] for a, _ in seen):
                seen.append((payload[0], body))

        return seen


# ------------------------------------------------------- transport adapter


class HsfzTransport:
    """
    Adapts HsfzClient to bmwdiag.protocol.DiagnosticTransport.

    This three-method surface is the only thing the mapping engine ever
    sees of the vehicle link, which is what keeps sockets, HSFZ framing,
    alive-checks and reconnect policy out of the mapping subsystem - and
    lets every decoder be tested against captured bytes instead of a car.
    """

    def __init__(self, client: "HsfzClient"):
        self.client = client

    def request(
        self,
        payload: bytes,
        *,
        dst: int,
        timeout: Optional[float] = None,
    ) -> bytes:
        return self.client.request(payload, timeout, dst)


# --------------------------------------------------------------- discovery


def find_link_local_ip() -> Optional[str]:
    try:
        out = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return None

    hits = re.findall(r"inet (169\.254\.\d+\.\d+)", out)

    return hits[0] if hits else None


def discover(local_ip: str, timeout: float = 5.0) -> Tuple[str, Optional[str]]:
    """Return (gateway_ip, vin)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((local_ip, 0))
        sock.settimeout(timeout)

        sock.sendto(
            DISCOVERY_PACKET, ("169.254.255.255", ENET_DISCOVERY_PORT)
        )

        data, addr = sock.recvfrom(4096)
    finally:
        sock.close()

    text = data[6:].decode("ascii", errors="replace")
    vin = None

    pos = text.find("BMWVIN")

    if pos >= 0:
        vin = text[pos + 6:pos + 23]

    return addr[0], vin


# ------------------------------------------------------------------- OBD


class ObdSession:
    """
    Mode 01 reader satisfying bmwdiag.protocol.ObdPidReader.

    Standard OBD is the one protocol where the wire framing is not one
    exchange per mapped request: an ECU may answer six PIDs at once, and
    may stop doing so mid-drive. That negotiation lives here rather than
    in the mapping engine, which just asks for PIDs and gets bytes back.
    """

    def __init__(self, client: HsfzClient, pid_len: Optional[Dict[int, int]] = None):
        self.client = client
        self.multi_ok = True
        self.fails: Dict[int, int] = {}
        self.dead: set = set()
        #
        # Byte counts for every PID we may ever ask for, so a multi-PID
        # response can be walked without guessing. The mapped PIDs come
        # from the registry; the support-bitmask PIDs are protocol
        # structure and come from the OBD layer.
        #
        self.pid_len: Dict[int, int] = dict(OBD_SUPPORT_PIDS)
        self.pid_len.update(pid_len or {})

    def _mode01(self, pids: List[int], timeout: Optional[float] = None) -> Dict[int, bytes]:
        resp = self.client.request(bytes([0x01] + pids), timeout)

        if not resp or resp[0] != 0x41:
            raise HsfzError(f"unexpected reply {resp.hex(' ')}")

        out: Dict[int, bytes] = {}
        i = 1

        while i < len(resp):
            pid = resp[i]
            n = self.pid_len.get(pid)

            if n is None or i + 1 + n > len(resp):
                break

            out[pid] = resp[i + 1:i + 1 + n]
            i += 1 + n

        return out

    def read(self, pids: List[int]) -> Dict[int, bytes]:
        """Read a set of PIDs, batching where the ECU allows it."""
        result: Dict[int, bytes] = {}

        if self.multi_ok:
            try:
                for i in range(0, len(pids), 6):
                    batch = pids[i:i + 6]
                    got = self._mode01(batch)

                    if not all(p in got for p in batch):
                        raise HsfzError("incomplete multi-PID response")

                    result.update(got)

                return result
            except HsfzError:
                self.multi_ok = False
                result.clear()

        for pid in pids:
            if pid in self.dead:
                continue

            try:
                result.update(self._mode01([pid]))
                self.fails.pop(pid, None)
            except (HsfzError,) + TIMEOUTS:
                #
                # A PID the ECU ignores costs a full timeout every
                # cycle, so retire it after a few strikes.
                #
                self.fails[pid] = self.fails.get(pid, 0) + 1

                if self.fails[pid] >= 3:
                    self.dead.add(pid)
                    print(f"[!] dropping unresponsive PID 0x{pid:02X}")

                continue

        return result


# --------------------------------------------------------------- recorder


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ended_at   REAL,
    vin        TEXT,
    gateway    TEXT,
    ecu        TEXT,
    ecu_addr   INTEGER
);

CREATE TABLE IF NOT EXISTS params (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    key   TEXT UNIQUE NOT NULL,
    pid   INTEGER,
    label TEXT,
    unit  TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    run_id   INTEGER NOT NULL,
    ts       REAL NOT NULL,
    param_id INTEGER NOT NULL,
    value    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    run_id  INTEGER,
    ts      REAL NOT NULL,
    kind    TEXT,
    message TEXT
);

CREATE INDEX IF NOT EXISTS samples_run_param_ts ON samples(run_id, param_id, ts);
CREATE INDEX IF NOT EXISTS samples_ts           ON samples(ts);
"""


class Recorder:
    """
    Buffers samples in memory and flushes them to SQLite in chunks, on a
    dedicated thread so a slow disk can never stall the poll loop.

    Only values actually read from the ECU in a given cycle are written -
    carried-forward cache values are not re-logged, so every row in
    `samples` corresponds to one real ECU reading.
    """

    def __init__(self, path: str, chunk: int = 500, interval: float = 2.0):
        self.path = path
        self.chunk = chunk
        self.interval = interval
        self.q: "queue.Queue" = queue.Queue(maxsize=20000)
        self.run_id: Optional[int] = None
        self.param_ids: Dict[str, int] = {}
        self.rows = 0
        self.dropped = 0
        self.db: Optional[sqlite3.Connection] = None
        #
        # Where label/unit/pid for a channel come from. Set once the
        # mapping registry has been resolved against the vehicle, before
        # any sample is written.
        #
        self.meta_source: Optional[ResolvedProfile] = None
        self.thread = threading.Thread(target=self._writer, daemon=True)

    def set_metadata(self, profile: ResolvedProfile) -> None:
        """Point the params table at the resolved mapping registry."""
        self.meta_source = profile

    # -- called from the poll thread --------------------------------

    def start_run(self, vin, gateway, ecu, ecu_addr) -> None:
        self.q.put(("run", (time.time(), vin, gateway, ecu, ecu_addr)))

    def event(self, kind: str, message: str) -> None:
        try:
            self.q.put_nowait(("event", (time.time(), kind, message)))
        except queue.Full:
            pass

    def write(self, ts: float, values: Dict[str, float]) -> None:
        try:
            self.q.put_nowait(("s", (ts, dict(values))))
        except queue.Full:
            self.dropped += 1

    def close(self) -> None:
        self.q.put(("stop", None))
        self.thread.join(timeout=5.0)

    # -- writer thread ----------------------------------------------

    def open(self) -> None:
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.commit()
        self.thread.start()

    def _param_id(self, key: str) -> int:
        if key in self.param_ids:
            return self.param_ids[key]

        #
        # `pid` stays NULL for anything that did not come from OBD - a
        # derived channel today, a proprietary job later. The schema
        # already allows it, so nothing here needs migrating.
        #
        if self.meta_source is not None:
            pid, label, unit = self.meta_source.param_row(key)
        else:
            pid, label, unit = None, key, ""

        row = (key, pid, label, unit)

        self.db.execute(
            "INSERT OR IGNORE INTO params(key, pid, label, unit) VALUES (?,?,?,?)",
            row,
        )

        ident = self.db.execute(
            "SELECT id FROM params WHERE key = ?", (key,)
        ).fetchone()[0]

        self.param_ids[key] = ident

        return ident

    def _flush(self, pending: List[Tuple]) -> None:
        if not pending or self.db is None:
            return

        self.db.executemany(
            "INSERT INTO samples(run_id, ts, param_id, value) VALUES (?,?,?,?)",
            pending,
        )
        self.db.commit()

        self.rows += len(pending)
        pending.clear()

    def _writer(self) -> None:
        pending: List[Tuple] = []
        last = time.monotonic()

        while True:
            timeout = max(0.05, self.interval - (time.monotonic() - last))

            try:
                kind, payload = self.q.get(timeout=timeout)
            except queue.Empty:
                kind, payload = None, None

            if kind == "stop":
                self._flush(pending)

                if self.run_id is not None:
                    self.db.execute(
                        "UPDATE runs SET ended_at = ? WHERE id = ?",
                        (time.time(), self.run_id),
                    )
                    self.db.commit()

                self.db.close()
                return

            if kind == "run":
                self._flush(pending)

                cur = self.db.execute(
                    "INSERT INTO runs(started_at, vin, gateway, ecu, ecu_addr) "
                    "VALUES (?,?,?,?,?)",
                    payload,
                )
                self.db.commit()
                self.run_id = cur.lastrowid

            elif kind == "event" and self.run_id is not None:
                ts, ekind, msg = payload
                self.db.execute(
                    "INSERT INTO events(run_id, ts, kind, message) VALUES (?,?,?,?)",
                    (self.run_id, ts, ekind, msg),
                )
                self.db.commit()

            elif kind == "s" and self.run_id is not None:
                ts, values = payload

                for key, value in values.items():
                    pending.append((self.run_id, ts, self._param_id(key), value))

            if len(pending) >= self.chunk or (
                pending and time.monotonic() - last >= self.interval
            ):
                self._flush(pending)
                last = time.monotonic()


# ------------------------------------------------------------------ state


class Telemetry:
    def __init__(self):
        self.lock = threading.Lock()
        self.snapshot: Dict = {
            "connected": False,
            "status": "starting",
            "vin": None,
            "gateway": None,
            "ecu": None,
            "ecu_addr": None,
            "ecus": [],
            "rows": 0,
            "dropped": 0,
            "hz": 0.0,
            "latency_ms": 0.0,
            "ts": 0.0,
            "values": {},
        }
        self.meta: List[Dict] = []
        self.meta_version = 0
        self.version = 0
        self.cond = threading.Condition(self.lock)

    def set_meta(self, meta: List[Dict]) -> None:
        """
        Publish the channel list.

        The rows come from the resolved mapping registry, in the same
        shape the dashboard has always consumed. Nothing downstream can
        tell whether a channel was read over OBD, over UDS, from a
        proprietary job, or computed from other channels.
        """
        with self.cond:
            self.meta = list(meta)
            self.meta_version += 1

    def update(self, **kwargs) -> None:
        with self.cond:
            self.snapshot.update(kwargs)
            self.snapshot["ts"] = time.time()
            self.snapshot["meta_version"] = self.meta_version
            self.version += 1
            self.cond.notify_all()

    def get(self) -> Dict:
        with self.cond:
            return dict(self.snapshot)

    def wait(self, seen: int, timeout: float = 2.0) -> Tuple[int, Dict]:
        with self.cond:
            if self.version == seen:
                self.cond.wait(timeout)

            return self.version, dict(self.snapshot)


# ------------------------------------------------------------------ poller


@dataclass
class EcuInfo:
    addr: int
    supported: set
    name: Optional[str] = None

    @property
    def is_engine(self) -> bool:
        #
        # The engine ECU is the one advertising engine speed. Address is
        # not evidence; the supported-PID bitmask is.
        #
        return ENGINE_PID in self.supported

    def capabilities(self) -> ObdCapabilitySet:
        """What the mapping registry resolves this ECU against."""
        return ObdCapabilitySet(self.supported)

    def score(self, pids: Iterable[int] = ()) -> int:
        """How many of the mapped PIDs this ECU advertises."""
        return self.capabilities().score(pids)

    def label(self) -> str:
        return f"0x{self.addr:02X}" + (f" ({self.name})" if self.name else "")


def read_supported(client: HsfzClient, addr: int, timeout: float) -> set:
    """
    Walk the Mode 01 supported-PID bitmask blocks for one address.

    The traversal itself lives in bmwdiag.obd: `01 00`, the bitmask
    layout and the next-block bit are OBD protocol structure, and keeping
    them out of the generic mapping layer is what lets a proprietary
    capability provider sit beside this one later.
    """
    return walk_supported_pids(
        lambda payload: client.request_safe(payload, timeout=timeout, dst=addr)
    )


def read_ecu_name(client: HsfzClient, addr: int, timeout: float) -> Optional[str]:
    """Mode 09 PID 0x0A - ECU name, when the ECU bothers to implement it."""
    try:
        resp = client.request(bytes([0x09, 0x0A]), timeout=timeout, dst=addr)
    except (HsfzError,) + TIMEOUTS:
        return None

    if len(resp) < 3 or resp[0] != 0x49:
        return None

    text = "".join(
        chr(b) for b in resp[3:] if 32 <= b < 127
    ).strip()

    return text or None


def probe(client: HsfzClient, addr: int, timeout: float) -> Optional[EcuInfo]:
    """Ask one address for its supported PIDs. None = nobody there."""
    try:
        resp = client.request_safe(bytes([0x01, 0x00]), timeout=timeout, dst=addr)
    except HsfzNack:
        return None
    except (HsfzError,) + TIMEOUTS:
        return None

    if len(resp) < 6 or resp[0] != 0x41:
        return None

    return EcuInfo(addr=addr, supported=read_supported(client, addr, timeout))


def discover_ecus(client: HsfzClient, args, report=lambda msg: None) -> List[EcuInfo]:
    """
    Find every ECU on the bus that answers OBD service 01, without
    assuming any address. Three escalating phases, cheapest first.
    """
    found: Dict[int, EcuInfo] = {}
    tried: set = set()

    # -- phase A: functional broadcast ------------------------------
    for fn in FUNCTIONAL_ADDRS:
        tried.add(fn)

        try:
            answers = client.collect(bytes([0x01, 0x00]), fn, window=args.scan_timeout * 3)
        except (HsfzError,) + TIMEOUTS:
            continue

        for src, _ in answers:
            if src in found:
                continue

            found[src] = EcuInfo(
                addr=src, supported=read_supported(client, src, args.scan_timeout)
            )

        if found:
            report(
                f"broadcast to 0x{fn:02X} answered by "
                + ", ".join(f"0x{a:02X}" for a in sorted(found))
            )
            break

    # -- phase B: likely addresses ----------------------------------
    if not any(e.is_engine for e in found.values()):
        for addr in LIKELY_ADDRS:
            if addr in tried or addr in found:
                continue

            tried.add(addr)
            info = probe(client, addr, args.scan_timeout)

            if info:
                found[addr] = info
                report(f"0x{addr:02X} answered ({len(info.supported)} PIDs)")

                if info.is_engine and not args.scan_full:
                    break

    # -- phase C: full sweep ----------------------------------------
    need_sweep = args.scan_full or not any(e.is_engine for e in found.values())

    if need_sweep:
        report("sweeping 0x00-0xFF ...")

        for addr in range(0x100):
            if addr in tried or addr == client.src:
                continue

            if addr and addr % 32 == 0:
                report(f"swept to 0x{addr:02X} of 0xFF ...")

            info = probe(client, addr, args.scan_timeout)

            if info:
                found[addr] = info
                report(f"0x{addr:02X} answered ({len(info.supported)} PIDs)")

                if info.is_engine and not args.scan_full:
                    break

    for info in found.values():
        info.name = read_ecu_name(client, info.addr, args.scan_timeout)

    return sorted(found.values(), key=lambda e: e.addr)


def connect_and_discover(
    ip: str, local_ip: Optional[str], args, report=lambda msg: None,
    prefer: Optional[int] = None, score_pids: Sequence[int] = (),
) -> Tuple[HsfzClient, EcuInfo, List[EcuInfo]]:
    """One TCP session serves the whole scan - no reconnect per address."""
    client = HsfzClient(ip, local_ip, timeout=3.0)
    client.connect()

    try:
        forced = args.ecu if args.ecu is not None else prefer

        if forced is not None:
            info = probe(client, forced, 3.0)

            #
            # `prefer` is the address found last time round. If it still
            # answers we skip the sweep entirely, so recovering from a
            # dropped connection costs one request instead of a scan.
            #
            if info is None and args.ecu is None:
                report(f"0x{forced:02X} no longer answering, rescanning")
            elif info is None:
                raise HsfzError(f"forced ECU 0x{forced:02X} did not answer")
            else:
                info.name = read_ecu_name(client, forced, 1.0)
                return client, info, [info]

        ecus = discover_ecus(client, args, report)

        if not ecus:
            raise HsfzError(
                "no ECU answered OBD service 01 - is terminal 15 / ignition on?"
            )

        engines = [e for e in ecus if e.is_engine]

        if not engines:
            raise HsfzError(
                "ECUs answered ("
                + ", ".join(e.label() for e in ecus)
                + ") but none advertises PID 0x0C (engine speed)"
            )

        #
        # Pick the engine ECU that advertises most of what the mappings
        # actually ask for, rather than most PIDs in the abstract.
        #
        best = max(engines, key=lambda e: e.score(score_pids))
        client.dst = best.addr

        return client, best, ecus
    except Exception:
        client.close()
        raise


def poll_loop(
    tel: Telemetry,
    args,
    rec: Optional["Recorder"] = None,
    registry: Optional[MappingRegistry] = None,
) -> None:
    registry = registry or load_registry(args.mappings)
    score_pids = registry.obd_pids()
    local_ip = args.local_ip or find_link_local_ip()
    last_engine: Optional[int] = None

    while True:
        client: Optional[HsfzClient] = None

        try:
            ip, vin = args.ip, args.vin

            if ip is None:
                tel.update(status="discovering vehicle", connected=False)

                if not local_ip:
                    raise HsfzError(
                        "no 169.254.x.x interface found - pass --local-ip"
                    )

                ip, vin = discover(local_ip)
                print(f"[+] found gateway {ip} (VIN {vin})")

            tel.update(status=f"connecting to {ip}", gateway=ip, vin=vin)

            tel.update(status="scanning for ECUs")

            def report(msg: str) -> None:
                print(f"[scan] {msg}", flush=True)
                tel.update(status=f"scan: {msg}")

            client, engine, ecus = connect_and_discover(
                ip, local_ip, args, report, prefer=last_engine,
                score_pids=score_pids,
            )
            last_engine = engine.addr

            print(
                f"[+] engine ECU {engine.label()} - "
                f"{len(engine.supported)} PIDs advertised"
            )

            others = [e.label() for e in ecus if e.addr != engine.addr]

            if others:
                print(f"[+] other OBD-capable ECUs: {', '.join(others)}")

            tel.update(
                ecu=engine.label(),
                ecu_addr=engine.addr,
                ecus=[e.label() for e in ecus],
            )

            if rec is not None:
                rec.start_run(vin, ip, engine.label(), engine.addr)
                rec.event("connect", f"engine ECU {engine.label()}")

            #
            # Resolve the mapping registry against this particular ECU.
            # `discovered_engine` is a late-bound target: mapping files
            # never name an address, the scan does.
            #
            profile = registry.resolve(
                engine.capabilities(),
                config={"tank": args.tank},
                targets={"discovered_engine": engine.addr},
            )

            if not profile.requests:
                raise HsfzError("ECU reports no usable PIDs")

            if rec is not None:
                rec.set_metadata(profile)

            session = ObdSession(client, profile.obd_pid_lengths())
            plan = PollingPlan(profile.requests, polling_classes(registry, args))
            executor = MappingExecutor(
                profile,
                transport=HsfzTransport(client),
                obd_reader=session,
            )

            tel.set_meta(profile.meta())

            counts = plan.counts()

            print(
                f"[+] polling {counts.get('fast', 0)} fast + "
                f"{counts.get('slow', 0)} slow PIDs: "
                + ", ".join(profile.signal_keys())
            )

            values: Dict[str, float] = {}
            cycle = 0
            interval = 1.0 / args.rate
            hz_mark = time.monotonic()
            hz_count = 0
            hz = 0.0

            tel.update(status="live", connected=True)

            while True:
                started = time.monotonic()

                #
                # The plan schedules requests, not channel names, so two
                # signals decoded from one reply cost one exchange.
                #
                fresh = executor.execute(plan.due(cycle, started))
                values.update(fresh)

                derived = profile.apply_derived(values, fresh)
                values.update(derived)
                fresh.update(derived)

                if rec is not None and fresh:
                    rec.write(time.time(), numeric_only(fresh))

                latency = (time.monotonic() - started) * 1000.0
                hz_count += 1
                now = time.monotonic()

                if now - hz_mark >= 1.0:
                    hz = hz_count / (now - hz_mark)
                    hz_mark, hz_count = now, 0

                tel.update(
                    connected=True,
                    status="live",
                    values=dict(values),
                    latency_ms=round(latency, 1),
                    hz=round(hz, 1),
                    rows=rec.rows if rec else 0,
                    dropped=rec.dropped if rec else 0,
                )

                cycle += 1

                sleep_for = interval - (time.monotonic() - started)

                if sleep_for > 0:
                    time.sleep(sleep_for)

        except KeyboardInterrupt:
            raise
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"

            if isinstance(exc, ConnectionResetError):
                #
                # The ZGW serves one HSFZ client at a time; a second tool
                # on the same cable shows up as a reset, not a refusal.
                #
                msg += " (another tool connected to the gateway?)"

            print(f"[!] {msg}", flush=True)
            tel.update(connected=False, status=msg)

            if rec is not None:
                rec.event("error", msg)
        finally:
            if client is not None:
                client.close()

        time.sleep(2.0)


def demo_loop(
    tel: Telemetry,
    args,
    rec: Optional["Recorder"] = None,
    registry: Optional[MappingRegistry] = None,
) -> None:
    registry = registry or load_registry(args.mappings)

    #
    # No car, so no capability discovery: take every mapped channel. The
    # metadata is the real thing from the registry; only the numbers below
    # are synthetic.
    #
    profile = registry.resolve(
        AllCapabilities(),
        config={"tank": args.tank},
        targets={"discovered_engine": DDE_ADDR},
    )

    tel.set_meta(profile.meta())

    if rec is not None:
        rec.set_metadata(profile)

    tel.update(
        connected=True, status="live (demo)", ecu="demo",
        gateway="127.0.0.1", vin=DEMO_VIN,
    )

    if rec is not None:
        rec.start_run(DEMO_VIN, "127.0.0.1", "demo", 0x12)

    t0 = time.monotonic()

    while True:
        t = time.monotonic() - t0
        drive = 0.5 + 0.5 * math.sin(t / 7.0)
        rpm = 780 + drive * 3200
        boost = max(-0.05, (drive ** 2) * 1.6)
        baro = 99.0

        values = {
            "rpm": round(rpm),
            "map": round(baro + boost * 100, 1),
            "boost": round(boost, 3),
            "load": round(10 + drive * 85, 1),
            "throttle": round(drive * 100, 1),
            "speed": round(drive * 130),
            "maf": round(8 + drive * 190, 1),
            "rail": round(280 + drive * 1300),
            "torque": round(drive * 90, 1),
            "coolant": round(min(89, 20 + t * 2), 1),
            "oil": round(min(96, 18 + t * 1.7), 1),
            "iat": round(24 + boost * 12, 1),
            "ambient": 19.0,
            "voltage": round(14.1 - drive * 0.4, 2),
            "baro": baro,
            "fuel": 63.0,
            "fuel_l": round(63.0 / 100.0 * args.tank, 2),
            "fuelrate": round(0.8 + drive * 14, 1),
            "runtime": round(t),
        }

        if rec is not None:
            rec.write(time.time(), numeric_only(values))

        tel.update(
            values=values, latency_ms=round(6 + drive * 4, 1), hz=10.0,
            rows=rec.rows if rec else 0,
            dropped=rec.dropped if rec else 0,
        )

        time.sleep(1.0 / args.rate)


# ------------------------------------------------------------------- page


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>F10 520d - live telemetry</title>
<style>
  :root {
    --bg: #0b0e13;
    --surface: #141922;
    --surface2: #1b2230;
    --line: #263041;
    --grid: #1f2836;
    --text: #e6edf7;
    --muted: #8b97ab;
    --series: #3987e5;
    --good: #199e70;
    --warn: #c98500;
    --bad: #e66767;
    --speed: #9085e9;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px;
    background: var(--bg); color: var(--text);
    font: 13px/1.45 ui-sans-serif, -apple-system, "SF Pro Text", Inter, system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  header { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  h1 { font-size: 15px; margin: 0; letter-spacing: .05em; text-transform: uppercase; }
  .chips { display: flex; gap: 7px; flex-wrap: wrap; margin-left: auto; }
  .chip {
    background: var(--surface); border: 1px solid var(--line); border-radius: 999px;
    padding: 4px 10px; font-size: 11.5px; color: var(--muted);
    font-variant-numeric: tabular-nums; white-space: nowrap;
  }
  .chip b { color: var(--text); font-weight: 600; }
  .dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; margin-right: 6px; }
  .dot.on { background: var(--good); } .dot.off { background: var(--bad); }

  .controls {
    display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
    margin: 14px 0; padding-bottom: 14px; border-bottom: 1px solid var(--line);
  }
  label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
  select, button {
    background: var(--surface); color: var(--text); font: inherit; font-size: 12px;
    border: 1px solid var(--line); border-radius: 8px; padding: 5px 10px; cursor: pointer;
  }
  button.on { background: var(--series); border-color: var(--series); color: #fff; }
  .seg { display: flex; gap: 4px; }

  .gauges { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; max-width: 640px; }
  .gauge { text-align: center; }
  .gauge svg { width: 100%; max-width: 170px; height: auto; }
  .gval { font-size: 26px; font-weight: 650; font-variant-numeric: tabular-nums; }
  .gunit { font-size: 10.5px; color: var(--muted); letter-spacing: .1em; text-transform: uppercase; }

  h2 {
    font-size: 11px; letter-spacing: .12em; text-transform: uppercase;
    color: var(--muted); font-weight: 600; margin: 22px 0 10px;
  }
  .panels { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 10px; }
  .panel {
    background: var(--surface); border: 1px solid var(--line);
    border-radius: 10px; padding: 10px 11px 6px;
  }
  .phead { display: flex; align-items: baseline; gap: 6px; margin-bottom: 2px; }
  .pname { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }
  .pval {
    margin-left: auto; font-size: 17px; font-weight: 640;
    font-variant-numeric: tabular-nums;
  }
  .punit { font-size: 10.5px; color: var(--muted); }
  .prange { display: flex; justify-content: space-between; font-size: 10px; color: var(--muted);
            font-variant-numeric: tabular-nums; margin-top: 1px; }
  .panel canvas { width: 100%; height: 54px; display: block; cursor: crosshair; }
  .badge {
    font-size: 9.5px; padding: 1px 5px; border-radius: 4px; font-weight: 600;
    letter-spacing: .04em; text-transform: uppercase;
  }
  .badge.warn { background: #3a2c08; color: #f0b429; }
  .badge.bad  { background: #3d1d1d; color: #ff8f8f; }
  .empty { color: var(--muted); padding: 22px 0; }
  footer { color: var(--muted); font-size: 11px; margin-top: 20px; }
  .axis { display: flex; justify-content: space-between; color: var(--muted);
          font-size: 10.5px; margin-top: 6px; font-variant-numeric: tabular-nums; }
</style>
</head>
<body>

<header>
  <h1>BMW F10 520d &middot; telemetry</h1>
  <div class="chips">
    <div class="chip"><span id="dot" class="dot off"></span><b id="status">connecting</b></div>
    <div class="chip">VIN <b id="vin">-</b></div>
    <div class="chip">ECU <b id="ecu">-</b></div>
    <div class="chip"><b id="hz">0</b> Hz</div>
    <div class="chip"><b id="lat">0</b> ms</div>
    <div class="chip">logged <b id="rows">0</b></div>
  </div>
</header>

<div class="controls">
  <label for="run">Run</label>
  <select id="run"></select>
  <label>Window</label>
  <div class="seg" id="win"></div>
  <button id="reload">Reload history</button>
  <span id="hint" style="color:var(--muted);font-size:11.5px"></span>
</div>

<div class="gauges">
  <div class="gauge">
    <svg viewBox="0 0 200 165" id="g-rpm"></svg>
    <div class="gval" id="v-rpm">--</div><div class="gunit">rpm</div>
  </div>
  <div class="gauge">
    <svg viewBox="0 0 200 165" id="g-boost"></svg>
    <div class="gval" id="v-boost">--</div><div class="gunit">bar boost</div>
  </div>
  <div class="gauge">
    <svg viewBox="0 0 200 165" id="g-speed"></svg>
    <div class="gval" id="v-speed">--</div><div class="gunit">km/h</div>
  </div>
</div>

<h2>All channels <span id="span" style="text-transform:none;letter-spacing:0"></span></h2>
<div class="panels" id="panels"></div>
<div class="axis"><span id="ax0"></span><span id="ax1"></span></div>

<footer>Each panel has its own vertical scale - values share a time axis, never a y-axis.
Read-only OBD-2 service 01 over HSFZ / ENET.</footer>

<script>
const WINDOWS = [["1m",60],["5m",300],["15m",900],["1h",3600],["all",null]];
let meta = [], metaVersion = -1;
let series = {};            // key -> [[ts, value], ...]
let winSec = 300, runId = null, liveRun = null, dirty = false, hoverTs = null;
let lastTs = 0;

const el = id => document.getElementById(id);
const fmt = (v, d) => v === undefined || v === null ? "--" : v.toFixed(d);

function clockLabel(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
}

/* ---------------------------------------------------------- gauges */
function polar(cx, cy, r, deg) {
  const a = (deg - 90) * Math.PI / 180;
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
}
function arcPath(cx, cy, r, a0, a1) {
  const [x0, y0] = polar(cx, cy, r, a0), [x1, y1] = polar(cx, cy, r, a1);
  return `M ${x0} ${y0} A ${r} ${r} 0 ${(a1 - a0) > 180 ? 1 : 0} 1 ${x1} ${y1}`;
}
const A0 = 215, A1 = 505;
function drawGauge(svg, frac, ticks, color) {
  frac = Math.max(0, Math.min(1, frac));
  const cx = 100, cy = 98, r = 74;
  let s = `<path d="${arcPath(cx, cy, r, A0, A1)}" stroke="#263041" stroke-width="11" fill="none" stroke-linecap="round"/>`;
  if (frac > 0.001)
    s += `<path d="${arcPath(cx, cy, r, A0, A0 + (A1 - A0) * frac)}" stroke="${color}" stroke-width="11" fill="none" stroke-linecap="round"/>`;
  for (const t of ticks) {
    const a = A0 + (A1 - A0) * t.f;
    const [x0, y0] = polar(cx, cy, r - 12, a), [x1, y1] = polar(cx, cy, r - 18, a);
    s += `<line x1="${x0}" y1="${y0}" x2="${x1}" y2="${y1}" stroke="#3c485c" stroke-width="2"/>`;
    const [lx, ly] = polar(cx, cy, r - 30, a);
    s += `<text x="${lx}" y="${ly}" fill="#8b97ab" font-size="9.5" text-anchor="middle" dominant-baseline="middle">${t.t}</text>`;
  }
  svg.innerHTML = s;
}
const T = (lo, hi, n) => Array.from({length: n + 1}, (_, i) =>
  ({f: i / n, t: Math.round(lo + (hi - lo) * i / n)}));

/* ---------------------------------------------------------- panels */
function buildPanels() {
  const box = el("panels");
  box.innerHTML = "";
  if (!meta.length) { box.innerHTML = '<div class="empty">waiting for channel list...</div>'; return; }
  for (const m of meta) {
    const d = document.createElement("div");
    d.className = "panel";
    d.innerHTML =
      `<div class="phead"><span class="pname">${m.label}</span>` +
      `<span id="badge-${m.key}"></span>` +
      `<span class="pval" id="pv-${m.key}">--</span><span class="punit">${m.unit}</span></div>` +
      `<canvas id="pc-${m.key}"></canvas>` +
      `<div class="prange"><span id="plo-${m.key}"></span><span id="phi-${m.key}"></span></div>`;
    box.appendChild(d);
    const cv = d.querySelector("canvas");
    cv.addEventListener("mousemove", e => {
      const r = cv.getBoundingClientRect();
      const [t0, t1] = windowBounds();
      hoverTs = t0 + (t1 - t0) * ((e.clientX - r.left) / r.width);
      drawAll();
    });
    cv.addEventListener("mouseleave", () => { hoverTs = null; drawAll(); });
  }
}

function windowBounds() {
  let t1 = 0, t0 = Infinity;
  for (const k in series) {
    const s = series[k];
    if (!s.length) continue;
    t1 = Math.max(t1, s[s.length - 1][0]);
    t0 = Math.min(t0, s[0][0]);
  }
  if (!isFinite(t0) || !t1) return [0, 0];
  if (winSec) t0 = Math.max(t0, t1 - winSec);
  return [t0, t1];
}

function statusOf(key, v) {
  if (key === "coolant") return v > 108 ? "bad" : v > 100 ? "warn" : null;
  if (key === "oil") return v > 125 ? "bad" : v > 115 ? "warn" : null;
  if (key === "voltage") return v < 11.8 ? "bad" : v < 12.2 ? "warn" : null;
  if (key === "cattemp") return v > 700 ? "warn" : null;
  return null;
}

function drawPanel(m, t0, t1) {
  const cv = el("pc-" + m.key);
  if (!cv) return;
  const data = (series[m.key] || []).filter(p => p[0] >= t0 && p[0] <= t1);
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if (cv.width !== Math.round(w * dpr)) { cv.width = w * dpr; cv.height = h * dpr; }
  const x = cv.getContext("2d");
  x.setTransform(dpr, 0, 0, dpr, 0, 0);
  x.clearRect(0, 0, w, h);

  if (data.length < 2) {
    el("plo-" + m.key).textContent = "";
    el("phi-" + m.key).textContent = "no data in window";
    return;
  }
  let lo = Infinity, hi = -Infinity;
  for (const p of data) { lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); }
  if (hi - lo < 1e-9) { hi += 0.5; lo -= 0.5; }
  const pad = (hi - lo) * 0.12; lo -= pad; hi += pad;

  x.strokeStyle = "#1f2836"; x.lineWidth = 1;
  for (let i = 0; i <= 2; i++) {
    const y = 3 + (h - 8) * i / 2;
    x.beginPath(); x.moveTo(0, y); x.lineTo(w, y); x.stroke();
  }
  const px = t => ((t - t0) / Math.max(t1 - t0, 1e-6)) * w;
  const py = v => 3 + (h - 8) * (1 - (v - lo) / (hi - lo));

  x.beginPath();
  data.forEach((p, i) => i ? x.lineTo(px(p[0]), py(p[1])) : x.moveTo(px(p[0]), py(p[1])));
  x.strokeStyle = "#3987e5"; x.lineWidth = 2; x.lineJoin = "round"; x.stroke();

  let shown = data[data.length - 1][1];
  if (hoverTs !== null && hoverTs >= t0 && hoverTs <= t1) {
    let best = null, bd = Infinity;
    for (const p of data) { const d = Math.abs(p[0] - hoverTs); if (d < bd) { bd = d; best = p; } }
    if (best) {
      shown = best[1];
      const hx = px(best[0]);
      x.strokeStyle = "#4a5670"; x.lineWidth = 1;
      x.beginPath(); x.moveTo(hx, 0); x.lineTo(hx, h); x.stroke();
      x.beginPath(); x.arc(hx, py(best[1]), 3, 0, Math.PI * 2);
      x.fillStyle = "#3987e5"; x.fill();
      x.strokeStyle = "#141922"; x.lineWidth = 2; x.stroke();
    }
  }
  el("pv-" + m.key).textContent = fmt(shown, m.digits);
  el("plo-" + m.key).textContent = fmt(lo + pad, m.digits);
  el("phi-" + m.key).textContent = fmt(hi - pad, m.digits);

  const st = statusOf(m.key, shown);
  const b = el("badge-" + m.key);
  b.className = st ? "badge " + st : "";
  b.textContent = st === "bad" ? "critical" : st === "warn" ? "high" : "";
}

function drawAll() {
  const [t0, t1] = windowBounds();
  for (const m of meta) drawPanel(m, t0, t1);
  el("ax0").textContent = clockLabel(t0);
  el("ax1").textContent = clockLabel(t1) + (hoverTs ? "   (hover: " + clockLabel(hoverTs) + ")" : "");
  const secs = Math.round(t1 - t0);
  el("span").textContent = t1 ? `- ${secs}s window, ${Object.keys(series).length} channels` : "";
}

/* ---------------------------------------------------------- data */
async function loadMeta() {
  const j = await (await fetch("/api/meta")).json();
  meta = j.meta; metaVersion = j.meta_version;
  buildPanels();
}

async function loadRuns() {
  const runs = await (await fetch("/api/runs")).json();
  if (runs.error) return;
  liveRun = runs.length ? runs[0].id : null;
  const sel = el("run");
  sel.innerHTML = "";
  for (const r of runs) {
    const o = document.createElement("option");
    o.value = r.id;
    const when = new Date(r.started * 1000).toLocaleString();
    o.textContent = `#${r.id}  ${when}  ${r.samples.toLocaleString()} pts` +
                    (r.ended ? "" : "  (live)");
    sel.appendChild(o);
  }
  if (runId === null) runId = liveRun;
  sel.value = runId;
}

async function loadHistory() {
  el("hint").textContent = "loading history...";
  const q = new URLSearchParams({points: 900});
  if (runId !== null) q.set("run", runId);
  if (winSec) q.set("seconds", winSec);
  const j = await (await fetch("/api/history?" + q)).json();
  series = j.series || {};
  // meta may list channels the DB has never seen, and vice versa
  for (const m of meta) if (!series[m.key]) series[m.key] = [];
  el("hint").textContent = runId === liveRun
    ? "live run - appending in real time"
    : "historical run - live updates paused";
  dirty = true;
}

function appendLive(ts, values) {
  if (runId !== liveRun) return;
  for (const k in values) {
    (series[k] = series[k] || []).push([ts, values[k]]);
  }
  const cutoff = ts - (winSec ? winSec * 1.2 : 7200);
  for (const k in series) {
    const s = series[k];
    let i = 0; while (i < s.length && s[i][0] < cutoff) i++;
    if (i) s.splice(0, i);
  }
  dirty = true;
}

function renderHead(s) {
  el("status").textContent = s.status || "-";
  el("dot").className = "dot " + (s.connected ? "on" : "off");
  el("vin").textContent = s.vin || "-";
  el("ecu").textContent = s.ecu || "-";
  el("hz").textContent = (s.hz || 0).toFixed(1);
  el("lat").textContent = (s.latency_ms || 0).toFixed(0);
  el("rows").textContent = (s.rows || 0).toLocaleString();

  const v = s.values || {};
  drawGauge(el("g-rpm"), (v.rpm || 0) / 5000, T(0, 5, 5), v.rpm > 4400 ? "#e66767" : "#3987e5");
  el("v-rpm").textContent = v.rpm === undefined ? "--" : Math.round(v.rpm);
  drawGauge(el("g-boost"), ((v.boost || 0) + 0.2) / 2.4,
            [{f:0,t:"-.2"},{f:.29,t:".5"},{f:.5,t:"1"},{f:.71,t:"1.5"},{f:1,t:"2.2"}],
            v.boost > 1.9 ? "#c98500" : "#199e70");
  el("v-boost").textContent = v.boost === undefined ? "--" : v.boost.toFixed(2);
  drawGauge(el("g-speed"), (v.speed || 0) / 250, T(0, 250, 5), "#9085e9");
  el("v-speed").textContent = v.speed === undefined ? "--" : Math.round(v.speed);
}

/* ---------------------------------------------------------- wiring */
const seg = el("win");
WINDOWS.forEach(([label, secs]) => {
  const b = document.createElement("button");
  b.textContent = label;
  if (secs === winSec) b.className = "on";
  b.onclick = () => {
    winSec = secs;
    [...seg.children].forEach(c => c.className = "");
    b.className = "on";
    loadHistory();
  };
  seg.appendChild(b);
});
el("run").onchange = e => { runId = parseInt(e.target.value, 10); loadHistory(); };
el("reload").onclick = () => { loadRuns(); loadHistory(); };

const es = new EventSource("/api/stream");
es.onmessage = async e => {
  const s = JSON.parse(e.data);
  if (s.meta_version !== metaVersion) { await loadMeta(); await loadHistory(); }
  renderHead(s);
  if (s.connected && s.ts !== lastTs) { lastTs = s.ts; appendLive(s.ts, s.values || {}); }
};
es.onerror = () => { el("dot").className = "dot off"; el("status").textContent = "server unreachable"; };

function tick() { if (dirty) { dirty = false; drawAll(); } requestAnimationFrame(tick); }
requestAnimationFrame(tick);
window.addEventListener("resize", () => { dirty = true; });

(async () => { await loadMeta(); await loadRuns(); await loadHistory(); })();
</script>
</body>
</html>
"""


# ------------------------------------------------------------------ query


def db_runs(path: str) -> List[Dict]:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    try:
        rows = db.execute(
            "SELECT r.id, r.started_at, r.ended_at, r.vin, r.gateway, r.ecu, "
            "       (SELECT COUNT(*) FROM samples s WHERE s.run_id = r.id) "
            "FROM runs r ORDER BY r.id DESC"
        ).fetchall()
    finally:
        db.close()

    return [
        {
            "id": r[0], "started": r[1], "ended": r[2], "vin": r[3],
            "gateway": r[4], "ecu": r[5], "samples": r[6],
        }
        for r in rows
    ]


def db_history(
    path: str,
    run_id: Optional[int],
    seconds: Optional[float],
    points: int = 600,
) -> Dict:
    """
    Bucket-average each channel down to at most `points` samples so a
    multi-hour run stays a few hundred KB instead of a few hundred MB.
    """
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    try:
        if run_id is None:
            row = db.execute("SELECT MAX(id) FROM runs").fetchone()
            run_id = row[0] if row else None

        if run_id is None:
            return {"run_id": None, "series": {}, "t0": 0, "t1": 0}

        span = db.execute(
            "SELECT MIN(ts), MAX(ts) FROM samples WHERE run_id = ?", (run_id,)
        ).fetchone()

        if not span or span[0] is None:
            return {"run_id": run_id, "series": {}, "t0": 0, "t1": 0}

        t0, t1 = span

        if seconds:
            t0 = max(t0, t1 - seconds)

        bucket = max((t1 - t0) / max(points, 1), 1e-6)

        rows = db.execute(
            "SELECT p.key, CAST((s.ts - ?) / ? AS INTEGER) AS b, "
            "       AVG(s.ts), AVG(s.value) "
            "FROM samples s JOIN params p ON p.id = s.param_id "
            "WHERE s.run_id = ? AND s.ts >= ? "
            "GROUP BY p.key, b ORDER BY p.key, b",
            (t0, bucket, run_id, t0),
        ).fetchall()
    finally:
        db.close()

    series: Dict[str, List[List[float]]] = {}

    for key, _b, ts, value in rows:
        series.setdefault(key, []).append([round(ts, 3), round(value, 3)])

    return {"run_id": run_id, "series": series, "t0": t0, "t1": t1}


# ----------------------------------------------------------------- server


def make_handler(tel: Telemetry, db_path: Optional[str]):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _headers(self, ctype: str, extra: Optional[Dict] = None):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")

            for k, v in (extra or {}).items():
                self.send_header(k, v)

        def _body(self, ctype: str, payload: bytes):
            self._headers(ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = self.path.split("?")[0]

            if path == "/":
                self._body("text/html; charset=utf-8", PAGE.encode())
                return

            if path == "/api/meta":
                with tel.lock:
                    payload = json.dumps({
                        "meta": tel.meta, "meta_version": tel.meta_version
                    }).encode()

                self._body("application/json", payload)
                return

            if path == "/api/runs":
                if not db_path:
                    self._body("application/json", b"[]")
                    return

                try:
                    payload = json.dumps(db_runs(db_path)).encode()
                except sqlite3.Error as exc:
                    payload = json.dumps({"error": str(exc)}).encode()

                self._body("application/json", payload)
                return

            if path == "/api/history":
                if not db_path:
                    self._body("application/json", b'{"series":{}}')
                    return

                q = urllib.parse.parse_qs(
                    self.path.split("?")[1] if "?" in self.path else ""
                )

                def num(name, default=None, cast=float):
                    raw = q.get(name, [None])[0]

                    if raw in (None, "", "all"):
                        return default

                    try:
                        return cast(raw)
                    except ValueError:
                        return default

                try:
                    payload = json.dumps(db_history(
                        db_path,
                        num("run", None, int),
                        num("seconds", None),
                        int(num("points", 600) or 600),
                    )).encode()
                except sqlite3.Error as exc:
                    payload = json.dumps({"error": str(exc), "series": {}}).encode()

                self._body("application/json", payload)
                return

            if path == "/api/snapshot":
                self._body("application/json", json.dumps(tel.get()).encode())
                return

            if path == "/api/stream":
                self._headers("text/event-stream", {"Connection": "close"})
                self.close_connection = True
                self.end_headers()

                seen = -1

                try:
                    while True:
                        seen, snap = tel.wait(seen, timeout=2.0)
                        msg = "data: " + json.dumps(snap) + "\n\n"
                        self.wfile.write(msg.encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

                return

            self.send_error(404)

    return Handler


# ------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="BMW F10 live telemetry server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--local-ip", default=None,
                    help="local 169.254.x.x ENET address (auto-detected)")
    ap.add_argument("--ip", default=None,
                    help="gateway IP, skips UDP discovery")
    ap.add_argument("--vin", default=None)
    ap.add_argument("--ecu", type=lambda s: int(s, 0), default=None,
                    help="ECU diagnostic address, e.g. 0x12")
    ap.add_argument("--rate", type=float, default=10.0,
                    help="target poll rate in Hz (default 10)")
    ap.add_argument("--slow-every", type=int, default=10,
                    help="read slow PIDs every Nth cycle (default 10)")
    ap.add_argument("--demo", action="store_true",
                    help="simulated data, no vehicle needed")

    ap.add_argument("--tank", type=float, default=70.0,
                    help="tank capacity in litres for fuel-remaining (default 70)")
    ap.add_argument("--mappings", default=DEFAULT_MAPPING_DIR,
                    help="directory of diagnostic mapping files "
                         "(default: mappings/ next to live.py)")
    ap.add_argument("--scan-timeout", type=float, default=0.3,
                    help="per-address probe timeout during ECU scan")
    ap.add_argument("--scan-full", action="store_true",
                    help="sweep all 256 addresses even after finding the engine")

    ap.add_argument("--db", default="telemetry.db",
                    help="SQLite time-series file (default telemetry.db)")
    ap.add_argument("--no-db", action="store_true", help="disable logging")
    ap.add_argument("--db-chunk", type=int, default=500,
                    help="rows buffered before a flush (default 500)")
    ap.add_argument("--db-flush", type=float, default=2.0,
                    help="max seconds between flushes (default 2)")
    args = ap.parse_args()

    #
    # Load the mappings before anything else: a broken mapping file is a
    # startup error with a readable message, not something to rediscover
    # once a minute inside the poll loop's reconnect cycle.
    #
    try:
        registry = load_registry(args.mappings)
    except MappingError as exc:
        print(f"[!] mapping error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"[!] cannot read mappings from {args.mappings}: {exc}",
              file=sys.stderr)
        return 2

    if not registry.requests:
        print(f"[!] no diagnostic mappings found under {args.mappings}",
              file=sys.stderr)
        return 2

    tel = Telemetry()

    rec: Optional[Recorder] = None

    if not args.no_db:
        rec = Recorder(args.db, chunk=args.db_chunk, interval=args.db_flush)
        rec.open()

    worker = threading.Thread(
        target=demo_loop if args.demo else poll_loop,
        args=(tel, args, rec, registry),
        daemon=True,
    )
    worker.start()

    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(tel, None if args.no_db else args.db),
    )
    server.daemon_threads = True

    shown = "localhost" if args.host in ("0.0.0.0", "") else args.host

    print("=" * 60)
    print("BMW F10 live telemetry")
    print("=" * 60)
    print(f"[+] dashboard: http://{shown}:{args.port}/")
    print(f"[+] logging:   {args.db if rec else 'disabled'}")
    print(
        f"[+] mappings:  {args.mappings} "
        f"({len(registry.mappings)} file(s), {len(registry.requests)} requests, "
        f"{len(registry.signals) + len(registry.derived)} channels)"
    )
    print("[+] Ctrl-C to stop", flush=True)

    def stop(signum, frame):
        #
        # shutdown() blocks until serve_forever() returns, so it cannot be
        # called from the handler's own thread.
        #
        print(f"\n[+] signal {signum}, stopping ...", flush=True)
        threading.Thread(target=server.shutdown, daemon=True).start()

    #
    # Installing these explicitly also covers the case where the parent
    # shell handed us SIGINT already set to SIG_IGN (background job with
    # job control disabled), which would otherwise make Ctrl-C a no-op.
    #
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping ...")
    finally:
        if rec is not None:
            rec.close()
            print(f"[+] wrote {rec.rows} rows to {args.db}"
                  + (f" ({rec.dropped} dropped)" if rec.dropped else ""))

    return 0


if __name__ == "__main__":
    sys.exit(main())
