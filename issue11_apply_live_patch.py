#!/usr/bin/env python3
"""
ONE-SHOT: apply the issue #11 edits to live.py, then delete this file.

    python3 issue11_apply_live_patch.py && rm issue11_apply_live_patch.py

Every substitution asserts exactly one match, so a live.py that has moved
on fails loudly instead of being half-patched. Run from the repo root.
"""

import pathlib

P = pathlib.Path(__file__).resolve().parent / "live.py"


def sub(old, new, count=1):
    s = P.read_text()
    n = s.count(old)
    assert n == count, f"live.py: expected {count} of {old[:70]!r}, found {n}"
    P.write_text(s.replace(old, new))


# ---- imports -------------------------------------------------------------
sub('''from bmwdiag.mapping import (
    fault_kind,
    MappingError,''',
'''from bmwdiag.mapping import (
    fault_detail,
    fault_kind,
    MappingError,''')
sub('''from bmwdiag.mapping.model import PollingClassDef
''',
'''from bmwdiag.mapping.model import PollingClassDef
from bmwdiag.protocol.errors import (
    DiagnosticError,
    LinkError,
    NegativeResponse,
    RequestTimeout,
    ResponseMismatch,
    RoutingNack,
)
''')

# ---- HsfzClient ------------------------------------------------------------
sub('''class HsfzError(Exception):
    pass


class HsfzNack(HsfzError):
    """Gateway refused to route to that address - nobody home."""
''',
'''class HsfzError(DiagnosticError):
    """
    An application-level diagnostic failure with no finer category:
    discovery found no ECU, a forced address did not answer. The
    classified failures - a dead link, a routing refusal, a timeout, a
    negative response, a malformed reply - are the `bmwdiag.protocol`
    types, and every policy decision keys on those, never on this.
    """


def _link_reason(exc: OSError) -> str:
    """Why a socket failed, as a stable label rather than an errno."""
    if isinstance(exc, ConnectionResetError):
        return "reset"

    if isinstance(exc, BrokenPipeError):
        return "broken_pipe"

    if isinstance(exc, ConnectionRefusedError):
        return "refused"

    return "socket"
''')

sub('''        try:
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
''',
'''        try:
            return self.request(data, timeout, dst)
        except LinkError:
            #
            # Only the LINK category reconnects. A timeout, a routing
            # refusal or a negative response is one exchange's outcome and
            # propagates as itself - reconnecting would not change it.
            #
            self.reconnect()

            return self.request(data, timeout, dst)
''')

sub('''        if not chunk:
            raise HsfzError("gateway closed the connection")
''',
'''        if not chunk:
            raise LinkError("gateway closed the connection", reason="closed")
''')
sub('''                if length > 0x00100000:
                    raise HsfzError(f"absurd HSFZ length {length}")
''',
'''                if length > 0x00100000:
                    #
                    # The stream is desynchronised; nothing after this
                    # byte can be trusted, so it is a link fault.
                    #
                    raise LinkError(
                        f"absurd HSFZ length {length}", reason="framing"
                    )
''')

sub('''        """Send a UDS/OBD payload, return the ECU's response bytes."""
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
''',
'''        """
        Send a UDS/OBD payload, return the ECU's response bytes.

        Fails with exactly one of the `bmwdiag.protocol` categories:
        `LinkError` (socket gone), `RoutingNack` (gateway refused the
        target), `RequestTimeout` (no answer in time), `NegativeResponse`
        (the ECU said `7F <sid> <nrc>`). Socket-level failures are
        wrapped here, once, so no caller ever needs to know what an
        `OSError` from `recv` means.
        """
        self._gate(bytes(data))

        if self.sock is None:
            raise LinkError("not connected", reason="not_connected")

        target = self.dst if dst is None else dst
        budget = timeout or self.timeout

        try:
            return self._exchange(bytes(data), target, expect_src, budget)
        except DiagnosticError:
            raise
        except TIMEOUTS as exc:
            raise RequestTimeout(target, budget) from exc
        except OSError as exc:
            raise LinkError(str(exc) or type(exc).__name__,
                            reason=_link_reason(exc)) from exc

    def _exchange(
        self,
        data: bytes,
        target: int,
        expect_src: Optional[int],
        budget: float,
    ) -> bytes:
        want_sid = data[0] + 0x40
        want_src = target if expect_src is None else expect_src

        #
        # Discard anything still queued from an earlier exchange, so a
        # straggler cannot be mistaken for this request's answer.
        #
        self._drain()

        self._send(HSFZ_DIAG_REQ, bytes([self.src, target]) + data)

        deadline = time.monotonic() + budget

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
                raise RoutingNack(target, control)
''')

