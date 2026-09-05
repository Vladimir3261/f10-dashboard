#!/usr/bin/env python3
"""
live.py - real-time engine telemetry for a BMW F10 over ENET.

Observational. It speaks HSFZ (BMW's diagnostic-over-IP framing) to the
central gateway on TCP 6801, sends mapping-defined diagnostic reads to
the DDE (and EGS), and serves a live HTML dashboard over SSE.

    python3 live.py                 # discover car, serve on :8080
    python3 live.py --ip 169.254.x.x
    python3 live.py --demo          # no car needed, simulated data

Nothing state-changing is ever sent to the vehicle. Every outgoing
diagnostic frame passes the observational allowlist in
bmwdiag/protocol/safety.py - OBD services 0x01/0x09, UDS reads 0x22/
0x19/0x3E and the 0x2C define/clear subfunctions that arm the dynamic
DIDs - plus HSFZ alive-check replies. Write/control services abort
before any I/O.

Which channels exist, how their bytes decode and how often they are read
is not in this file: it comes from the versioned mapping files under
mappings/, loaded through bmwdiag.mapping. See
docs/MAPPING_ARCHITECTURE.md.
"""

import argparse
import hmac
import json
import math
import os
import queue
import re
import secrets
import signal
import socket
import sqlite3
import struct
import subprocess
import urllib.parse
import urllib.request
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from bmwdiag.identity import new_ulid
from bmwdiag.vehicle import load_profile
from bmwdiag.mapping import (
    fault_kind,
    MappingError,
    MappingExecutor,
    MappingRegistry,
    PollingPlan,
    ResolvedProfile,
    load_tree,
)
from bmwdiag.mapping.model import PollingClassDef
from bmwdiag.mapping.modes import DEFAULT_MODE_CONFIG, ModeTable, load_modes
from bmwdiag.mapping.polling import resolve_classes
from bmwdiag.mapping.registry import AllCapabilities
from bmwdiag.protocol import (
    NegativeResponse, ObservationalTransport, assert_observational,
)
from bmwdiag.obd import (
    OBD_SUPPORT_PIDS,
    ObdCapabilitySet,
    walk_supported_pids,
)
from bmwdiag.obd.capability import ENGINE_PID
from bmwdiag.variant import (
    COMPATIBLE,
    CombinedCapabilitySet,
    EcuIdentity,
    ProfileProbe,
    profile_nominations,
)


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


# ------------------------------------------------------------- the clock
#
# The Pi has no RTC. It boots with whatever `fake-hwclock` saved at the
# last shutdown, and systemd-timesyncd corrects it whenever the network
# comes back - which on this host is mid-drive, over a phone hotspot.
#
# On 2026-08-29 that correction landed 47 seconds into a recording and
# moved the clock forward 76.5 minutes. The run it corrupted contains a
# phantom 4578-second gap, claims a 5064-second duration for eight real
# minutes, and has ~18 seconds of samples stamped 76 minutes in the past.
# All of it shipped to the lake that way.
#
# That is worse than a bad value. A bad value is one wrong number; a bad
# clock silently corrupts every rate, gradient and trend derived from the
# data - which is the entire premise of the long-term model. A drive that
# looks like a 76-minute idle never happened.
#
# Three defences, because no single one is enough:
#
#   1. WAIT at startup for the clock to be trustworthy, briefly. Solves
#      the common case (booting on a known network) outright.
#   2. RECORD whether it was trustworthy, per run. A car that never sees
#      a network still has to be able to record; it just must not claim
#      its timestamps are wall-clock truth.
#   3. DETECT a step mid-run and end the run there. `time.monotonic()`
#      does not jump, so the difference between it and `time.time()` is
#      constant unless the clock is stepped. One run then never spans a
#      discontinuity - the same invariant a drive mode change preserves.
#
# Deliberately NOT done: retro-correcting already-written timestamps. The
# sync agent ships continuously, so rows are often already in ClickHouse
# by then; the lake keys on (vehicle, channel, ts, session), so a
# corrected ts inserts a duplicate rather than replacing anything.

#: systemd-timesyncd creates this once it has synchronised. Present is a
#: positive answer; absent is only "cannot tell from here".
TIMESYNC_STAMP = "/run/systemd/timesync/synchronized"

#: A wall-clock jump larger than this (seconds) is a step, not drift.
#: NTP slews small corrections gradually and only steps for large ones,
#: so anything past a couple of seconds is a discontinuity.
CLOCK_STEP_THRESHOLD = 2.0


def clock_anchor() -> float:
    """
    `time.time() - time.monotonic()`.

    Constant while the clock only drifts; jumps by exactly the step when
    the clock is corrected. Comparing this against a value captured at
    run start is how a mid-run correction is detected.
    """
    return time.time() - time.monotonic()


def clock_is_synced() -> bool:
    """
    Whether the host clock has been disciplined by NTP.

    Two probes, cheapest first. Both failing means "unknown", which is
    reported as not-synced: claiming the timestamps are good when we
    cannot tell is the failure mode this whole section exists to stop.
    """
    if os.path.exists(TIMESYNC_STAMP):
        return True

    try:
        proc = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=3.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    return proc.returncode == 0 and proc.stdout.strip() == "yes"


def wait_for_clock(timeout: float, report=print) -> bool:
    """
    Give NTP a bounded chance to land before recording starts.

    Bounded, and non-fatal on expiry: a car parked out of range of any
    network would otherwise never record at all, and a run with an
    honestly-labelled bad clock is worth more than no run.
    """
    if timeout <= 0 or clock_is_synced():
        return clock_is_synced()

    report(f"[~] waiting up to {timeout:g}s for the clock to sync "
           f"(no RTC on this host)")
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if clock_is_synced():
            report("[+] clock synced")

            return True

        time.sleep(0.5)

    report("[!] clock NOT synced - recording anyway, and the runs will "
           "say so. Timestamps may be wrong until NTP lands.")

    return False


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


def load_extra(registry: MappingRegistry, paths: Sequence[str]) -> MappingRegistry:
    """
    Add verified-but-non-production mapping trees on top of the base.

    This is how the F-series proprietary channels (the d72n47a0 dynamic
    reads, verified on the car but derived from BMW SGBD data) enter the
    runtime: only when the operator explicitly points --extra-mappings at
    them. The default `mappings/` load stays standard OBD only, so the
    repository's "no proprietary data in the production set" property is
    unchanged - opting in is a deliberate, per-run choice.

    These files carry `production: false`, so they are loaded with the
    filter off; they still only activate on an ECU that satisfies their
    capability match (see the variant probe in the poll loop).
    """
    for path in paths:
        for mapping in load_tree(path, production_only=False):
            registry.add(mapping)

    return registry


def describe_class(cls: PollingClassDef) -> str:
    """A polling class as a human-readable rate, for startup logging."""
    if cls.period < 1.0:
        return f"{cls.hz:g} Hz"

    return f"1/{cls.period:g}s"


def polling_classes(registry: MappingRegistry, args) -> Dict[str, PollingClassDef]:
    """
    Resolve polling classes from the loaded mappings.

    There is no CLI rate override any more. A rate is a property of what
    the channel measures, so it belongs in the mapping file; wanting all
    of them faster or slower for one drive is what a drive mode is for,
    and unlike a flag a mode is recorded with the data.
    """
    return resolve_classes(registry.polling_classes())


def host_boot_id() -> str:
    """
    An identifier for this boot of the host, or "".

    Evidence for trip grouping: two runs from different boots cannot
    belong to the same physical drive, and the in-car Pi is powered down
    between drives. Linux exposes one; anything else gets "" and the
    grouping falls back to the time gap alone.
    """
    try:
        with open("/proc/sys/kernel/random/boot_id") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def numeric_only(
    values: Dict[str, Any], profile: Optional[ResolvedProfile] = None
) -> Dict[str, float]:
    """
    Samples the recorder should store.

    Two things are filtered out:

    * Non-numeric values. `samples.value` is REAL, so an enum or ASCII
      channel is shown on the dashboard but not logged.
    * Channels declared `log: false` in their mapping - decoded and shown,
      deliberately not persisted. That is for a channel whose finding is
      that it never changes: `egs_da2e_b0` cost 124,485 stored rows over
      three days carrying exactly one distinct value. Reading it is free
      (it shares a response with the gear), storing it is not.

    With no profile the log flag cannot be consulted and everything numeric
    is kept, which is the safe direction: a missing profile must not silently
    drop channels.
    """
    return {
        key: value for key, value in values.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        and (profile is None or profile.is_logged(key))
    }


# ------------------------------------------------------------------- HSFZ


class HsfzError(Exception):
    pass


class HsfzNack(HsfzError):
    """Gateway refused to route to that address - nobody home."""


class HsfzNegativeResponse(HsfzError, NegativeResponse):
    """
    The ECU answered `7F <service> <NRC>`. Both an HsfzError (every
    existing handler keeps working) and a NegativeResponse (the code is
    data, not prose in a message).
    """

    def __init__(self, service: int, nrc: int):
        NegativeResponse.__init__(self, service, nrc)


class HsfzClient:
    """Minimal HSFZ client: 4-byte length, 2-byte control, payload."""

    def __init__(
        self,
        ip: str,
        local_ip: Optional[str] = None,
        src: int = TESTER_ADDR,
        dst: int = DDE_ADDR,
        timeout: float = 3.0,
        permit_session_control: bool = False,
    ):
        self.ip = ip
        self.local_ip = local_ip
        self.src = src
        self.dst = dst
        self.timeout = timeout
        #: Research-tool escape hatch, and the ONLY one: a client built
        #: with permit_session_control=True may additionally send service
        #: 0x10 (DiagnosticSessionControl). Everything else on the write/
        #: control list stays rejected even then. The production runtime
        #: and run_car.sh never set this - tools/egs.py sets it for its
        #: opt-in --session probe, and a test pins the default to False.
        self.permit_session_control = permit_session_control
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
        self._gate(bytes(data))

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

                raise HsfzNegativeResponse(body[1], nrc)

            return body

    def _gate(self, payload: bytes) -> None:
        """
        The safety gate, shared by BOTH diagnostic send paths - request()
        and the collect() broadcast - so every outgoing diagnostic frame
        in this process passes it: mapped requests, setup frames, OBD
        batches, functional discovery, the ECU scan, ident probes and
        variant probes. Called before ANY I/O, including the
        not-connected error, so an unsafe payload can never reach the
        wire and the property does not depend on call sites remembering
        to validate. See bmwdiag/protocol/safety.py.
        """
        if self.permit_session_control and payload[:1] == b"\x10":
            return

        assert_observational(payload)

    def collect(self, data: bytes, dst: int, window: float) -> List[Tuple[int, bytes]]:
        """Broadcast a payload and gather every ECU that answers."""
        self._gate(bytes(data))

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
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    vin         TEXT,
    gateway     TEXT,
    ecu         TEXT,
    ecu_addr    INTEGER,
    -- Compact fingerprint of the mapping set that decoded this run:
    -- "id@version,..." sorted. The per-file detail is in run_mappings.
    mapping_set TEXT,
    -- The drive mode in force for this run. A run has exactly ONE mode:
    -- switching mode ends the current run and starts a new one, so a
    -- dataset can never silently mix rates. Mode is a confound in every
    -- longitudinal comparison this project exists to make, and making it
    -- a property of the session is the only encoding where no query can
    -- forget it. Drives that span a switch are reassembled by time
    -- contiguity, which sessions already support.
    -- WHICH revision of the mode table this name refers to is not here:
    -- it rides in `mapping_set` as `drive-modes@<version>`, so one string
    -- identifies the entire sampling configuration of the run.
    mode        TEXT,
    -- Was the host clock NTP-disciplined when this run opened? The Pi
    -- has no RTC, so a run started before the network came back carries
    -- timestamps that are simply wrong. 1 = trustworthy, 0 = not, NULL =
    -- recorded before this was tracked. Anything time-derived - rates,
    -- gradients, trends, which is most of the point - must filter on it.
    clock_synced INTEGER,
    -- What the CAR PHYSICALLY WAS when this run was recorded: the stable
    -- VIN-free label, and a deterministic `subsystem=state,...` summary
    -- of its hardware configuration.
    --
    -- Snapshotted for the same reason mapping provenance is (see
    -- run_channels): the configuration file is mutable and describes the
    -- car TODAY. Analyse a drive recorded while the DPF was fitted, after
    -- the filter is removed and the profile updated, and a live lookup
    -- would declare that run's differential-pressure readings void - a
    -- statement about hardware that did exist at the time. The reverse is
    -- as bad after a part is restored.
    --
    -- NULL means the run predates this being tracked: unknown, and the
    -- analysis says so rather than substituting today's answer silently.
    vehicle_label    TEXT,
    vehicle_hardware TEXT,
    -- Durable identity, minted when the run is created and never
    -- derived from where the file happens to live. The lake's numeric
    -- session_id is a function of THIS, not of the database's basename:
    -- renaming a file used to change the identity of every run in it,
    -- and two drive files sharing a basename collided outright.
    -- See bmwdiag/identity.py.
    session_uid      TEXT,
    -- Which boot of the host recorded this run. Evidence for grouping
    -- runs into one physical trip: runs from different boots cannot be
    -- the same drive, and the Pi reboots between drives. "" when the
    -- host does not expose one.
    boot_id          TEXT
);

CREATE TABLE IF NOT EXISTS params (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT UNIQUE NOT NULL,
    pid         INTEGER,
    label       TEXT,
    unit        TEXT,
    -- Data version of the mapping file that owns this channel, as an
    -- integer string ("" if unknown). Stamped onto every sample in the
    -- lake so a dataset ties back to the exact mapping revision.
    mapping_ver TEXT
);

-- Which mapping files (and versions) decoded each run - the authoritative
-- "what produced this dataset" record. One row per loaded file per run.
CREATE TABLE IF NOT EXISTS run_mappings (
    run_id      INTEGER NOT NULL,
    mapping_id  TEXT NOT NULL,
    version     INTEGER NOT NULL,
    production  INTEGER NOT NULL,
    source_path TEXT
);

-- Per-run, per-channel provenance: which mapping file and which data
-- version decoded THIS channel during THIS run, plus the label and unit
-- in force at the time.
--
-- `params` is channel IDENTITY and is written once, on first sight, with
-- INSERT OR IGNORE. That makes params.mapping_ver a property of the FIRST
-- run that ever saw the channel, which stops being true the moment a
-- mapping is revised and the same database is reused: new samples would
-- keep pointing at the old version. Updating that row in place would be
-- worse - every historical sample would then claim to have been decoded
-- by a revision that did not exist when it was recorded.
--
-- So provenance is scoped to the run instead. A sample resolves its
-- version through (run_id, param_id), and because a run has exactly one
-- mapping configuration, one row per channel per run is enough - no need
-- to repeat the version on all 100k samples.
--
-- runs.mapping_set stays as the whole-run fingerprint; it answers "what
-- was loaded" but cannot answer "which file owned this one channel".
CREATE TABLE IF NOT EXISTS run_channels (
    run_id          INTEGER NOT NULL,
    param_id        INTEGER NOT NULL,
    mapping_id      TEXT,
    -- Integer string, "" when unknown. Never NULL for a row that exists:
    -- the reader falls back to params.mapping_ver only when the ROW is
    -- absent, so an empty string has to mean "this run knew, and the
    -- answer was nothing" rather than "ask somewhere else".
    mapping_version TEXT,
    label           TEXT,
    unit            TEXT,
    PRIMARY KEY (run_id, param_id)
);