sub('''                raise HsfzError(
                    f"negative response to 0x{body[1]:02X}: NRC 0x{nrc:02X}"
                )

            return body
''',
'''                raise NegativeResponse(body[1], nrc, body, target)

            return body
''')

sub('''        self._gate(bytes(data))

        if self.sock is None:
            raise HsfzError("not connected")

        self._send(HSFZ_DIAG_REQ, bytes([self.src, dst]) + data)
''',
'''        self._gate(bytes(data))

        if self.sock is None:
            raise LinkError("not connected", reason="not_connected")

        self._send(HSFZ_DIAG_REQ, bytes([self.src, dst]) + data)
''')
sub('''            try:
                control, payload = self._read_frame(deadline)
            except TIMEOUTS:
                break
            except HsfzError:
                break
''',
'''            try:
                control, payload = self._read_frame(deadline)
            except TIMEOUTS:
                break
            except DiagnosticError:
                break
''')

# ---- ObdSession ----------------------------------------------------------
sub('''        resp = self.client.request(bytes([0x01] + pids), timeout)

        if not resp or resp[0] != 0x41:
            raise HsfzError(f"unexpected reply {resp.hex(' ')}")
''',
'''        resp = self.client.request(bytes([0x01] + pids), timeout)

        if not resp or resp[0] != 0x41:
            raise ResponseMismatch(
                f"unexpected reply {resp.hex(' ')}", raw=resp, expected="41",
            )
''')
sub('''                    if not all(p in got for p in batch):
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
''',
'''                    if not all(p in got for p in batch):
                        raise ResponseMismatch(
                            "incomplete multi-PID response",
                            expected=" ".join(f"{p:02X}" for p in batch),
                        )

                    result.update(got)

                return result
            except LinkError:
                #
                # The socket is gone. Not a batching problem, and not
                # something the per-PID fallback below could absorb
                # without polling a dead link for three strikes per PID.
                #
                raise
            except DiagnosticError:
                self.multi_ok = False
                result.clear()

        for pid in pids:
            if pid in self.dead:
                continue

            try:
                result.update(self._mode01([pid]))
                self.fails.pop(pid, None)
            except LinkError:
                raise
            except (DiagnosticError,) + TIMEOUTS:
''')

# ---- discovery excepts -----------------------------------------------------
sub('''        resp = client.request(bytes([0x09, 0x0A]), timeout=timeout, dst=addr)
    except (HsfzError,) + TIMEOUTS:
        return None
''',
'''        resp = client.request(bytes([0x09, 0x0A]), timeout=timeout, dst=addr)
    except (DiagnosticError,) + TIMEOUTS:
        return None
''')
sub('''        resp = client.request_safe(bytes([0x01, 0x00]), timeout=timeout, dst=addr)
    except HsfzNack:
        return None
    except (HsfzError,) + TIMEOUTS:
        return None
''',
'''        resp = client.request_safe(bytes([0x01, 0x00]), timeout=timeout, dst=addr)
    except RoutingNack:
        return None
    except (DiagnosticError,) + TIMEOUTS:
        return None
''')
sub('''            answers = client.collect(bytes([0x01, 0x00]), fn, window=args.scan_timeout * 3)
        except (HsfzError,) + TIMEOUTS:
            continue
''',
'''            answers = client.collect(bytes([0x01, 0x00]), fn, window=args.scan_timeout * 3)
        except (DiagnosticError,) + TIMEOUTS:
            continue
''')

# ---- main loop: the reset hint, by category ---------------------------------
sub('''            msg = f"{type(exc).__name__}: {exc}"

            if isinstance(exc, ConnectionResetError):
''',
'''            msg = f"{type(exc).__name__}: {exc}"

            if isinstance(exc, ConnectionResetError) or (
                isinstance(exc, LinkError) and exc.reason == "reset"
            ):
''')

# ---- Recorder: structured detail beside kind + message ---------------------
sub('''CREATE TABLE IF NOT EXISTS errors (
    run_id     INTEGER NOT NULL,
    ts         REAL NOT NULL,
    request_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    message    TEXT
);
''',
'''CREATE TABLE IF NOT EXISTS errors (
    run_id     INTEGER NOT NULL,
    ts         REAL NOT NULL,
    request_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    message    TEXT,
    -- The structured half of the fault, as a JSON object: the NRC as a
    -- number, the service byte, the target, why a link died. `kind` is
    -- what to GROUP BY; this is what to read once grouped. NULL on rows
    -- recorded before it existed; '{}' when the fault carried nothing.
    detail     TEXT
);
''')
sub('''        if "session_uid" not in cols("runs"):
            self.db.execute("ALTER TABLE runs ADD COLUMN session_uid TEXT")
''',
'''        if "session_uid" not in cols("runs"):
            self.db.execute("ALTER TABLE runs ADD COLUMN session_uid TEXT")

        if "detail" not in cols("errors"):
            self.db.execute("ALTER TABLE errors ADD COLUMN detail TEXT")
''')
sub('''    def error(self, request_id: str, kind: str, message: str) -> None:
        """Record one per-request fault. Dropped silently if the queue is
        full - a fault storm must never stall the poll loop."""
        try:
            self.q.put_nowait(
                ("error", (time.time(), request_id, kind, message[:500]))
            )
        except queue.Full:
            pass
''',
'''    def error(
        self,
        request_id: str,
        kind: str,
        message: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record one per-request fault. Dropped silently if the queue is
        full - a fault storm must never stall the poll loop."""
        try:
            self.q.put_nowait((
                "error",
                (time.time(), request_id, kind, message[:500],
                 encode_fault_detail(detail)),
            ))
        except queue.Full:
            pass
''')
sub('''            elif kind == "error" and self.run_id is not None:
                ts, request_id, err_kind, message = payload
                self.db.execute(
                    "INSERT INTO errors(run_id, ts, request_id, kind, message) "
                    "VALUES (?,?,?,?,?)",
                    (self.run_id, ts, request_id, err_kind, message),
                )
''',
'''            elif kind == "error" and self.run_id is not None:
                ts, request_id, err_kind, message, detail = payload
                self.db.execute(
                    "INSERT INTO errors"
                    "(run_id, ts, request_id, kind, message, detail) "
                    "VALUES (?,?,?,?,?,?)",
                    (self.run_id, ts, request_id, err_kind, message, detail),
                )
''')

sub('''class Recorder:
    """
    Buffers samples in memory and flushes them to SQLite in chunks, on a''',
'''#: Upper bound on one stored detail. A raw response is the only field
#: that can grow, and a fault storm must not be able to bloat the database
#: through it any more than through the message.
FAULT_DETAIL_LIMIT = 1000


def encode_fault_detail(detail: Optional[Dict[str, Any]]) -> str:
    """
    The structured half of a fault as compact, sorted JSON.

    Sorted keys so identical faults encode identically; a bounded size so
    a garbage response cannot become a kilobyte per row. The bound drops
    the field rather than truncating the JSON, which would leave a row
    that no longer parses.
    """
    if not detail:
        return "{}"

    text = json.dumps(detail, sort_keys=True, separators=(",", ":"),
                      default=str)

    if len(text) <= FAULT_DETAIL_LIMIT:
        return text

    slim = {k: v for k, v in detail.items() if k != "raw"}
    text = json.dumps(slim, sort_keys=True, separators=(",", ":"),
                      default=str)

    return text if len(text) <= FAULT_DETAIL_LIMIT else "{}"


class Recorder:
    """
    Buffers samples in memory and flushes them to SQLite in chunks, on a''')

sub('''            def note_fault(request_id: str, exc: Exception) -> None:
                if rec is not None:
                    rec.error(request_id, fault_kind(exc), str(exc))
''',
'''            def note_fault(request_id: str, exc: Exception) -> None:
                if rec is not None:
                    rec.error(
                        request_id, fault_kind(exc), str(exc), fault_detail(exc)
                    )
''')

sub('''                "last_error": st.get("last_error"),
                "last_error_age": (
                    None if not st.get("last_error_at")
                    else round(now - st["last_error_at"], 1)
                ),
            })
''',
'''                "last_error": st.get("last_error"),
                "last_error_age": (
                    None if not st.get("last_error_at")
                    else round(now - st["last_error_at"], 1)
                ),
                #: The structured half of that fault: `{"nrc": 49,
                #: "nrc_hex": "0x31", "service": 34, ...}` for a negative
                #: response, `{"target": 24}` for a routing refusal,
                #: `{"reason": "reset"}` for a dead link. Empty when the
                #: fault carried nothing structured.
                "last_error_detail": st.get("last_detail") or {},
            })
''')

print("live.py patched for issue #11 - now delete this script")