CREATE TABLE IF NOT EXISTS samples (
    run_id   INTEGER NOT NULL,
    ts       REAL NOT NULL,
    param_id INTEGER NOT NULL,
    value    REAL NOT NULL,
    -- Why this value may not be a measurement: one of the lake's six
    -- labels ('ok', 'saturated', 'sentinel', 'stale', 'clipped',
    -- 'decode_fail'). Without it a sentinel the ECU returned to say
    -- "no value" is indistinguishable from a real reading, and a value
    -- the decoder rejected is indistinguishable from one never polled -
    -- both were simply an absent row.
    --
    -- The label is written for every sample INCLUDING 'ok', so NULL keeps
    -- one unambiguous meaning: recorded before quality was tracked. Same
    -- convention as runs.clock_synced, and for the same reason - a NULL
    -- that doubles as a default would make "fine" and "unknown" the same
    -- row again, which is the defect this column exists to remove.
    quality  TEXT
);

CREATE TABLE IF NOT EXISTS events (
    run_id  INTEGER,
    ts      REAL NOT NULL,
    kind    TEXT,
    message TEXT
);

-- Per-request faults, so a channel that fails is distinguishable from one
-- that is merely quiet. Without this a timing-out request looks exactly like
-- a healthy one nobody asked about: both simply have no rows.
--
-- Keyed by request_id, not channel: a request carries several signals and
-- fails as a unit. Resolving request -> channels needs the mappings, which
-- the analysis side already loads.
CREATE TABLE IF NOT EXISTS errors (
    run_id     INTEGER NOT NULL,
    ts         REAL NOT NULL,
    request_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    message    TEXT
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
        #: (run_id, param_id) pairs already given a run_channels row. Reset
        #: on every run start: the same Recorder outlives a run, and the
        #: next run must re-record provenance even for a channel it has
        #: already seen, because that is the whole point of the table.
        self.run_channel_seen: set = set()
        #: key -> (mapping_id, version, label, unit) as it was when the
        #: CURRENT run opened. Handed over in the run payload, never read
        #: from `meta_source` by the writer thread. See start_run().
        self.run_provenance: Dict[str, Tuple] = {}
        #: The vehicle profile in force, or None. Snapshotted per run.
        self.vehicle = None
        self.rows = 0
        self.dropped = 0
        self.db: Optional[sqlite3.Connection] = None
        #
        # Where label/unit/pid for a channel come from. Set once the
        # mapping registry has been resolved against the vehicle, before
        # any sample is written.
        #
        self.meta_source: Optional[ResolvedProfile] = None
        self.extra_versions: Tuple[str, ...] = ()
        self.thread = threading.Thread(target=self._writer, daemon=True)

    def set_metadata(self, profile: ResolvedProfile,
                     extra_versions: Sequence[str] = ()) -> None:
        """
        Point the params table at the resolved mapping registry.

        `extra_versions` are `id@version` entries for versioned config
        that is not a mapping file but still determines how the run was
        recorded - the drive-mode table. They join the same fingerprint,
        so one string identifies the whole sampling configuration.
        """
        self.meta_source = profile
        self.extra_versions = tuple(extra_versions)

    def set_vehicle(self, vehicle) -> None:
        """
        Point the recorder at what this car physically is.

        Snapshotted onto every run opened afterwards, so a drive keeps the
        configuration that was true when it was recorded. See
        bmwdiag/vehicle.py and docs/VEHICLE_PROFILE.md.
        """
        self.vehicle = vehicle

    # -- called from the poll thread --------------------------------

    def start_run(self, vin, gateway, ecu, ecu_addr, mode="normal",
                  clock_synced=None) -> None:
        """
        Open a run. Any run already open is closed first.

        A mode switch calls this again with the new mode, which is what
        keeps one run == one sampling configuration. `mode` is stored as
        plain text; WHICH revision of the mode table that name refers to
        is in `mapping_set`, alongside every mapping version.

        `clock_synced` records whether the host clock was NTP-disciplined
        when the run opened. None means unknown and is stored as NULL -
        never guessed.
        """
        #
        # The provenance is SNAPSHOT here, on the calling thread, and
        # travels in the payload. It used to be looked up by the writer
        # thread when it happened to pop this message - a read of mutable
        # shared state at an arbitrary later time, which is a race even
        # when the call order is right. Now the writer only writes what
        # it was handed.
        #
        manifest = (
            self.meta_source.mapping_manifest()
            if self.meta_source is not None else []
        )
        mapping_set = (
            self.meta_source.mapping_set(self.extra_versions)
            if self.meta_source is not None else ""
        )
        #
        # Per-channel provenance is snapshot here for the same reason and
        # in the same breath as the manifest above. Reading it in the
        # writer thread instead would leave a window: set_metadata() can
        # replace the profile between this run being queued and the
        # writer popping the first sample of it, and the sample would then
        # be labelled with the NEXT run's mapping version. That is the
        # very defect run_channels exists to remove, so it must not be
        # reintroduced by where the lookup happens.
        #
        channel_provenance = (
            self._provenance_snapshot()
            if self.meta_source is not None else {}
        )
        #
        # Same discipline: taken here, on the calling thread, and carried
        # in the payload. A live lookup later would answer for the car as
        # it is then, not as it was when this run opened.
        #
        #: Minted here, once, and carried with the run for the rest of
        #: its life. Never recomputed from anything about the file.
        session_uid = new_ulid()
        boot_id = host_boot_id()
        vehicle_label = getattr(self.vehicle, "label", "") or ""
        vehicle_hardware = (
            self.vehicle.fingerprint() if self.vehicle is not None else ""
        )

        if not mapping_set:
            #
            # Loud, because silence is exactly how this survived: a run
            # with no provenance looks identical to a healthy one until
            # someone tries to attribute the data months later.
            #
            print("[!] opening a run with NO mapping provenance - "
                  "set_metadata() must be called first", flush=True)

        self.q.put((
            "run",
            (time.time(), vin, gateway, ecu, ecu_addr, mode,
             None if clock_synced is None else int(bool(clock_synced)),
             mapping_set, manifest, channel_provenance,
             vehicle_label, vehicle_hardware, session_uid, boot_id),
        ))

    def _provenance_snapshot(self) -> Dict[str, Tuple]:
        """
        Freeze every channel's mapping identity as it is right now.

        Cheap: one pass over the profile's channels, tens of entries, once
        per run - not once per sample.
        """
        snapshot: Dict[str, Tuple] = {}

        for key in self.meta_source.keys():
            version = self.meta_source.channel_version(key)
            _pid, label, unit = self.meta_source.param_row(key)
            snapshot[key] = (
                self.meta_source.channel_mapping_id(key),
                "" if version is None else str(version),
                label,
                unit,
            )

        return snapshot

    def error(self, request_id: str, kind: str, message: str) -> None:
        """Record one per-request fault. Dropped silently if the queue is
        full - a fault storm must never stall the poll loop."""
        try:
            self.q.put_nowait(
                ("error", (time.time(), request_id, kind, message[:500]))
            )
        except queue.Full:
            pass

    def event(self, kind: str, message: str) -> None:
        try:
            self.q.put_nowait(("event", (time.time(), kind, message)))
        except queue.Full:
            pass

    def write(
        self,
        ts: float,
        values: Dict[str, float],
        qualities: Optional[Dict[str, str]] = None,
        stamps: Optional[Dict[str, float]] = None,
    ) -> None:
        """
        Record one cycle's values, optionally with a quality label each.

        `stamps` gives a signal its own acquisition time; anything
        missing keeps `ts`, the cycle time. Requests in one cycle are
        executed sequentially, so sharing one timestamp would erase the
        separation an alignment contract exists to measure.

        `qualities` is keyed like `values`; anything missing from it is
        recorded as 'ok'. That default is honest rather than convenient:
        a caller that does not pass labels is going through the narrow
        decode path, which drops every reading that is not usable, so
        what reaches here really is 'ok'.
        """
        try:
            self.q.put_nowait(
                ("s", (ts, dict(values), dict(qualities or {}),
                       dict(stamps or {})))
            )
        except queue.Full:
            self.dropped += 1

    def close(self) -> None:
        self.q.put(("stop", None))
        self.thread.join(timeout=5.0)

    # -- writer thread ----------------------------------------------

    def open(self) -> None:
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.commit()
        self.thread.start()

    def _migrate(self) -> None:
        """
        Add the mapping-versioning columns to pre-existing databases.
        CREATE TABLE IF NOT EXISTS never alters an existing table, so a db
        made before versioning would otherwise lack these columns. Adding
        them is idempotent - skipped when already present.
        """
        def cols(table: str) -> set:
            return {
                r[1] for r in self.db.execute(f"PRAGMA table_info({table})")
            }

        if "mapping_set" not in cols("runs"):
            self.db.execute("ALTER TABLE runs ADD COLUMN mapping_set TEXT")

        if "mapping_ver" not in cols("params"):
            self.db.execute("ALTER TABLE params ADD COLUMN mapping_ver TEXT")

        if "mode" not in cols("runs"):
            self.db.execute("ALTER TABLE runs ADD COLUMN mode TEXT")

        if "clock_synced" not in cols("runs"):
            self.db.execute(
                "ALTER TABLE runs ADD COLUMN clock_synced INTEGER"
            )

        if "quality" not in cols("samples"):
            self.db.execute("ALTER TABLE samples ADD COLUMN quality TEXT")

        if "vehicle_label" not in cols("runs"):
            self.db.execute("ALTER TABLE runs ADD COLUMN vehicle_label TEXT")

        if "vehicle_hardware" not in cols("runs"):
            self.db.execute(
                "ALTER TABLE runs ADD COLUMN vehicle_hardware TEXT"
            )

        if "session_uid" not in cols("runs"):
            self.db.execute("ALTER TABLE runs ADD COLUMN session_uid TEXT")

        if "boot_id" not in cols("runs"):
            self.db.execute("ALTER TABLE runs ADD COLUMN boot_id TEXT")

        #
        # Deliberately NOT back-filled. A ULID minted now would claim a
        # creation time that is not the run's, and the numeric identity
        # of already-synced sessions is derived from the filename - giving
        # them a uid would change that derivation and duplicate every one
        # of them in the lake. Old runs keep the identity they were
        # written with; see bmwdiag/identity.py.
        #

        #
        # `run_channels` needs no ALTER: it is a new table, and the
        # CREATE TABLE IF NOT EXISTS in SCHEMA runs on every open, so an
        # older database gains it simply by being opened. Idempotent.
        #
        # It is deliberately NOT back-filled for runs that predate it.
        # The only version those rows could be given is today's, which is
        # exactly the retroactive relabelling this table exists to
        # prevent. They keep resolving through params.mapping_ver - the
        # best available answer for them, though not a guaranteed one if
        # that database had already crossed a revision.
        #
        self.db.commit()

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
            version = self.meta_source.channel_version(key)
        else:
            pid, label, unit = None, key, ""
            version = None

        mapping_ver = "" if version is None else str(version)
        row = (key, pid, label, unit, mapping_ver)

        self.db.execute(
            "INSERT OR IGNORE INTO params(key, pid, label, unit, mapping_ver) "
            "VALUES (?,?,?,?,?)",
            row,
        )

        ident = self.db.execute(
            "SELECT id FROM params WHERE key = ?", (key,)
        ).fetchone()[0]

        self.param_ids[key] = ident

        return ident

    def _note_run_channel(self, param_id: int, key: str) -> None:
        """
        Record which mapping decoded `key` during the current run.

        Written once per channel per run, on the channel's first sample.

        Read STRICTLY from the snapshot taken when this run opened, never
        from `self.meta_source`. The profile is shared mutable state owned
        by another thread: set_metadata() can replace it between a run
        being queued and this writer popping the run's first sample, and
        reading it here would label that sample with the next run's
        mapping version - the exact defect this table exists to remove.

        Derived channels come through here on exactly the same path: they
        are written with `write()` like any other value, and the snapshot
        covers every channel the profile knows, derived ones included.

        A channel absent from the snapshot records unknown rather than
        borrowing an answer from somewhere else. That happens when a run
        opened with no profile at all, and "" is the honest reply.
        """
        if self.run_id is None:
            return

        seen = (self.run_id, param_id)

        if seen in self.run_channel_seen:
            return

        mapping_id, version, label, unit = self.run_provenance.get(
            key, (None, "", key, "")
        )

        #
        # INSERT OR IGNORE, not REPLACE: within one run the first write
        # wins and nothing may overwrite it. A run has exactly one
        # sampling and mapping configuration, so a second answer here
        # would mean something is wrong, and silently taking the newer
        # one would hide it.
        #
        self.db.execute(
            "INSERT OR IGNORE INTO run_channels"
            "(run_id, param_id, mapping_id, mapping_version, label, unit) "
            "VALUES (?,?,?,?,?,?)",
            (self.run_id, param_id, mapping_id, version, label, unit),
        )
        self.run_channel_seen.add(seen)

    def _flush(self, pending: List[Tuple]) -> None:
        if not pending or self.db is None:
            return

        self.db.executemany(
            "INSERT INTO samples(run_id, ts, param_id, value, quality) "
            "VALUES (?,?,?,?,?)",
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

                #: Close the previous run before opening the next, so a
                #: mode switch leaves a properly bounded session rather
                #: than one that appears to run until the process exits.
                if self.run_id is not None:
                    self.db.execute(
                        "UPDATE runs SET ended_at = ? WHERE id = ?",
                        (payload[0], self.run_id),
                    )

                (started, vin, gateway, ecu, ecu_addr, mode,
                 clock_synced, mapping_set, manifest,
                 channel_provenance, vehicle_label,
                 vehicle_hardware, session_uid, boot_id) = payload

                cur = self.db.execute(
                    "INSERT INTO runs"
                    "(started_at, vin, gateway, ecu, ecu_addr, mapping_set,"
                    " mode, clock_synced, vehicle_label, vehicle_hardware,"
                    " session_uid, boot_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (started, vin, gateway, ecu, ecu_addr, mapping_set,
                     mode, clock_synced, vehicle_label, vehicle_hardware,
                     session_uid, boot_id),
                )
                self.run_id = cur.lastrowid
                self.run_channel_seen.clear()
                self.run_provenance = channel_provenance

                if manifest:
                    self.db.executemany(
                        "INSERT INTO run_mappings"
                        "(run_id, mapping_id, version, production, source_path) "
                        "VALUES (?,?,?,?,?)",
                        [
                            (self.run_id, m["id"], m["version"],
                             1 if m["production"] else 0, m["source_path"])
                            for m in manifest
                        ],
                    )

                self.db.commit()

            elif kind == "error" and self.run_id is not None:
                ts, request_id, err_kind, message = payload
                self.db.execute(
                    "INSERT INTO errors(run_id, ts, request_id, kind, message) "
                    "VALUES (?,?,?,?,?)",
                    (self.run_id, ts, request_id, err_kind, message),
                )
                self.db.commit()

            elif kind == "event" and self.run_id is not None:
                ts, ekind, msg = payload
                self.db.execute(
                    "INSERT INTO events(run_id, ts, kind, message) VALUES (?,?,?,?)",
                    (self.run_id, ts, ekind, msg),
                )
                self.db.commit()

            elif kind == "s" and self.run_id is not None:
                ts, values, qualities, stamps = payload

                for key, value in values.items():
                    param_id = self._param_id(key)
                    self._note_run_channel(param_id, key)
                    pending.append((
                        #
                        # The signal's own acquisition time where the
                        # executor recorded one; the cycle time otherwise
                        # (derived channels, and any caller that passes
                        # no stamps). Without this two sequential reads
                        # in one cycle are indistinguishable in storage.
                        #
                        self.run_id, stamps.get(key, ts), param_id, value,
                        qualities.get(key, "ok"),
                    ))

            if len(pending) >= self.chunk or (
                pending and time.monotonic() - last >= self.interval
            ):
                self._flush(pending)
                last = time.monotonic()


# ------------------------------------------------------------------ state


class ModeControl:
    """
    The requested drive mode, handed from the HTTP thread to the poll loop.

    The HTTP thread only ever *requests* a mode; the poll loop applies it
    between cycles and nothing else touches the plan. That keeps the
    scheduler single-threaded - no lock around `due()`, no chance of a
    mode changing halfway through building a cycle's request list.

    A request survives a reconnect: `current` is what the loop last
    applied, so a plan rebuilt after a dropped link comes back in the
    mode the operator chose rather than the one the process started in.

    Holds the loaded `ModeTable` so the mode NAME and the table VERSION
    travel together - a session records both, and a name on its own does
    not identify a rate.
    """

    def __init__(self, table: ModeTable, name: Optional[str] = None):
        self._lock = threading.Lock()
        self.table = table
        self.current = name or table.default
        self._pending: Optional[str] = None

        #: Fail at construction, not on the first switch.
        table.get(self.current)

    @property
    def version(self) -> int:
        return self.table.version

    def mode(self, name: Optional[str] = None):
        return self.table.get(name if name is not None else self.current)

    def request(self, name: str) -> None:
        """Validates eagerly, so a bad name is a 400 and never reaches the loop."""
        self.table.get(name)

        with self._lock:
            self._pending = name

    def take(self) -> Optional[str]:
        """The pending change, if any, clearing it. Called only by the loop."""
        with self._lock:
            pending, self._pending = self._pending, None

        return pending


class Diagnostics:
    """
    The full car-communication picture for this session, for the HTTP
    thread to read while the poll loop owns the objects.

    This exists because the interesting question when a channel is
    missing - "did we not ask, did the ECU not answer, or did resolution
    drop it?" - had no answer anywhere. The sample table cannot tell a
    request nobody made from one that always fails, and resolution
    filters silently by design.

    The poll loop publishes REFERENCES once per connection; the report is
    built on demand, so a page nobody opens costs nothing per cycle.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state: Dict[str, Any] = {}

    def publish(self, **kwargs: Any) -> None:
        with self._lock:
            self._state.update(kwargs)

    def clear(self, status: str = "") -> None:
        """
        Drop the car-dependent picture, keep what was loaded from disk.

        A stale session reads as live and is worse than none. But the
        mapping set is a property of how the process was started, not of
        the link, so wiping it too would leave the panel unable to say
        what it would poll once the car came back.
        """
        with self._lock:
            keep = {
                k: v for k, v in self._state.items()
                if k in ("registry", "extra_ids")
            }
            keep["status"] = status
            self._state = keep

    def _get(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def loaded(self) -> Dict[str, Any]:
        """
        What is known WITHOUT the car: which mapping files loaded, their
        versions, which arrived via --extra-mappings, and the rates they
        declare.

        All of it is settled at boot, before any socket exists. Reporting
        nothing until the link comes up made the panel unable to answer
        "did my extra mappings actually load?" - which is precisely the
        question you have in the driveway, with the car off, before you
        find out on the motorway that you recorded 24 channels instead
        of 47.
        """
        state = self._get()
        registry = state.get("registry")

        if registry is None:
            return {"mappings": [], "classes": [], "channels": 0}

        extra_ids = set(state.get("extra_ids") or ())
        classes = {c.name: c for c in registry.polling_classes()}
        members: Dict[str, int] = {}

        for request in registry.requests:
            members[request.polling_class] = members.get(
                request.polling_class, 0
            ) + 1

        return {
            "mappings": [{
                "id": m.id,
                "version": m.version,
                "production": m.production,
                "path": m.source_path,
                "source_type": m.provenance.type,
                "verification": m.verification.status,
                "ecu_family": m.ecu.family,
                "ecu_target": m.ecu.target.describe(),
                "extra": m.id in extra_ids,
                "requests": len(m.requests),
                "signals": len(m.signals) + len(m.derived),
            } for m in registry.mappings],
            "classes": [{
                "name": name,
                #: The honest per-channel interval: a staggered class
                #: fires one member per firing, so a member's own
                #: refresh is period x members.
                "period_s": cls.period * (
                    members.get(name, 1) if cls.stagger else 1
                ),
                "requests": members.get(name, 0),
                "stagger": cls.stagger,
            } for name, cls in sorted(
                classes.items(), key=lambda kv: kv[1].priority
            ) if members.get(name)],
            "channels": len(registry.signals) + len(registry.derived),
        }

    def report(self) -> Dict[str, Any]:
        state = self._get()
        profile = state.get("profile")

        if profile is None:
            #
            # No link yet. Everything that does not depend on the car is
            # still worth showing - the alternative is a blank page that
            # cannot distinguish "not connected" from "nothing loaded".
            #
            return {
                "ready": False,
                "detail": state.get("status") or "not connected to the car yet",
                "loaded": self.loaded(),
            }

        executor = state.get("executor")
        plan = state.get("plan")
        stats = executor.stats() if executor is not None else {}
        #: Signal-level, deliberately fetched separately from stats():
        #: "the exchange worked" and "a usable value came back" are
        #: different questions, and a channel can be perfect on the first
        #: while answering nothing but sentinels on the second.
        quality = executor.quality_stats() if executor is not None else {}
        extra_ids = set(state.get("extra_ids") or ())
        now = time.time()

        #: request id -> the mapping file that declares it
        owner: Dict[str, Any] = {}

        for mapping in profile.mappings:
            for request in mapping.requests:
                owner[request.id] = mapping

        classes = plan.classes if plan is not None else {}
        counts: Dict[str, int] = {}

        for request in profile.requests:
            counts[request.polling_class] = counts.get(
                request.polling_class, 0
            ) + 1

        requests = []
        totals = {"sent": 0, "ok": 0, "failed": 0}

        for request in profile.requests:
            st = stats.get(request.id, {})
            mapping = owner.get(request.id)
            cls = classes.get(request.polling_class)
            sent = st.get("sent", 0)
            ok = st.get("ok", 0)

            for key in totals:
                totals[key] += st.get(key, 0)

            #
            # A staggered class fires one member per firing, so its
            # period is the gap between firings of the CLASS - a member's
            # own interval is period x members. Reporting the raw period
            # would overstate these ~22x.
            #
            period = None

            if cls is not None:
                members = counts.get(request.polling_class, 1)
                period = cls.period * (members if cls.stagger else 1)

            requests.append({
                "id": request.id,
                "mapping": mapping.id if mapping else "",
                "protocol": request.protocol,
                "target": request.target.describe(),
                "address": (
                    f"0x{request.target.resolve(profile.targets):02X}"
                    if request.target.resolve(profile.targets) is not None
                    else request.target.describe()
                ),
                "pid": None if request.pid is None else f"0x{request.pid:02X}",
                "did": None if request.did is None else f"0x{request.did:04X}",
                "setup_frames": len(request.setup or ()),
                "class": request.polling_class,
                "period_s": period,
                "signals": [sig.key for sig in request.signals],
                "sent": sent,
                "ok": ok,
                "failed": st.get("failed", 0),
                "kinds": st.get("kinds", {}),
                #: From the resting mechanism (cfbabd4): seconds this
                #: request is standing down after repeated faults, and
                #: the consecutive-fault count that caused it. A snapshot
                #: at report time - the page must render it as a state
                #: ("resting ~5s"), never as a live countdown, or a
                #: cached value reads as a hung page.
                "resting_for": st.get("resting_for", 0.0),
                "consecutive_faults": st.get("consecutive_faults", 0),
                #: None, not 0, until something has actually been asked -
                #: "0% success" on an unpolled request would read as a
                #: failure rather than as no data yet.
                #
                # Never rounded UP to 100 while a failure exists.
                # 6963/6964 rounds to 100.0, and "100%" printed beside
                # "failed: 1" is exactly what makes someone stop
                # trusting a panel whose only job is telling them what
                # is broken. Floored instead, so the number can be
                # optimistic by a tenth but never claim perfection the
                # data does not support.
                #
                "success_pct": (
                    None if not sent
                    else 100.0 if ok == sent
                    else math.floor(1000.0 * ok / sent) / 10.0
                ),
                "last_ok_age": (
                    None if not st.get("last_ok")
                    else round(now - st["last_ok"], 1)
                ),
                "last_error": st.get("last_error"),
                "last_error_age": (
                    None if not st.get("last_error_at")
                    else round(now - st["last_error_at"], 1)
                ),
            })

        values = state.get("values") or {}
        channels = []

        for meta in profile.meta():
            key = meta["key"]
            #: `signal()` is the read channels; anything it does not
            #: know is derived. A hasattr guard here silently reported
            #: EVERY channel as derived, including rpm.
            signal = profile.signal(key)
            request_id = signal.request_id if signal is not None else ""

            counts = quality.get(key, {})
            flagged = sum(n for q, n in counts.items() if q != "ok")

            channels.append({
                "key": key,
                "label": meta.get("label", ""),
                "unit": meta.get("unit", ""),
                "request": request_id,
                "derived": not request_id,
                "logged": profile.is_logged(key),
                "version": profile.channel_version(key),
                "value": values.get(key),
                #: How this channel's readings came out, by quality label.
                #: Empty until it has decoded at least once, which is
                #: distinct from decoding only unusable values.
                "quality": counts,
                "flagged": flagged,
                #: None until something decoded - "0% flagged" on a
                #: channel that never answered would read as healthy.
                "flagged_pct": (
                    None if not counts
                    else round(100.0 * flagged / sum(counts.values()), 1)
                ),
            })

        mappings = []

        for mapping in profile.mappings:
            mappings.append({
                "id": mapping.id,
                "version": mapping.version,
                "production": mapping.production,
                "path": mapping.source_path,
                "source_type": mapping.provenance.type,
                "verification": mapping.verification.status,
                "ecu_family": mapping.ecu.family,
                "ecu_target": mapping.ecu.target.describe(),
                #: Loaded only because --extra-mappings named it. This is
                #: the repo's "no proprietary data in the production set"
                #: line, made visible per run.
                "extra": mapping.id in extra_ids,
                "requests": len([
                    r for r in profile.requests if owner.get(r.id) is mapping
                ]),
            })

        return {
            "ready": True,
            "loaded": self.loaded(),
            "session": {
                "ecu": state.get("ecu"),
                "ecu_addr": (
                    None if state.get("ecu_addr") is None
                    else f"0x{state['ecu_addr']:02X}"
                ),
                "gateway": state.get("gateway"),
                "other_ecus": state.get("other_ecus") or [],
                #: Compatibility and identity, separately: which profiles
                #: the ECU answered (and how each probe went) beside what
                #: is actually known about which SGBD it is - which, with
                #: no identity evidence, is "unknown", and the view says
                #: exactly that rather than promoting a probe to an ident.
                "identity": (
                    state["identity"].as_dict()
                    if state.get("identity") is not None
                    else EcuIdentity().as_dict()
                ),
                "supported_pids": state.get("supported_pids"),
                "mapping_set": profile.mapping_set(
                    state.get("extra_versions") or ()
                ),
                "mode": state.get("mode"),
                "connected_at": state.get("connected_at"),
                "uptime_s": (
                    None if not state.get("connected_at")
                    else round(now - state["connected_at"], 1)
                ),
            },
            "mappings": mappings,
            "dropped": [d.as_dict() for d in profile.report.dropped],
            "requests": requests,
            "channels": channels,
            "totals": {
                **totals,
                "requests": len(profile.requests),
                "channels": len(channels),
                "success_pct": (
                    None if not totals["sent"]
                    else round(100.0 * totals["ok"] / totals["sent"], 1)
                ),
            },
        }


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
            #: Drive mode in force, and whether a duty-cycled mode is
            #: currently in its awake or asleep window.
            "mode": "normal",
            "duty": "continuous",
            #: Whether the host clock is NTP-disciplined. Surfaced so a
            #: drive recorded on a bad clock is visible while it is
            #: happening, not only in the database afterwards.
            "clock_synced": None,
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
    modes: Optional[ModeControl] = None,
    diag: Optional[Diagnostics] = None,
) -> None:
    diag = diag if diag is not None else Diagnostics()
    #: The caller normally supplies this; constructing one here keeps
    #: the loop runnable on its own (tests, ad-hoc use).
    modes = modes if modes is not None else ModeControl(
        load_modes(getattr(args, "modes", DEFAULT_MODE_CONFIG)),
        getattr(args, "mode", None),
    )
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

                #
                # Re-detect the link-local interface each attempt: the
                # ENET cable may be plugged in after the process starts
                # (e.g. logging armed before the car is connected), and
                # the 169.254.x.x address only appears once it is.
                #
                if not local_ip:
                    local_ip = args.local_ip or find_link_local_ip()

                if not local_ip:
                    raise HsfzError(
                        "no 169.254.x.x interface found yet - waiting for "
                        "the ENET cable (or pass --local-ip)"
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

            #: Re-probed per connect, not cached from startup: on this
            #: host the network usually arrives well after boot, so a run
            #: opened later may be trustworthy when the first was not.
            synced = clock_is_synced()
            anchor = clock_anchor()

            #
            # The run is NOT opened here. It cannot be: the mapping
            # provenance it must record does not exist until the profile
            # is resolved, forty lines below. Opening it here recorded
            # every first run of every drive with an empty `mapping_set`
            # and no `run_mappings` rows - the exact link
            # docs/DATA_VERSIONING.md relies on to tie a dataset to the
            # revision that produced it.
            #
            # It went unnoticed for weeks because fragmentation masked
            # it: while a drive split into 4-6 runs, only the first lost
            # its provenance and the rest carried it. Fixing the
            # fragmentation made every drive one run, and the loss went
            # from ~18% to 100%.
            #

            #
            # Establish what the ECU can DO by probe, never by assumption:
            # replay the reads each profile-gated mapping nominates and
            # keep the profiles the ECU actually answers, with the reason
            # for every one it does not. On a base (OBD-only) load this is
            # a no-op; with --extra-mappings it is what lets the F-series
            # dynamic channels activate on an ECU that accepts the F303
            # sequence and stay dormant on anything else.
            #
            # What the ECU IS is a separate question with a separate
            # answer. No identity evidence is read here - the ident DIDs
            # this DDE refuses are documented in
            # research/reports/n47-oncar-results.md - so `exact_sgbd`
            # stays `unknown`, and the diagnostics view says so next to
            # the profiles it did prove.
            #
            nominations = profile_nominations(registry.mappings)
            identity = EcuIdentity()

            if nominations:
                identity = EcuIdentity(ProfileProbe(
                    lambda p, dst, timeout=None: client.request(p, timeout, dst),
                    timeout=1.0,
                ).resolve(nominations, engine.addr))

                for resolution in identity.profiles.values():
                    print(
                        f"[{'+' if resolution.outcome == COMPATIBLE else '-'}] "
                        f"profile {resolution.profile}: {resolution.describe()}"
                    )

                print(f"[ ] exact SGBD: {identity.identity.describe()}")

            capabilities = CombinedCapabilitySet(engine.capabilities(), identity)

            #
            # Resolve the mapping registry against this particular ECU.
            # `discovered_engine` is a late-bound target: mapping files
            # never name an address, the scan does.
            #
            profile = registry.resolve(
                capabilities,
                config={"tank": args.tank},
                targets={"discovered_engine": engine.addr},
            )

            if not profile.requests:
                raise HsfzError("ECU reports no usable PIDs")

            #
            # Metadata first, THEN the run: `start_run` snapshots the
            # provenance at call time, so a run opened before the profile
            # exists would record none.
            #
            if rec is not None:
                rec.set_metadata(profile, [modes.table.fingerprint()])
                #: what the car physically is, snapshotted onto every run
                #: that follows, so a drive keeps the configuration that
                #: was true when it was recorded rather than whatever the
                #: profile file happens to say months later
                rec.set_vehicle(load_profile())
                rec.start_run(vin, ip, engine.label(), engine.addr,
                              modes.current, synced)
                rec.event("connect", f"engine ECU {engine.label()}")

                if not synced:
                    rec.event("clock", "run opened with an unsynced clock")

            session = ObdSession(client, profile.obd_pid_lengths())
            plan = PollingPlan(
                profile.requests,
                polling_classes(registry, args),
                modes.mode(),
            )
            #
            # Record every per-request fault. Until this was wired the
            # executor's on_error hook went nowhere, so a request that timed
            # out or was refused left no trace at all - indistinguishable
            # from a channel nobody asked about, since both simply have no
            # rows. `fault_kind` gives a stable name to group by rather than
            # an exception message to parse.
            #
            def note_fault(request_id: str, exc: Exception) -> None:
                if rec is not None:
                    rec.error(request_id, fault_kind(exc), str(exc))

            executor = MappingExecutor(
                profile,
                #: Defence in depth: HsfzClient.request is the real choke
                #: point today, but the executor seam is where a FUTURE
                #: transport plugs in, and wrapping here means that
                #: transport inherits the policy without anyone
                #: remembering to add it.
                transport=ObservationalTransport(HsfzTransport(client)),
                obd_reader=session,
                on_error=note_fault,
            )

            tel.set_meta(profile.meta())

            #
            # Everything the diagnostics view needs, published once per
            # connection. References, not copies: the report is built on
            # demand so a page nobody opens costs nothing per cycle.
            #
            diag.publish(
                profile=profile, executor=executor, plan=plan,
                ecu=engine.label(), ecu_addr=engine.addr, gateway=ip,
                other_ecus=[e.label() for e in ecus if e.addr != engine.addr],
                identity=identity,
                supported_pids=len(engine.supported or ()),
                extra_versions=[modes.table.fingerprint()],
                mode=modes.current, connected_at=time.time(),
            )

            counts = plan.counts()

            print(
                "[+] polling "
                + ", ".join(
                    f"{n}x {name} @ {describe_class(plan.classes[name])}"
                    for name, n in sorted(
                        counts.items(),
                        key=lambda kv: plan.classes[kv[0]].priority,
                    )
                )
                + f" [mode: {modes.current}]"
            )
            print(f"[+] channels:  {', '.join(profile.signal_keys())}")

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
                # Apply a mode switch between cycles, never during one.
                # The new mode starts a NEW run: one run has exactly one
                # sampling configuration, so no analysis can mix rates
                # without noticing. The drive is still reassembled from
                # consecutive sessions when that is what you want.
                #
                wanted = modes.take()

                if wanted is not None and wanted != modes.current:
                    plan.set_mode(modes.mode(wanted))
                    modes.current = wanted
                    print(f"[+] drive mode -> {wanted}", flush=True)

                    if rec is not None:
                        synced = clock_is_synced()
                        anchor = clock_anchor()
                        rec.start_run(vin, ip, engine.label(), engine.addr,
                                      wanted, synced)
                        rec.event("mode", f"drive mode -> {wanted}")

                #
                # Did the wall clock step? `time.monotonic()` cannot, so
                # a change in the difference between them is a clock
                # correction - the 76-minute jump that corrupted drive 8.
                # End the run there: one run then never spans a timeline
                # discontinuity, and the segment recorded against the bad
                # clock stays identifiable instead of being silently
                # stitched to good data.
                #
                now_anchor = clock_anchor()
                step = now_anchor - anchor

                if abs(step) > CLOCK_STEP_THRESHOLD:
                    synced = clock_is_synced()
                    anchor = now_anchor
                    print(f"[!] host clock stepped {step:+.1f}s - starting a "
                          f"new run (synced={synced})", flush=True)

                    if rec is not None:
                        rec.event(
                            "clock",
                            f"clock stepped {step:+.1f}s; previous run's "
                            f"timestamps are not comparable with this one",
                        )
                        rec.start_run(vin, ip, engine.label(), engine.addr,
                                      modes.current, synced)

                #
                # The plan schedules requests, not channel names, so two
                # signals decoded from one reply cost one exchange.
                #
                readings, stamps = executor.execute_readings_at(
                    plan.due(cycle, started)
                )
                fresh = {
                    key: r.value for key, r in readings.items() if r.usable
                }
                flagged = {
                    key: r for key, r in readings.items() if not r.usable
                }
                values.update(fresh)

                #
                # A channel that answered with something unusable this
                # cycle must not leave its last good value standing. The
                # carried-forward view feeds both the dashboard and the
                # derived channels, and showing a MAP from thirty seconds
                # ago as if it were current - or computing boost from it -
                # is worse than showing nothing. The reading itself is
                # still recorded below, with the label saying what
                # happened.
                #
                for key in flagged:
                    values.pop(key, None)

                #
                # ...and neither must anything COMPUTED from it. Popping
                # the input alone left boost frozen at its last healthy
                # value for as long as MAP stayed railed - the sustained
                # full-throttle window, on the hero gauge - which is the
                # same silently-stale failure one layer up. A derived
                # channel whose required input is flagged goes too;
                # fallback-satisfied inputs do not count, so boost
                # survives a flagged baro (it declares a fallback for
                # it) but not a flagged map. Iterated, so a derived
                # channel feeding another cascades through.
                #
                if flagged:
                    dropped_keys = set(flagged)

                    while True:
                        grew = False

                        for definition in profile.derived:
                            if definition.key in dropped_keys:
                                continue

                            fallbacks = definition.fallback_map()
                            needed = [
                                name for role, name in definition.inputs
                                if role not in fallbacks
                            ]

                            if any(name in dropped_keys for name in needed):
                                values.pop(definition.key, None)
                                dropped_keys.add(definition.key)
                                grew = True

                        if not grew:
                            break

                derived = profile.apply_derived(values, fresh)
                values.update(derived)
                fresh.update(derived)

                if rec is not None and (fresh or flagged):
                    #
                    # Flagged readings are stored, not shown. That is the
                    # whole change: "the ECU said no-value" becomes a row
                    # instead of an absence, while the display keeps
                    # suppressing exactly what it suppressed before.
                    #
                    # A derived channel carries no label because it is
                    # only computed from usable inputs - when an input is
                    # flagged it is gone from `values` above, so the
                    # derived value is not produced at all rather than
                    # produced from a poisoned one.
                    #
                    stored = dict(fresh)
                    stored.update(
                        {key: r.value for key, r in flagged.items()}
                    )
                    rec.write(
                        time.time(),
                        numeric_only(stored, profile),
                        {key: r.quality for key, r in readings.items()},
                        #
                        # Per-signal acquisition times. Requests in a
                        # cycle go out sequentially, so a paired
                        # actual/setpoint stamped with one cycle time
                        # would report a gap of exactly zero however far
                        # apart the two exchanges were - the alignment
                        # contract would then be grading the recorder.
                        # Derived channels have no exchange of their own
                        # and keep the cycle timestamp.
                        #
                        stamps,
                    )

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
                    mode=modes.current,
                    duty=plan.duty_state(started),
                    clock_synced=synced,
                )

                diag.publish(values=values, mode=modes.current)

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
            #: A disconnected picture is worse than none - the counters
            #: would keep reading as if the link were live.
            diag.clear(msg)

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
    modes: Optional[ModeControl] = None,
    diag: Optional[Diagnostics] = None,
) -> None:
    #: The demo synthesises values rather than scheduling requests, so a
    #: mode has nothing to scale here - it is accepted and reported so the
    #: dashboard control behaves the same way without a car attached.
    modes = modes if modes is not None else ModeControl(
        load_modes(getattr(args, "modes", DEFAULT_MODE_CONFIG)),
        getattr(args, "mode", None),
    )
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
        rec.set_metadata(profile, [modes.table.fingerprint()])
        #: see the note at the other call site - configuration is
        #: snapshotted per run, not looked up at analysis time
        rec.set_vehicle(load_profile())

    tel.update(
        connected=True, status="live (demo)", ecu="demo",
        gateway="127.0.0.1", vin=DEMO_VIN,
    )

    if rec is not None:
        rec.start_run(DEMO_VIN, "127.0.0.1", "demo", 0x12, modes.current,
                      clock_is_synced())

    t0 = time.monotonic()

    while True:
        wanted = modes.take()

        if wanted is not None and wanted != modes.current:
            modes.current = wanted

            if rec is not None:
                rec.start_run(DEMO_VIN, "127.0.0.1", "demo", 0x12, wanted,
                              clock_is_synced())
                rec.event("mode", f"drive mode -> {wanted}")

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
            #
            # Synthetic proprietary/transmission channels so the demo can
            # exercise the M-Performance drive view. They only surface if
            # the matching --extra-mappings are also loaded.
            #
            "gear": max(1, min(8, int(drive * 130 / 22) + 1)),
            "n47d_gbx_oil_temp": round(min(92, 40 + t * 1.2), 1),
            "n47d_turbine_speed": round(rpm * (0.85 + drive * 0.15)),
            "n47d_converter_temp": round(min(95, 42 + t * 1.3), 1),
            "n47d_boost_act": round(1000 + boost * 100 * 10, 0),
            "n47d_rail_act": round(300 + drive * 1200),
            "n47d_coolant": round(min(89, 20 + t * 2), 1),
            "n47d_oil_temp": round(min(96, 18 + t * 1.7), 1),
            "n47d_maf_per_cyl": round(240 + drive * 900, 1),
            "n47d_dpf_dp": round(drive * 45, 1),
            "distance": round(1000 + t * 0.02, 1),
        }

        if rec is not None:
            rec.write(time.time(), numeric_only(values, profile))

        tel.update(
            values=values, latency_ms=round(6 + drive * 4, 1), hz=10.0,
            rows=rec.rows if rec else 0,
            dropped=rec.dropped if rec else 0,
            mode=modes.current,
            clock_synced=clock_is_synced(),
        )

        time.sleep(1.0 / args.rate)


# ------------------------------------------------------------------- page


PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>F10 520d - live telemetry</title>
<style>
  :root {
    --bg: #0b0e13; --surface: #141922; --surface2: #1b2230;
    --line: #263041; --grid: #1f2836; --text: #e6edf7; --muted: #8b97ab;
    --series: #3987e5; --good: #199e70; --warn: #c98500; --bad: #e66767;
    --accent: #9085e9;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px; background: var(--bg); color: var(--text);
    font: 13px/1.45 ui-sans-serif, -apple-system, "SF Pro Text", Inter, system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  header { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  h1 { font-size: 15px; margin: 0; letter-spacing: .05em; text-transform: uppercase; }
  .chips { display: flex; gap: 7px; flex-wrap: wrap; margin-left: auto; }
  .sheet { display: none; position: fixed; inset: 0; z-index: 50;
           background: rgba(0,0,0,.6); align-items: center; justify-content: center; }
  .sheet.open { display: flex; }
  .sheetbox { background: var(--panel); border: 1px solid var(--line);
              border-radius: 10px; padding: 18px; width: min(560px, 92vw);
              max-height: 86vh; overflow: auto; }
  .sheetbox h2 { margin: 0 0 8px; font-size: 15px; }
  .sheetbox p { margin: 0 0 12px; font-size: 12px; line-height: 1.5; }
  .sheetbox .row { display: flex; gap: 8px; align-items: center;
                   margin-top: 10px; flex-wrap: wrap; }
  .sheetbox label { font-size: 12px; color: var(--dim); }
  .sheetbox input, .sheetbox select, .sheetbox button {
    background: var(--bg); color: var(--text); border: 1px solid var(--line);
    border-radius: 6px; padding: 7px 10px; font: inherit; font-size: 12px; }
  .sheetbox input { flex: 1 1 240px; font-family: ui-monospace, monospace; }
  .sheetbox button { cursor: pointer; }
  .sheetbox button.danger { border-color: var(--bad); color: var(--bad); }
  .sharerow { display: flex; gap: 8px; align-items: center; font-size: 12px;
              border-top: 1px solid var(--line); padding: 8px 0; }
  .sharerow code { font-size: 11px; color: var(--dim); flex: 1;
                   overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .chip {
    background: var(--surface); border: 1px solid var(--line); border-radius: 999px;
    padding: 4px 10px; font-size: 11.5px; color: var(--muted);
    font-variant-numeric: tabular-nums; white-space: nowrap;
  }
  .chip b { color: var(--text); font-weight: 600; }
  .dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; margin-right: 6px; }
  .dot.on { background: var(--good); } .dot.off { background: var(--bad); }

  /* drive-mode picker: a native select so it works on a phone in the
     car without any custom dropdown code */
  #drivemode {
    background: none; border: 0; color: var(--text); font: inherit;
    font-weight: 600; cursor: pointer; padding: 0 0 0 4px;
    -webkit-appearance: none; appearance: none;
  }
  #drivemode:focus { outline: 1px solid var(--accent); border-radius: 4px; }
  #drivemode option { background: var(--surface2); color: var(--text); }
  #drivemodechip.pending b, #drivemodechip.pending #drivemode { color: var(--warn); }
  #drivemodechip.asleep { opacity: 0.55; }

  /* mode switch */
  .modes { display: flex; gap: 0; margin: 14px 0; border: 1px solid var(--line);
           border-radius: 10px; overflow: hidden; width: fit-content; }
  .modes button {
    background: var(--surface); color: var(--muted); font: inherit; font-size: 12px;
    border: 0; border-right: 1px solid var(--line); padding: 7px 16px; cursor: pointer;
    text-transform: uppercase; letter-spacing: .06em;
  }
  .modes button:last-child { border-right: 0; }
  .modes button.on { background: var(--series); color: #fff; }

  .controls {
    display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
    margin: 12px 0; padding-bottom: 12px; border-bottom: 1px solid var(--line);
  }
  label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
  select, button.ctl {
    background: var(--surface); color: var(--text); font: inherit; font-size: 12px;
    border: 1px solid var(--line); border-radius: 8px; padding: 5px 10px; cursor: pointer;
  }
  button.ctl.on { background: var(--series); border-color: var(--series); color: #fff; }
  .seg { display: flex; gap: 4px; }
  .hidden { display: none !important; }

  /* --- mode 1: drive --- */
  /* --- M-Performance drive cluster --- */
  :root { --m-red: #E4002B; --m-blue: #1C5EAB; --m-lblue: #2E9BD6; --m-violet: #6c4b9e; }
  .mcluster { max-width: 980px; margin: 0 auto; }
  .revbar { display: flex; gap: 3px; height: 16px; margin-bottom: 14px; }
  .revbar .led { flex: 1; border-radius: 2px; background: #171d28; transition: background .05s; }
  .herorow {
    display: grid; grid-template-columns: 1fr 0.9fr 1fr; gap: 10px; align-items: center;
  }
  .herogauge { position: relative; text-align: center; }
  .herogauge svg { width: 100%; max-width: 300px; height: auto; }
  .herogauge .heroval {
    position: absolute; left: 0; right: 0; top: 52%; transform: translateY(-50%);
    font-size: 46px; font-weight: 800; font-variant-numeric: tabular-nums;
    letter-spacing: -.02em; text-shadow: 0 0 18px rgba(46,155,214,.25);
  }
  .herogauge .herolabel {
    font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .18em;
    margin-top: -6px;
  }
  .gearbox {
    background: linear-gradient(160deg, #10151f, #0a0d13);
    border: 1px solid var(--line); border-radius: 16px; padding: 10px 6px 12px;
    text-align: center; box-shadow: inset 0 0 40px rgba(0,0,0,.5);
  }
  .gearlabel { font-size: 11px; color: var(--muted); letter-spacing: .28em; }
  .gearnum {
    font-size: 128px; font-weight: 800; line-height: .95; font-variant-numeric: tabular-nums;
    background: linear-gradient(180deg, #fff, #9fc6e6); -webkit-background-clip: text;
    background-clip: text; color: transparent; text-shadow: 0 0 30px rgba(46,155,214,.35);
  }
  .gearnum.rev { background: linear-gradient(180deg, #fff, #f2a0a0); -webkit-background-clip: text; }
  .mstripe {
    height: 6px; border-radius: 3px; margin: 4px 22px 0;
    background: linear-gradient(90deg, var(--m-lblue) 0 33%, var(--m-blue) 33% 66%, var(--m-red) 66% 100%);
  }
  .tilerow { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
             gap: 8px; margin-top: 16px; }
  .mtile {
    background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
    padding: 8px 11px; border-left: 3px solid var(--m-blue);
  }
  .mtile.warn { border-left-color: var(--warn); } .mtile.bad { border-left-color: var(--bad); }
  .mtile .tname { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .08em; }
  .mtile .tval { font-size: 21px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .mtile .tunit { font-size: 11px; color: var(--muted); margin-left: 3px; }
  @media (max-width: 640px) {
    .herorow { grid-template-columns: 1fr; }
    .gearnum { font-size: 92px; }
  }

  /* --- mode 2: detail (gauges + panels) --- */
  .gauges { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; max-width: 640px; }
  .gauge { text-align: center; }
  .gauge svg { width: 100%; max-width: 170px; height: auto; }
  .gval { font-size: 26px; font-weight: 650; font-variant-numeric: tabular-nums; }
  .gunit { font-size: 10.5px; color: var(--muted); letter-spacing: .1em; text-transform: uppercase; }
  h2 { font-size: 11px; letter-spacing: .12em; text-transform: uppercase; color: var(--muted);
       font-weight: 600; margin: 22px 0 10px; }
  .panels { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 10px; }
  .panel { background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
           padding: 10px 11px 6px; }
  .phead { display: flex; align-items: baseline; gap: 6px; }
  .pname { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }
  .pval { margin-left: auto; font-size: 17px; font-weight: 650; font-variant-numeric: tabular-nums; }
  .punit { font-size: 10px; color: var(--muted); }
  .panel canvas { width: 100%; height: 46px; display: block; margin-top: 6px; }
  .prange { display: flex; justify-content: space-between; font-size: 9.5px; color: var(--muted);
            font-variant-numeric: tabular-nums; margin-top: 2px; }
  .badge { font-size: 9px; padding: 1px 6px; border-radius: 999px; text-transform: uppercase; }
  .badge.warn { background: rgba(201,133,0,.2); color: var(--warn); }
  .badge.bad { background: rgba(230,103,103,.2); color: var(--bad); }
  .axis { display: flex; justify-content: space-between; font-size: 10px; color: var(--muted);
          margin-top: 8px; font-variant-numeric: tabular-nums; }
  .empty { color: var(--muted); padding: 20px; }

  /* --- mode 3: table --- */
  #table table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
  #table th, #table td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--line);
                         font-size: 12.5px; }
  #table th { color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: 10px;
              letter-spacing: .07em; position: sticky; top: 0; background: var(--bg); }
  #table td.num { text-align: right; font-weight: 600; }
  #table td.dim { color: var(--muted); font-weight: 400; }
  #table tr.stale td.num { color: var(--muted); }
  #table .src { font-size: 10px; padding: 1px 6px; border-radius: 4px; background: var(--surface2);
                color: var(--muted); }

  footer { color: var(--muted); font-size: 11px; margin-top: 24px; }
</style>
</head>
<body>
<header>
  <h1>F10 520d</h1>
  <div class="chips">
    <div class="chip"><span id="dot" class="dot off"></span><b id="status">connecting</b></div>
    <div class="chip">VIN <b id="vin">-</b></div>
    <div class="chip">ECU <b id="ecu">-</b></div>
    <div class="chip"><b id="hz">0</b> Hz</div>
    <div class="chip"><b id="lat">0</b> ms</div>
    <div class="chip">logged <b id="rows">0</b></div>
    <div class="chip" id="drivemodechip" title="how hard to poll the car">
      mode <select id="drivemode"></select><b id="driveduty"></b>
    </div>
    <div class="chip" id="syncchip" title="click to pause/resume sync"
         style="cursor:pointer">
      <span id="syncdot" class="dot off"></span>sync
      <b id="syncstate">-</b><span id="syncpend"></span>
    </div>
    <div class="chip" id="sharechip" title="create a temporary public link"
         style="cursor:pointer">share</div>
    <div class="chip" id="sharedbadge" style="display:none">shared &middot; live</div>
  </div>
</header>

<div class="sheet" id="sharesheet">
  <div class="sheetbox">
    <h2>Share a live link</h2>
    <p class="dim">
      Anyone with the link sees this dashboard live, read-only, with the VIN
      masked and no drive history. It stops working when it expires, when you
      revoke it, or when the dashboard restarts.
    </p>
    <div class="row">
      <label for="sharettl">Expires in</label>
      <select id="sharettl">
        <option value="900">15 minutes</option>
        <option value="3600" selected>1 hour</option>
        <option value="14400">4 hours</option>
        <option value="43200">12 hours</option>
      </select>
      <button id="sharemint">Create link</button>
    </div>
    <div class="row" id="sharenew" style="display:none">
      <input id="shareurl" readonly>
      <button id="sharecopy">Copy</button>
    </div>
    <div id="sharelist"></div>
    <div class="row">
      <button id="sharerevokeall" class="danger">Revoke all</button>
      <button id="shareclose">Close</button>
    </div>
  </div>
</div>

<div class="modes" id="modeswitch">
  <button data-mode="drive">Drive</button>
  <button data-mode="detail">Detail</button>
  <button data-mode="table">All data</button>
</div>

<!-- shared history controls (detail mode) -->
<div class="controls" id="histctl">
  <label>Run</label>
  <select id="run"></select>
  <label>Window</label>
  <div class="seg" id="win"></div>
  <button class="ctl" id="reload">Reload history</button>
  <span id="hint" style="color:var(--muted);font-size:11.5px"></span>
</div>

<!-- ===================== MODE 1: DRIVE ===================== -->
<section id="drive" class="hidden">
  <div class="mcluster">
    <div class="revbar" id="revbar"></div>
    <div class="herorow">
      <div class="herogauge">
        <svg viewBox="0 0 200 165" id="bg-rpm"></svg>
        <div class="heroval" id="hv-rpm">--</div>
        <div class="herolabel">RPM</div>
      </div>
      <div class="gearbox" id="gearbox">
        <div class="gearlabel">GEAR</div>
        <div class="gearnum" id="gearnum">–</div>
        <div class="mstripe"></div>
      </div>
      <div class="herogauge">
        <svg viewBox="0 0 200 165" id="bg-speed"></svg>
        <div class="heroval" id="hv-speed">--</div>
        <div class="herolabel">KM/H</div>
      </div>
    </div>
    <div class="tilerow" id="tilerow"></div>
  </div>
</section>

<!-- ===================== MODE 2: DETAIL ===================== -->
<section id="detail" class="hidden">
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
</section>

<!-- ===================== MODE 3: TABLE ===================== -->
<section id="table" class="hidden">
  <table>
    <thead><tr>
      <th>Channel</th><th>Key</th><th style="text-align:right">Value</th><th>Unit</th>
      <th style="text-align:right">Min</th><th style="text-align:right">Max</th>
      <th style="text-align:right">Age</th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table>
</section>

<footer>
  Read-only OBD-2 service 01 + verified BMW proprietary reads over HSFZ / ENET.
  Nothing is written to the vehicle.
</footer>

<script>
const WINDOWS = [["1m",60],["5m",300],["15m",900],["1h",3600],["all",null]];
let meta = [], metaByKey = {}, metaVersion = -1;
let series = {};              // key -> [[ts, value], ...] (history + live, detail mode)
let latest = {};             // key -> latest value
let stat = {};               // key -> {min, max, ts} running, since page load
let winSec = 300, runId = null, liveRun = null, dirty = false, hoverTs = null, lastTs = 0;
let MODE = localStorage.getItem("f10mode") || "drive";

const el = id => document.getElementById(id);
const fmt = (v, d) => v === undefined || v === null ? "--" :
  (typeof v === "number" ? v.toFixed(d) : String(v));
function clockLabel(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"});
}
function ageLabel(ts, now) {
  if (!ts) return "--";
  const s = Math.max(0, now - ts);
  return s < 2 ? "live" : s < 90 ? Math.round(s) + "s" : Math.round(s/60) + "m";
}

/* which channels lead the Drive view, best-effort by key (present ones win) */
const DRIVE_PRIMARY = ["rpm","boost","speed"];
const DRIVE_SECONDARY = [
  "gear","n47d_gbx_oil_temp","n47d_turbine_speed",
  "n47d_rail_act","rail","n47d_boost_act","map","load","throttle","pedal","n47d_pedal",
  "coolant","n47d_coolant","oil","n47d_oil_temp","n47d_engine_temp",
  "n47d_maf_per_cyl","maf","n47d_charge_air_temp","iat","voltage",
  "n47d_dpf_dp","n47d_exh_temp_pre_dpf","n47d_soot_meas","n47d_ambient_press"
];
function present(keys) {
  const seen = new Set(); const out = [];
  for (const k of keys) if (metaByKey[k] && !seen.has(k)) { seen.add(k); out.push(k); }
  return out;
}

/* ---------------------------------------------------------- gauges */
function polar(cx, cy, r, deg) { const a=(deg-90)*Math.PI/180; return [cx+r*Math.cos(a), cy+r*Math.sin(a)]; }
function arcPath(cx, cy, r, a0, a1) {
  const [x0,y0]=polar(cx,cy,r,a0),[x1,y1]=polar(cx,cy,r,a1);
  return `M ${x0} ${y0} A ${r} ${r} 0 ${(a1-a0)>180?1:0} 1 ${x1} ${y1}`;
}
const A0=215, A1=505;
function gaugeSVG(frac, ticks, color, r, sw) {
  frac = Math.max(0, Math.min(1, frac));
  const cx=100, cy=98;
  let s = `<path d="${arcPath(cx,cy,r,A0,A1)}" stroke="var(--line)" stroke-width="${sw}" fill="none" stroke-linecap="round"/>`;
  if (frac > 0.001)
    s += `<path d="${arcPath(cx,cy,r,A0,A0+(A1-A0)*frac)}" stroke="${color}" stroke-width="${sw}" fill="none" stroke-linecap="round"/>`;
  for (const t of ticks) {
    const a=A0+(A1-A0)*t.f;
    const [x0,y0]=polar(cx,cy,r-12,a),[x1,y1]=polar(cx,cy,r-18,a);
    s += `<line x1="${x0}" y1="${y0}" x2="${x1}" y2="${y1}" stroke="#3c485c" stroke-width="2"/>`;
    const [lx,ly]=polar(cx,cy,r-30,a);
    s += `<text x="${lx}" y="${ly}" fill="var(--muted)" font-size="9.5" text-anchor="middle" dominant-baseline="middle">${t.t}</text>`;
  }
  return s;
}
function drawGauge(svg, frac, ticks, color) { svg.innerHTML = gaugeSVG(frac, ticks, color, 74, 11); }
const T = (lo, hi, n) => Array.from({length:n+1}, (_,i) => ({f:i/n, t:Math.round(lo+(hi-lo)*i/n)}));

/* alarms (shared) */
function statusOf(key, v) {
  if (v === undefined || v === null) return null;
  if (key==="coolant"||key==="n47d_coolant") return v>115?"bad":v>108?"warn":null;
  if (key==="oil"||key==="n47d_oil_temp"||key==="n47d_engine_temp") return v>125?"bad":v>115?"warn":null;
  if (key==="voltage") return v<11.8?"bad":v<12.2?"warn":null;
  if (key==="cattemp") return v>700?"warn":null;
  return null;
}

/* ---------------------------------------------------------- mode 1: drive */
/* which channels fill the secondary tile row, best available first */
const DRIVE_TILES = ["boost","n47d_boost_act","n47d_rail_act","coolant","n47d_coolant",
  "oil","n47d_oil_temp","n47d_gbx_oil_temp","n47d_turbine_speed","load","n47d_maf_per_cyl",
  "maf","n47d_dpf_dp","voltage","distance"];
const RPM_MAX = 5200, RPM_REDLINE = 4600, REV_LEDS = 22;

function buildDrive() {
  // rev-bar LEDs
  const rb = el("revbar"); rb.innerHTML = "";
  for (let i=0;i<REV_LEDS;i++){ const d=document.createElement("div"); d.className="led"; rb.appendChild(d); }
  // secondary tiles from whatever channels exist
  const tiles = present(DRIVE_TILES).slice(0, 6);
  const tr = el("tilerow"); tr.innerHTML = "";
  for (const k of tiles) {
    const m = metaByKey[k];
    const d = document.createElement("div");
    d.className = "mtile"; d.id = "tile-"+k;
    d.innerHTML = `<div class="tname">${m.label}</div>` +
      `<div><span class="tval" id="tv-${k}">--</span><span class="tunit">${m.unit}</span></div>`;
    tr.appendChild(d);
  }
}

function heroGauge(svgId, valId, key, unit, digits, gmax, color) {
  const m = metaByKey[key];
  const v = latest[key];
  const svg = el(svgId); if (!svg) return;
  const hi = gmax || (m ? m.hi : 100);
  const frac = (v||0) / Math.max(hi, 1e-6);
  svg.innerHTML = gaugeSVG(frac, T(0, hi, 5), color, 82, 14);
  el(valId).textContent = (v===undefined||v===null) ? "--" : (unit==="k" ? (v/1000).toFixed(1) : Math.round(v));
}

function renderDrive() {
  // rev bar: green -> the last few red (shift light)
  const rpm = latest.rpm || 0;
  const lit = Math.round(REV_LEDS * Math.min(1, rpm / RPM_MAX));
  const leds = el("revbar").children;
  for (let i=0;i<leds.length;i++){
    const on = i < lit;
    const red = i >= REV_LEDS * (RPM_REDLINE/RPM_MAX);
    const mid = i >= REV_LEDS * 0.6;
    leds[i].style.background = !on ? "#171d28"
      : red ? "var(--m-red)" : mid ? "var(--warn)" : "var(--m-lblue)";
    leds[i].style.boxShadow = on ? "0 0 6px "+(red?"var(--m-red)":mid?"var(--warn)":"var(--m-lblue)") : "none";
  }
  // hero gauges: rpm (x1000) + speed
  heroGauge("bg-rpm","hv-rpm","rpm","k",1, RPM_MAX,
            rpm>RPM_REDLINE ? "var(--m-red)" : "var(--m-lblue)");
  el("hv-rpm").textContent = rpm ? (rpm/1000).toFixed(1) : "--";
  heroGauge("bg-speed","hv-speed","speed","",0, 250, "var(--accent)");
  // big gear
  const g = latest.gear;
  const gn = el("gearnum");
  gn.textContent = (g===undefined||g===null) ? "N" : String(Math.round(g));
  gn.classList.toggle("rev", rpm>RPM_REDLINE);
  // tiles
  for (const k of present(DRIVE_TILES)) {
    const tv = el("tv-"+k); if (!tv) continue;
    tv.textContent = fmt(latest[k], metaByKey[k].digits);
    const st = statusOf(k, latest[k]);
    const tile = el("tile-"+k);
    if (tile) tile.className = "mtile" + (st ? " "+st : "");
  }
}
function sparkline(cvId, key) {
  const cv = el(cvId); if (!cv) return;
  const data = (series[key] || []).slice(-120);
  const dpr = window.devicePixelRatio||1, w=cv.clientWidth, h=cv.clientHeight;
  if (cv.width !== Math.round(w*dpr)) { cv.width=w*dpr; cv.height=h*dpr; }
  const x = cv.getContext("2d"); x.setTransform(dpr,0,0,dpr,0,0); x.clearRect(0,0,w,h);
  if (data.length < 2) return;
  let lo=Infinity, hi=-Infinity;
  for (const p of data) { lo=Math.min(lo,p[1]); hi=Math.max(hi,p[1]); }
  if (hi-lo<1e-9) { hi+=.5; lo-=.5; }
  const px = i => (i/(data.length-1))*w, py = v => h-2-(h-4)*(v-lo)/(hi-lo);
  x.beginPath(); data.forEach((p,i)=> i?x.lineTo(px(i),py(p[1])):x.moveTo(px(i),py(p[1])));
  x.strokeStyle="var(--series)"; x.lineWidth=1.5; x.lineJoin="round"; x.stroke();
}

/* ---------------------------------------------------------- mode 2: detail panels */
function buildPanels() {
  const box = el("panels"); box.innerHTML = "";
  if (!meta.length) { box.innerHTML='<div class="empty">waiting for channel list...</div>'; return; }
  for (const m of meta) {
    const d = document.createElement("div"); d.className="panel";
    d.innerHTML =
      `<div class="phead"><span class="pname">${m.label}</span>` +
      `<span id="badge-${m.key}"></span>` +
      `<span class="pval" id="pv-${m.key}">--</span><span class="punit">${m.unit}</span></div>` +
      `<canvas id="pc-${m.key}"></canvas>` +
      `<div class="prange"><span id="plo-${m.key}"></span><span id="phi-${m.key}"></span></div>`;
    box.appendChild(d);
    const cv = d.querySelector("canvas");
    cv.addEventListener("mousemove", e => {
      const r = cv.getBoundingClientRect(); const [t0,t1]=windowBounds();
      hoverTs = t0 + (t1-t0)*((e.clientX-r.left)/r.width); dirty=true;
    });
    cv.addEventListener("mouseleave", () => { hoverTs=null; dirty=true; });
  }
}
function windowBounds() {
  let t1=0, t0=Infinity;
  for (const k in series) { const s=series[k]; if (!s.length) continue;
    t1=Math.max(t1,s[s.length-1][0]); t0=Math.min(t0,s[0][0]); }
  if (!isFinite(t0)||!t1) return [0,0];
  if (winSec) t0=Math.max(t0, t1-winSec);
  return [t0,t1];
}
function drawPanel(m, t0, t1) {
  const cv = el("pc-"+m.key); if (!cv) return;
  const data = (series[m.key]||[]).filter(p => p[0]>=t0 && p[0]<=t1);
  const dpr=window.devicePixelRatio||1, w=cv.clientWidth, h=cv.clientHeight;
  if (cv.width!==Math.round(w*dpr)) { cv.width=w*dpr; cv.height=h*dpr; }
  const x=cv.getContext("2d"); x.setTransform(dpr,0,0,dpr,0,0); x.clearRect(0,0,w,h);
  if (data.length < 2) { el("plo-"+m.key).textContent=""; el("phi-"+m.key).textContent="no data in window"; return; }
  let lo=Infinity, hi=-Infinity;
  for (const p of data) { lo=Math.min(lo,p[1]); hi=Math.max(hi,p[1]); }
  if (hi-lo<1e-9) { hi+=.5; lo-=.5; }
  const pad=(hi-lo)*.12; lo-=pad; hi+=pad;
  x.strokeStyle="var(--grid)"; x.lineWidth=1;
  for (let i=0;i<=2;i++){ const y=3+(h-8)*i/2; x.beginPath(); x.moveTo(0,y); x.lineTo(w,y); x.stroke(); }
  const px=t=>((t-t0)/Math.max(t1-t0,1e-6))*w, py=v=>3+(h-8)*(1-(v-lo)/(hi-lo));
  x.beginPath(); data.forEach((p,i)=> i?x.lineTo(px(p[0]),py(p[1])):x.moveTo(px(p[0]),py(p[1])));
  x.strokeStyle="var(--series)"; x.lineWidth=2; x.lineJoin="round"; x.stroke();
  let shown = data[data.length-1][1];
  if (hoverTs!==null && hoverTs>=t0 && hoverTs<=t1) {
    let best=null, bd=Infinity;
    for (const p of data){ const d=Math.abs(p[0]-hoverTs); if (d<bd){bd=d;best=p;} }
    if (best) { shown=best[1]; const hx=px(best[0]);
      x.strokeStyle="#4a5670"; x.lineWidth=1; x.beginPath(); x.moveTo(hx,0); x.lineTo(hx,h); x.stroke();
      x.beginPath(); x.arc(hx,py(best[1]),3,0,Math.PI*2); x.fillStyle="var(--series)"; x.fill();
      x.strokeStyle="var(--surface)"; x.lineWidth=2; x.stroke(); }
  }
  el("pv-"+m.key).textContent = fmt(shown, m.digits);
  el("plo-"+m.key).textContent = fmt(lo+pad, m.digits);
  el("phi-"+m.key).textContent = fmt(hi-pad, m.digits);
  const st = statusOf(m.key, shown), b = el("badge-"+m.key);
  b.className = st ? "badge "+st : ""; b.textContent = st==="bad"?"critical":st==="warn"?"high":"";
}
function drawDetail() {
  const [t0,t1]=windowBounds();
  for (const m of meta) drawPanel(m, t0, t1);
  el("ax0").textContent = clockLabel(t0);
  el("ax1").textContent = clockLabel(t1) + (hoverTs?"   (hover: "+clockLabel(hoverTs)+")":"");
  const secs = Math.round(t1-t0);
  el("span").textContent = t1 ? `- ${secs}s window, ${meta.length} channels` : "";
}

/* ---------------------------------------------------------- mode 3: table */
function renderTable() {
  const now = latest.__ts || 0;
  const tb = el("tbody");
  const rows = meta.map(m => {
    const v = latest[m.key], s = stat[m.key] || {};
    const age = ageLabel(s.ts, now), stale = s.ts && (now - s.ts) > 5;
    const prox = m.key.startsWith("n47d_");
    return `<tr class="${stale?'stale':''}">` +
      `<td>${m.label}</td>` +
      `<td class="dim">${m.key}${prox?' <span class="src">DDE</span>':''}</td>` +
      `<td class="num">${fmt(v, m.digits)}</td>` +
      `<td class="dim">${m.unit}</td>` +
      `<td class="num dim">${s.min!==undefined?fmt(s.min,m.digits):'--'}</td>` +
      `<td class="num dim">${s.max!==undefined?fmt(s.max,m.digits):'--'}</td>` +
      `<td class="num dim">${age}</td></tr>`;
  });
  tb.innerHTML = rows.join("") || '<tr><td colspan="7" class="dim">waiting for data...</td></tr>';
}

/* ---------------------------------------------------------- head + gauges (detail) */
function renderHead(s) {
  el("status").textContent = s.status || "-";
  el("dot").className = "dot " + (s.connected ? "on" : "off");
  el("vin").textContent = s.vin || "-";
  el("ecu").textContent = s.ecu || "-";
  el("hz").textContent = (s.hz||0).toFixed(1);
  el("lat").textContent = (s.latency_ms||0).toFixed(0);
  el("rows").textContent = (s.rows||0).toLocaleString();
  const v = s.values || {};
  drawGauge(el("g-rpm"), (v.rpm||0)/5000, T(0,5,5), v.rpm>4400?"var(--bad)":"var(--series)");
  el("v-rpm").textContent = v.rpm===undefined?"--":Math.round(v.rpm);
  drawGauge(el("g-boost"), ((v.boost||0)+0.2)/2.4,
    [{f:0,t:"-.2"},{f:.29,t:".5"},{f:.5,t:"1"},{f:.71,t:"1.5"},{f:1,t:"2.2"}],
    v.boost>1.9?"var(--warn)":"var(--good)");
  el("v-boost").textContent = v.boost===undefined?"--":v.boost.toFixed(2);
  drawGauge(el("g-speed"), (v.speed||0)/250, T(0,250,5), "var(--accent)");
  el("v-speed").textContent = v.speed===undefined?"--":Math.round(v.speed);
}

/* ---------------------------------------------------------- data plumbing */
async function loadMeta() {
  const j = await (await fetch(API+"/api/meta")).json();
  meta = j.meta; metaVersion = j.meta_version;
  metaByKey = {}; for (const m of meta) metaByKey[m.key] = m;
  buildDrive(); buildPanels();
}
async function loadRuns() {
  const runs = await (await fetch(API+"/api/runs")).json();
  if (runs.error) return;
  liveRun = runs.length ? runs[0].id : null;
  const sel = el("run"); sel.innerHTML = "";
  for (const r of runs) {
    const o = document.createElement("option"); o.value = r.id;
    const when = new Date(r.started*1000).toLocaleString();
    o.textContent = `#${r.id}  ${when}  ${r.samples.toLocaleString()} pts` + (r.ended?"":"  (live)");
    sel.appendChild(o);
  }
  if (runId === null) runId = liveRun;
  sel.value = runId;
}
async function loadHistory() {
  el("hint").textContent = "loading history...";
  const q = new URLSearchParams({points: 900});
  if (runId!==null) q.set("run", runId);
  if (winSec) q.set("seconds", winSec);
  const j = await (await fetch(API+"/api/history?"+q)).json();
  series = j.series || {};
  for (const m of meta) if (!series[m.key]) series[m.key] = [];
  el("hint").textContent = runId===liveRun ? "live run - appending in real time"
                                           : "historical run - live updates paused";
  dirty = true;
}
function appendLive(ts, values) {
  latest = Object.assign({}, values); latest.__ts = ts;
  for (const k in values) {
    const v = values[k];
    if (typeof v !== "number") continue;
    const s = stat[k] || (stat[k] = {min:v, max:v, ts:ts});
    s.min = Math.min(s.min, v); s.max = Math.max(s.max, v); s.ts = ts;
  }
  if (runId === liveRun) {
    for (const k in values) (series[k] = series[k] || []).push([ts, values[k]]);
    const cutoff = ts - (winSec ? winSec*1.2 : 7200);
    for (const k in series) { const s=series[k]; let i=0; while(i<s.length && s[i][0]<cutoff) i++; if (i) s.splice(0,i); }
  }
  dirty = true;
}

/* ---------------------------------------------------------- mode switching + render loop */
function setMode(mode) {
  MODE = mode; localStorage.setItem("f10mode", mode);
  for (const b of el("modeswitch").children) b.className = b.dataset.mode===mode ? "on" : "";
  el("drive").classList.toggle("hidden", mode!=="drive");
  el("detail").classList.toggle("hidden", mode!=="detail");
  el("table").classList.toggle("hidden", mode!=="table");
  el("histctl").classList.toggle("hidden", mode!=="detail");
  dirty = true;
}
for (const b of el("modeswitch").children) b.onclick = () => setMode(b.dataset.mode);

const seg = el("win");
WINDOWS.forEach(([label, secs]) => {
  const b = document.createElement("button"); b.className="ctl"; b.textContent=label;
  if (secs===winSec) b.classList.add("on");
  b.onclick = () => { winSec=secs; [...seg.children].forEach(c=>c.className="ctl"); b.classList.add("on"); loadHistory(); };
  seg.appendChild(b);
});
el("run").onchange = e => { runId = parseInt(e.target.value,10); loadHistory(); };
el("reload").onclick = () => { loadRuns(); loadHistory(); };

/* ---------------------------------------------------------- sync agent */
/* The sync agent (infra/sync/agent.py) runs on this machine and exposes
   a CORS-enabled control endpoint. The dashboard polls it so sync can be
   watched and paused during a drive. If the agent is not running the
   chip just shows "off". This talks to a separate process; live.py's
   recording path is untouched. */
/* Same-origin: live.py proxies the agent's status at /api/sync, so this
   works both on the Pi and through the server's reverse proxy. Pause and
   resume still go straight to the agent, which only works when the page is
   opened on the Pi itself - deliberately, so a public vhost cannot pause
   syncing. */
/* Served under /s/ for a share link, at / for the owner. Every same-origin
   call is built off API so one page serves both. */
const API = window.__F10_API__ || "";
const SHARED = !!window.__F10_SHARE__;
const SYNC_BASE = `http://${location.hostname || "localhost"}:8091`;
let syncEnabled = null;
async function pollSync() {
  try {
    const s = await (await fetch(API+"/api/sync", {cache: "no-store"})).json();
    syncEnabled = (s.state === "unreachable") ? null : s.enabled;
    el("syncdot").className = "dot " + (s.enabled && s.state !== "offline" ? "on" : "off");
    el("syncstate").textContent = s.state || "-";
    let pend = 0;
    for (const k in (s.databases || {})) pend += (s.databases[k].pending || 0);
    el("syncpend").textContent = pend > 0 ? `  ${pend.toLocaleString()} pending` : "";
    el("syncchip").title = (s.last_error ? "error: " + s.last_error + " — " : "") +
      "click to " + (s.enabled ? "pause" : "resume") + " sync";
  } catch (e) {
    el("syncdot").className = "dot off";
    el("syncstate").textContent = "off";
    el("syncpend").textContent = "";
    syncEnabled = null;
  }
}
el("syncchip").onclick = async () => {
  if (syncEnabled === null) return;                 // agent not reachable
  try {
    await fetch(SYNC_BASE + (syncEnabled ? "/sync/pause" : "/sync/resume"),
                {method: "POST"});
    pollSync();
  } catch (e) {}
};
setInterval(pollSync, 3000);
pollSync();

/* ------------------------------------------------------- drive mode */
/* How hard to poll the car. The picker POSTs a request; the poll loop
   applies it between cycles, so the chip shows the REQUESTED mode in
   amber until a snapshot comes back confirming it took effect. Without
   that the control would look instant and lie whenever the link is down. */
let modeWanted = null;
async function loadModes() {
  try {
    const m = await (await fetch(API+"/api/modes", {cache: "no-store"})).json();
    const sel = el("drivemode");
    sel.innerHTML = "";
    for (const mode of m.modes || []) {
      const o = document.createElement("option");
      o.value = mode.name;
      o.textContent = mode.name;
      o.title = mode.description || "";
      sel.appendChild(o);
    }
    sel.value = m.current;
    el("drivemodechip").title =
      (m.modes || []).map(x => `${x.name} — ${x.description}`).join("\n");
  } catch (e) {}
}
el("drivemode").onchange = async ev => {
  const want = ev.target.value;
  modeWanted = want;
  el("drivemodechip").classList.add("pending");
  try {
    const r = await fetch(API+"/api/mode", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({mode: want}),
    });
    if (!r.ok) throw new Error(await r.text());
  } catch (e) {
    /* Put the picker back where it was: nothing changed on the car. */
    modeWanted = null;
    el("drivemodechip").classList.remove("pending");
    loadModes();
  }
};
function renderMode(s) {
  if (SHARED || !s.mode) return;
  const sel = el("drivemode");
  if (modeWanted !== null && s.mode === modeWanted) modeWanted = null;
  if (modeWanted === null) {
    el("drivemodechip").classList.remove("pending");
    if (document.activeElement !== sel) sel.value = s.mode;
  }
  const asleep = s.duty === "asleep";
  el("drivemodechip").classList.toggle("asleep", asleep);
  el("driveduty").textContent = asleep ? " · asleep" : "";
}
if (!SHARED) loadModes();

const es = new EventSource(API+"/api/stream");
es.onmessage = async e => {
  const s = JSON.parse(e.data);
  if (s.meta_version !== metaVersion) { await loadMeta(); if (MODE==="detail") await loadHistory(); }
  renderHead(s);
  renderMode(s);
  if (s.connected && s.ts !== lastTs) { lastTs = s.ts; appendLive(s.ts, s.values||{}); }
};
es.onerror = () => { el("dot").className="dot off"; el("status").textContent="server unreachable"; };

function render() {
  if (!dirty) return; dirty = false;
  if (MODE === "drive") renderDrive();
  else if (MODE === "detail") drawDetail();
  else renderTable();
}
function tick() { render(); requestAnimationFrame(tick); }
requestAnimationFrame(tick);
window.addEventListener("resize", () => { dirty = true; });

/* ---------------------------------------------------------------- sharing */
const shareSheet = el("sharesheet");
function fmtLeft(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60);
  return h ? `${h}h ${m}m` : (m ? `${m}m` : `${sec}s`);
}
async function loadShares() {
  const box = el("sharelist");
  let j;
  try { j = await (await fetch(API+"/api/share", {cache:"no-store"})).json(); }
  catch (e) { box.innerHTML = '<div class="sharerow dim">could not reach the server</div>'; return; }
  const links = j.links || [];
  if (!links.length) { box.innerHTML = '<div class="sharerow dim">no active links</div>'; return; }
  const now = Date.now()/1000;
  box.innerHTML = links.map(l =>
    `<div class="sharerow"><code>${l.url}</code>` +
    `<span class="dim">${fmtLeft(l.expires-now)} left &middot; ${l.hits} hit${l.hits===1?"":"s"}</span>` +
    `<button data-revoke="${l.token}">Revoke</button></div>`).join("");
  box.querySelectorAll("[data-revoke]").forEach(b => {
    b.onclick = async () => {
      await fetch(API+"/api/share/revoke", {method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({token: b.getAttribute("data-revoke")})});
      loadShares();
    };
  });
}
if (!SHARED) {
  el("sharechip").onclick = () => { shareSheet.classList.add("open"); loadShares(); };
  el("shareclose").onclick = () => shareSheet.classList.remove("open");
  shareSheet.onclick = e => { if (e.target === shareSheet) shareSheet.classList.remove("open"); };
  el("sharemint").onclick = async () => {
    const ttl = parseInt(el("sharettl").value, 10);
    const r = await fetch(API+"/api/share", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify({ttl})});
    const j = await r.json();
    if (j.url) { el("shareurl").value = j.url; el("sharenew").style.display = "flex"; }
    loadShares();
  };
  el("sharecopy").onclick = async () => {
    const input = el("shareurl");
    input.select();
    try { await navigator.clipboard.writeText(input.value); }
    catch (e) { document.execCommand("copy"); }
    el("sharecopy").textContent = "Copied";
    setTimeout(() => { el("sharecopy").textContent = "Copy"; }, 1500);
  };
  el("sharerevokeall").onclick = async () => {
    await fetch(API+"/api/share/revoke", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify({all:true})});
    el("sharenew").style.display = "none";
    loadShares();
  };
}

/* A share viewer gets the live views only: the owner-only chips go away,
   and Detail is removed because its history endpoints are not served
   under the share prefix. */
if (SHARED) {
  el("sharechip").style.display = "none";
  el("syncchip").style.display = "none";
  //: A share viewer must not be able to change how the car is polled.
  //: The POST is refused server-side for the /s/ surface regardless;
  //: this keeps the control from appearing at all.
  el("drivemodechip").style.display = "none";
  el("sharedbadge").style.display = "";
  const vinChip = el("vin").closest(".chip"); if (vinChip) vinChip.style.display = "none";
  const detail = document.querySelector('[data-mode="detail"]');
  if (detail) detail.style.display = "none";
  if (MODE === "detail") MODE = "drive";
}

setMode(MODE);
(async () => {
  await loadMeta();
  if (!SHARED) { await loadRuns(); await loadHistory(); }
})();
</script>
</body>
</html>
"""


#: The same page, told at load time that it is the public view. One page
#: means a share viewer can never drift from what the owner sees.
SHARE_PAGE = PAGE.replace(
    "</head>",
    '<script>window.__F10_API__ = "/s"; window.__F10_SHARE__ = true;</script>\n</head>',
    1,
)

SHARE_DENIED_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Link expired</title>
<style>
 body { margin:0; min-height:100vh; display:flex; align-items:center;
        justify-content:center; background:#0f1113; color:#e6e6e6;
        font:14px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
 div { text-align:center; padding:24px; }
 h1 { font-size:17px; margin:0 0 8px; }
 p { color:#8b9196; margin:0; }
</style></head>
<body><div>
<h1>This link is no longer valid</h1>
<p>Share links expire, can be revoked, and do not survive a restart.<br>
Ask for a fresh one.</p>
</div></body></html>
"""


# ------------------------------------------------------------------ query


#: Opt-in VIN masking for the HTTP/SSE API. OFF by default: the dashboard
#: serves the full VIN as it always has, and the deployed instance is put
#: behind authentication instead. `--redact-vin` turns masking on for a
#: deployment where the dashboard is exposed without auth. Storage is never
#: affected - SQLite and the lake always hold the full VIN (it is the
#: vehicle_id there).
REDACT_VIN = False


def mask_vin(vin: Optional[str]) -> Optional[str]:
    """
    Mask a VIN unconditionally: keep the last 4 characters, which is
    enough to tell cars apart, and hide the rest.
    """
    if vin is None:
        return None

    text = str(vin)

    return ("*" * max(0, len(text) - 4)) + text[-4:] if len(text) > 4 else "****"


def redact_vin(vin: Optional[str]) -> Optional[str]:
    """Mask a VIN for the HTTP API. No-op unless `--redact-vin` is on."""
    if vin is None or not REDACT_VIN:
        return vin

    return mask_vin(vin)


def public_snapshot(snap: Dict) -> Dict:
    """A telemetry snapshot with the VIN masked, for the HTTP/SSE API."""
    if not REDACT_VIN or "vin" not in snap:
        return snap

    out = dict(snap)
    out["vin"] = redact_vin(out.get("vin"))

    return out


# ---------------------------------------------------------------- sharing

#: URL prefix for the public, token-gated view. Everything under it is
#: read-only and live-only; nginx serves this prefix without Basic Auth
#: (see infra/ansible/roles/nginx/templates/dashboard.conf.j2).
SHARE_PREFIX = "/s"

#: The cookie that carries the token after the first click, so the SSE
#: stream and the API calls do not each need the query string.
SHARE_COOKIE = "f10share"

#: Exactly what a share viewer may reach, relative to SHARE_PREFIX.
#: Deliberately live-only: no /api/runs or /api/history (past drives are
#: not the owner's to hand out by accident) and no /api/sync (it exposes
#: local database paths and the agent's control surface).
SHARE_ALLOWED = frozenset({"/", "/api/snapshot", "/api/stream", "/api/meta"})

#: Snapshot fields a share viewer never sees. The VIN is masked rather
#: than dropped so the page still has something to render.
SHARE_HIDDEN = ("gateway", "ecus")

#: Offered link lifetimes: 15 min, 1 h, 4 h, 12 h.
SHARE_TTL_CHOICES = (900, 3600, 14400, 43200)


def share_snapshot(snap: Dict) -> Dict:
    """
    A telemetry snapshot safe to hand to a share-link viewer.

    The VIN is masked here *unconditionally* - independently of the
    global `--redact-vin` switch, which governs the owner's own view.
    A share link must never be able to leak it.
    """
    out = dict(snap)

    if out.get("vin"):
        out["vin"] = mask_vin(out["vin"])

    for field in SHARE_HIDDEN:
        out.pop(field, None)

    out["shared"] = True

    return out


class ShareTokens:
    """
    Bearer tokens granting temporary, read-only, live-only access.

    Minted by the owner from the authenticated dashboard. They expire on
    their own and can be revoked early. Nothing is persisted on purpose:
    a restart invalidates every outstanding link, which is the safe
    direction to fail.
    """

    def __init__(self, max_active: int = 16) -> None:
        self._lock = threading.Lock()
        self._tokens: Dict[str, Dict[str, Any]] = {}
        self.max_active = max_active

    def mint(self, ttl: float, label: str = "") -> Dict[str, Any]:
        now = time.time()
        token = secrets.token_urlsafe(24)
        entry = {
            "token": token, "created": now, "expires": now + float(ttl),
            "label": str(label)[:64], "hits": 0, "last_seen": None,
        }

        with self._lock:
            self._prune(now)

            #
            # A bound on outstanding links, so a stuck script cannot mint
            # without limit. The owner is the one minting, so drop the
            # oldest rather than refusing the new one.
            #
            while len(self._tokens) >= self.max_active:
                oldest = min(self._tokens.values(), key=lambda e: e["created"])
                self._tokens.pop(oldest["token"], None)

            self._tokens[token] = entry

        return dict(entry)

    def validate(self, token: Optional[str]) -> bool:
        if not token:
            return False

        now = time.time()

        with self._lock:
            self._prune(now)

            #
            # compare_digest against every candidate, without breaking out
            # early: whether a token is valid must not be readable from
            # how long the check took.
            #
            hit = None

            for known, entry in self._tokens.items():
                if hmac.compare_digest(known, token):
                    hit = entry

            if hit is None:
                return False

            hit["hits"] += 1
            hit["last_seen"] = now

            return True

    def remaining(self, token: str) -> float:
        """Seconds left on a token, 0 if it is unknown or already past."""
        now = time.time()

        with self._lock:
            entry = self._tokens.get(token)

            return max(0.0, entry["expires"] - now) if entry else 0.0

    def revoke(self, token: str) -> bool:
        with self._lock:
            return self._tokens.pop(token, None) is not None

    def revoke_all(self) -> int:
        with self._lock:
            count = len(self._tokens)
            self._tokens.clear()

            return count

    def active(self) -> List[Dict[str, Any]]:
        now = time.time()

        with self._lock:
            self._prune(now)

            return sorted(
                (dict(e) for e in self._tokens.values()),
                key=lambda e: e["created"],
            )

    def _prune(self, now: float) -> None:
        """Drop expired tokens. Caller holds the lock."""
        for token in [t for t, e in self._tokens.items() if e["expires"] <= now]:
            self._tokens.pop(token, None)


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
            "id": r[0], "started": r[1], "ended": r[2],
            "vin": redact_vin(r[3]),
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


def make_handler(
    tel: Telemetry,
    db_path: Optional[str],
    shares: Optional["ShareTokens"] = None,
    share_base_url: str = "",
    modes: Optional["ModeControl"] = None,
    diag: Optional["Diagnostics"] = None,
):
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

        def _body(self, ctype: str, payload: bytes,
                  extra: Optional[Dict] = None):
            self._headers(ctype, extra)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _json_body(self, obj, code: int = 200):
            payload = json.dumps(obj).encode()

            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        # -- sharing ------------------------------------------------

        def _cookie(self, name: str) -> Optional[str]:
            raw = self.headers.get("Cookie") or ""

            for part in raw.split(";"):
                key, _, value = part.strip().partition("=")

                if key == name:
                    return value

            return None

        def _base_url(self) -> str:
            """Public origin to build a share link against."""
            if share_base_url:
                return share_base_url.rstrip("/")

            #
            # Behind the server's nginx these carry the public name; on the
            # LAN they are simply the Host we were reached on. Only ever
            # used to render a link for the owner, never to authorise.
            #
            proto = self.headers.get("X-Forwarded-Proto") or "http"
            host = (self.headers.get("X-Forwarded-Host")
                    or self.headers.get("Host") or "localhost")

            return "%s://%s" % (proto, host.split(",")[0].strip())

        def _share_url(self, token: str) -> str:
            return "%s%s/?t=%s" % (self._base_url(), SHARE_PREFIX, token)

        def _serve_share(self, path: str, query: Dict) -> None:
            """The public, token-gated surface. Read-only and live-only."""
            if shares is None:
                self.send_error(404)
                return

            sub_path = path[len(SHARE_PREFIX):] or "/"

            if sub_path != "/" and sub_path.endswith("/"):
                sub_path = sub_path.rstrip("/") or "/"

            token = (query.get("t", [None])[0]) or self._cookie(SHARE_COOKIE)

            if not shares.validate(token):
                #
                # One message for "no token", "wrong token" and "expired":
                # a viewer learns nothing about which it was.
                #
                self._body(
                    "text/html; charset=utf-8",
                    SHARE_DENIED_PAGE.encode(),
                    {"Set-Cookie": "%s=; Path=%s; Max-Age=0" % (SHARE_COOKIE, SHARE_PREFIX)},
                )
                return

            if sub_path not in SHARE_ALLOWED:
                self.send_error(404)
                return

            if sub_path == "/":
                secure = "; Secure" if (
                    self.headers.get("X-Forwarded-Proto") == "https"
                ) else ""
                #
                # The cookie dies with the token it carries, so a viewer's
                # browser stops replaying a link that is already dead.
                #
                cookie = "%s=%s; Path=%s; Max-Age=%d; SameSite=Lax; HttpOnly%s" % (
                    SHARE_COOKIE, token, SHARE_PREFIX,
                    int(shares.remaining(token)) + 1, secure,
                )

                self._body("text/html; charset=utf-8",
                           SHARE_PAGE.encode(), {"Set-Cookie": cookie})
                return

            if sub_path == "/api/meta":
                with tel.lock:
                    payload = json.dumps({
                        "meta": tel.meta, "meta_version": tel.meta_version
                    }).encode()

                self._body("application/json", payload)
                return

            if sub_path == "/api/snapshot":
                self._body("application/json",
                           json.dumps(share_snapshot(tel.get())).encode())
                return

            if sub_path == "/api/stream":
                self._stream(share_snapshot)
                return

            self.send_error(404)

        def _stream(self, shape) -> None:
            """SSE loop. `shape` filters each snapshot before it goes out."""
            self._headers("text/event-stream", {"Connection": "close"})
            self.close_connection = True
            self.end_headers()

            seen = -1

            try:
                while True:
                    seen, snap = tel.wait(seen, timeout=2.0)
                    msg = "data: " + json.dumps(shape(snap)) + "\n\n"
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def do_GET(self):
            raw = self.path
            path = raw.split("?")[0]
            query = urllib.parse.parse_qs(
                raw.split("?")[1] if "?" in raw else ""
            )

            #
            # The share surface is matched FIRST and handled entirely
            # inside _serve_share, so no owner-only endpoint below can be
            # reached with a share token no matter what the path looks
            # like.
            #
            if path == SHARE_PREFIX or path.startswith(SHARE_PREFIX + "/"):
                self._serve_share(path, query)
                return

            if path == "/api/share":
                if shares is None:
                    self._json_body({"enabled": False, "links": []})
                    return

                self._json_body({
                    "enabled": True,
                    "ttl_choices": list(SHARE_TTL_CHOICES),
                    "links": [
                        {
                            "token": e["token"],
                            "url": self._share_url(e["token"]),
                            "created": e["created"],
                            "expires": e["expires"],
                            "label": e["label"],
                            "hits": e["hits"],
                            "last_seen": e["last_seen"],
                        }
                        for e in shares.active()
                    ],
                })
                return

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

            if path == "/api/sync":
                #
                # Same-origin, READ-ONLY view of the sync agent's status.
                #
                # The page used to fetch http://<host>:8091 directly, which
                # only works when the dashboard is opened on the Pi itself.
                # Through the server's reverse proxy that hostname resolves
                # to the proxy, port 8091 is not published there, and the
                # fetch fails - so the chip read "off" while sync was in fact
                # healthy.
                #
                # Only /sync/status is proxied. The agent also exposes
                # unauthenticated pause/resume on 8091; those must NOT become
                # reachable through a public vhost, so they are not forwarded.
                #
                try:
                    with urllib.request.urlopen(
                        "http://127.0.0.1:8091/sync/status", timeout=2.0
                    ) as response:
                        payload = response.read()
                except Exception as exc:
                    payload = json.dumps(
                        {"enabled": False, "state": "unreachable",
                         "last_error": str(exc), "databases": {}}
                    ).encode()

                self._body("application/json", payload)
                return

            if path == "/api/diagnostics":
                #
                # The full car-communication picture for this session:
                # which mappings loaded, what resolution dropped and why,
                # per-request success rates and last errors. Read-only,
                # and not in the share allowlist - it names file paths
                # and ECU addresses.
                #
                self._body("application/json",
                           json.dumps(
                               diag.report() if diag else {"ready": False}
                           ).encode())
                return

            if path == "/api/modes":
                #: The catalogue the mode picker renders from, so the
                #: page never hardcodes a list that can drift from the
                #: runtime's.
                self._body("application/json", json.dumps({
                    "current": modes.current if modes else "normal",
                    "version": modes.version if modes else 0,
                    "modes": [
                        {"name": m.name,
                         "description": m.description,
                         "polls": m.polls,
                         "duty": list(m.duty or ())}
                        for m in (
                            [modes.table.modes[n] for n in modes.table.names()]
                            if modes else []
                        )
                    ],
                }).encode())
                return

            if path == "/api/snapshot":
                self._body("application/json",
                           json.dumps(public_snapshot(tel.get())).encode())
                return

            if path == "/api/stream":
                self._stream(public_snapshot)
                return

            self.send_error(404)

        def do_POST(self):
            """
            Owner-only: mint and revoke share links.

            Unreachable from the share surface - do_GET dispatches /s/
            before anything else and _serve_share serves only its
            allowlist, and there is no POST handling under the prefix at
            all. Through the server this sits behind the vhost's Basic
            Auth; on the LAN it has the same exposure as the rest of the
            dashboard, which has never had a login of its own.
            """
            path = self.path.split("?")[0]

            if path.startswith(SHARE_PREFIX):
                self.send_error(404)
                return

            allowed = {"/api/mode"}

            if shares is not None:
                allowed |= {"/api/share", "/api/share/revoke"}

            if path not in allowed:
                self.send_error(404)
                return

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0

            raw = self.rfile.read(min(length, 4096)) if length > 0 else b"{}"

            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                self._json_body({"error": "bad JSON body"}, 400)
                return

            if not isinstance(body, dict):
                self._json_body({"error": "bad JSON body"}, 400)
                return

            if path == "/api/mode":
                #
                # Changing the poll rate is the one control the dashboard
                # has over the car link, and it is still observational:
                # every mode is a subset of the requests the mappings
                # already declare, and `off` sends nothing at all. No
                # mode can widen the service allowlist.
                #
                if modes is None:
                    self._json_body({"error": "mode control unavailable"}, 503)
                    return

                name = body.get("mode")

                if not isinstance(name, str):
                    self._json_body({"error": "mode must be a string"}, 400)
                    return

                try:
                    modes.request(name)
                except MappingError as exc:
                    self._json_body({"error": str(exc)}, 400)
                    return

                #: `requested`, not `current` - the poll loop applies it on
                #: its next cycle, so the page must not claim it is already
                #: in force.
                self._json_body({"requested": name})
                return

            if path == "/api/share/revoke":
                if body.get("all"):
                    self._json_body({"revoked": shares.revoke_all()})
                    return

                token = body.get("token")

                if not isinstance(token, str) or not token:
                    self._json_body({"error": "token required"}, 400)
                    return

                self._json_body({"revoked": 1 if shares.revoke(token) else 0})
                return

            #
            # Mint. The TTL is clamped to the offered choices rather than
            # trusted, so a hand-rolled POST cannot ask for a link that
            # outlives the session by weeks.
            #
            try:
                ttl = float(body.get("ttl") or SHARE_TTL_CHOICES[1])
            except (TypeError, ValueError):
                self._json_body({"error": "bad ttl"}, 400)
                return

            ttl = max(60.0, min(ttl, float(max(SHARE_TTL_CHOICES))))
            entry = shares.mint(ttl, str(body.get("label") or ""))

            self._json_body({
                "token": entry["token"],
                "url": self._share_url(entry["token"]),
                "expires": entry["expires"],
                "created": entry["created"],
                "label": entry["label"],
            })

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
    ap.add_argument("--redact-vin", action="store_true",
                    help="mask the VIN to its last 4 chars in the HTTP/SSE "
                         "API. Off by default (the deployed dashboard is put "
                         "behind auth instead); use this if you expose the "
                         "dashboard without authentication. Storage is "
                         "unaffected - SQLite and the lake keep the full VIN.")
    ap.add_argument("--no-share", action="store_true",
                    help="disable temporary share links entirely (the /s/ "
                         "prefix then 404s and no link can be minted)")
    ap.add_argument("--share-base-url", default="",
                    help="public origin used to build share links, e.g. "
                         "https://f10.example.com. Defaults to the Host the "
                         "request arrived on, which is right behind the "
                         "server's reverse proxy.")
    ap.add_argument("--ecu", type=lambda s: int(s, 0), default=None,
                    help="ECU diagnostic address, e.g. 0x12")
    ap.add_argument("--rate", type=float, default=10.0,
                    help="target poll rate in Hz (default 10)")
    ap.add_argument("--mode", default=None,
                    help="drive mode - how hard to poll. Names come from "
                         "the mode table (--modes); the shipped set is "
                         "off / sampling / long / normal / debug. Defaults "
                         "to the table's own `default`. Switchable at "
                         "runtime from the dashboard.")
    ap.add_argument("--modes", default=DEFAULT_MODE_CONFIG,
                    help="drive-mode table (default: config/modes.yaml)")
    ap.add_argument("--wait-for-clock", type=float, default=20.0,
                    help="seconds to wait at startup for NTP to discipline "
                         "the clock before recording (default 20). This "
                         "host has no RTC, so a run started too early "
                         "carries wrong timestamps. 0 disables the wait; "
                         "the run is still labelled either way.")
    ap.add_argument("--demo", action="store_true",
                    help="simulated data, no vehicle needed")

    ap.add_argument("--tank", type=float, default=70.0,
                    help="tank capacity in litres for fuel-remaining (default 70)")
    ap.add_argument("--mappings", default=DEFAULT_MAPPING_DIR,
                    help="directory of diagnostic mapping files "
                         "(default: mappings/ next to live.py)")
    ap.add_argument("--extra-mappings", action="append", default=[],
                    metavar="DIR",
                    help="additional mapping tree(s) to load, including "
                         "verified-but-non-production files (e.g. the "
                         "F-series N47 dynamic channels under "
                         "mappings/candidates/bmw/dde/n47). Each activates "
                         "only on an ECU that answers its variant probe. "
                         "Repeatable.")
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

    global REDACT_VIN
    REDACT_VIN = args.redact_vin

    #
    # Load the mappings before anything else: a broken mapping file is a
    # startup error with a readable message, not something to rediscover
    # once a minute inside the poll loop's reconnect cycle.
    #
    try:
        registry = load_registry(args.mappings)
        #: Which mappings are here only because --extra-mappings named
        #: them. That flag is the repo's "no proprietary data in the
        #: production set" line, and the diagnostics view shows per run
        #: which side of it every loaded file sits on.
        base_ids = {m.id for m in registry.mappings}

        if args.extra_mappings:
            load_extra(registry, args.extra_mappings)

        extra_ids = {m.id for m in registry.mappings} - base_ids
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

    #
    # The drive-mode table is data too (config/modes.yaml).
    #
    try:
        mode_table = load_modes(args.modes)
        modes = ModeControl(mode_table, args.mode)
    except MappingError as exc:
        print(f"[!] drive mode error: {exc}", file=sys.stderr)
        return 2

    #
    # A multiplier naming a class no loaded mapping declares does
    # nothing. Usually that is correct - a bare launch has no `dde_dyn`
    # or `egs` because those mappings are not loaded - so this warns
    # rather than refusing to start. It is still worth saying out loud:
    # the same silence is what a typo produces.
    #
    dead = mode_table.unknown_classes(
        c.name for c in registry.polling_classes()
    )

    if dead:
        for name, classes in sorted(dead.items()):
            print(f"[~] mode {name}: no loaded mapping declares "
                  f"{', '.join(classes)} - those multipliers do nothing")

    #
    # Give NTP a bounded chance before anything is recorded. The Pi has
    # no RTC; without this, a boot on a stale clock records against it
    # and a correction lands mid-drive - see the clock section above.
    #
    if not args.no_db:
        wait_for_clock(args.wait_for_clock)

    tel = Telemetry()

    rec: Optional[Recorder] = None

    if not args.no_db:
        rec = Recorder(args.db, chunk=args.db_chunk, interval=args.db_flush)
        rec.open()

    diag = Diagnostics()
    #: Published before the worker starts, so the panel can answer "what
    #: would this poll?" from the moment the process is up - with the car
    #: absent, and before the first connection attempt.
    diag.publish(registry=registry, extra_ids=extra_ids)

    worker = threading.Thread(
        target=demo_loop if args.demo else poll_loop,
        args=(tel, args, rec, registry, modes, diag),
        daemon=True,
    )
    worker.start()

    shares = None if args.no_share else ShareTokens()

    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(
            tel,
            None if args.no_db else args.db,
            shares,
            args.share_base_url,
            modes,
            diag,
        ),
    )
    server.daemon_threads = True

    shown = "localhost" if args.host in ("0.0.0.0", "") else args.host

    print("=" * 60)
    print("BMW F10 live telemetry")
    print("=" * 60)
    print(f"[+] dashboard: http://{shown}:{args.port}/")
    print(
        "[+] sharing:   "
        + ("disabled (--no-share)" if shares is None else
           "on - 'share' chip mints a temporary read-only link at "
           f"{args.share_base_url or '<this host>'}{SHARE_PREFIX}/")
    )
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
