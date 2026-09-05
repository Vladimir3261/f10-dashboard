"""
Request execution.

    request definitions -> payloads -> transport -> prefix match -> decode
                                    -> normalised {signal key: value}

Two dispatch paths exist, and only because standard OBD really does behave
differently on the wire:

  * `obd` requests are handed to an `ObdPidReader`, which batches PIDs and
    retires ones the ECU ignores. The reader returns data bytes per PID;
    this module rebuilds the logical `41 <pid> <data...>` response so that
    prefix matching and offsets work the same way they do for every other
    protocol.

  * everything else goes one request at a time through a
    `DiagnosticTransport`.

Adding a protocol means adding a branch here, not touching the decoder.
"""

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..protocol.request import (
    DecodedResponse,
    DiagnosticRequest,
    NegativeResponse,
    build_request,
)
from .decoder import read_response
from .errors import DecodeError, MappingError
from .model import RequestDef
from .registry import ResolvedProfile

__all__ = ["MappingExecutor", "obd_logical_response"]


def obd_logical_response(request: RequestDef, data: bytes) -> bytes:
    """
    Rebuild the full Mode 01 response for one PID.

    The OBD session hands back only the data bytes because that is what
    walking a multi-PID reply produces. Putting the `41 <pid>` echo back
    in front means mapping files describe a real response rather than a
    session-specific fragment.
    """
    prefix = bytes(request.response.prefix)

    if prefix and data[: len(prefix)] == prefix:
        return bytes(data)

    return prefix + bytes(data)


#: How many consecutive per-request transport faults to absorb before
#: concluding the link itself is gone and letting the error propagate to the
#: reconnect logic. Sized so a couple of unreachable ECUs (an EGS that will
#: not route, a slow DDE) never cost a reconnect, while a dead socket is
#: noticed within one polling cycle - failing requests return immediately.
TRANSPORT_FAULT_BUDGET = 6

#: Consecutive faults against ONE request before it is rested rather than
#: retried every time it comes due. Absorbing a fault is cheap; absorbing
#: the same fault forever is not, because a request that will never answer
#: still costs its full timeout on every turn. Mirrors the three-strikes
#: rule ObdSession already applies to PIDs an ECU ignores.
REQUEST_FAULT_LIMIT = 3

#: How long a rested request sits out, and the ceiling the rest doubles
#: towards. **Wall-clock seconds, deliberately - not turns.**
#:
#: A turn is not a unit of anything: the same count spans 3s on `motion`
#: (0.1s/turn) and 32 minutes on `rare` (60s/turn) - a 640x spread from one
#: constant. And the cost being suppressed IS wall-clock: a 0.4s timeout on
#: a 2 Hz channel is a 40% tax on the poll loop, while the same timeout on
#: a 60s channel is 0.7% and not worth suppressing at all. Counting turns
#: makes the constant mean the opposite of the intent at each end of the
#: range.
#:
#: In seconds it scales itself: `egs` sits out many turns, `rare` at most
#: one. It always comes back - an ECU that was briefly asleep, or a gateway
#: mid-reconfiguration, must not be written off for the rest of the drive.
REQUEST_REST_SECONDS = 5.0
REQUEST_REST_MAX_SECONDS = 60.0


class NoResponse(Exception):
    """
    A PID the OBD reader asked for and did not get back.

    Not raised by anything - `ObdSession` absorbs these under its own
    three-strikes policy and simply omits the PID from the reply. It
    exists so the no-response case can travel down the same `on_error`
    path as a real transport fault, rather than being counted in one
    place and reported in another.
    """


def fault_kind(exc: Exception) -> str:
    """
    A stable, structured name for what went wrong.

    Returned so callers can record and aggregate faults without parsing
    exception messages - a message is prose that changes; a kind is data you
    can group by. Used to attribute errors per request in the lake, which is
    what makes "this channel fails 8% of the time" answerable at all.
    """
    if isinstance(exc, (DecodeError, MappingError)):
        return "decode"

    if isinstance(exc, (ConnectionError, BrokenPipeError)):
        return "transport_link"

    if isinstance(exc, TimeoutError):
        return "transport_timeout"

    if isinstance(exc, NoResponse):
        return "no_response"

    if isinstance(exc, NegativeResponse):
        #: The ECU answered and refused. Until 2026-09-05 this was "other",
        #: which hid the one fault kind that says "the ECU is there, it
        #: just does not do this" behind the same label as a bug.
        return "negative_response"

    if exc.__class__.__name__.endswith("Nack"):
        return "transport_nack"

    return "other"


def _is_request_fault(exc: Exception) -> bool:
    """
    Did ONE exchange fail, or has the link died?

    Skipping a request is only safe while the link is still good; otherwise
    every later request fails identically and the reconnect never happens.

    Classified structurally rather than by importing the transport's own
    exceptions: `bmwdiag` deliberately knows nothing about HSFZ, imports
    nothing outside the standard library and opens no sockets, so it cannot
    reference `HsfzNack` by type. Anything unrecognised counts as a link
    fault, which is the conservative direction - a needless reconnect costs
    a few seconds, whereas mistaking a dead link for a slow ECU means polling
    a closed socket forever.
    """
    if isinstance(exc, (ConnectionError, BrokenPipeError)):
        return False                    # socket gone: let it reconnect

    if isinstance(exc, TimeoutError):
        return True                     # this ECU did not answer in time

    if isinstance(exc, NegativeResponse):
        # The ECU itself answered `7F`: the link carried the request there
        # and the reply back. It refused this one service or identifier,
        # which says nothing about the next request. Skip and record it.
        return True

    # A negative acknowledgement: the gateway is alive and refused to route
    # to one target (e.g. "gateway will not route to 0x18" for the EGS).
    return exc.__class__.__name__.endswith("Nack")


def _usable(readings: Dict[str, Any]) -> Dict[str, Any]:
    """The measurement subset of a reading map, as plain values."""
    return {
        key: reading.value
        for key, reading in readings.items()
        if reading.usable
    }


class MappingExecutor:
    """
    Runs a set of due requests and returns normalised signal values.

    Decode failures are swallowed per request: a garbled reply costs one
    channel for one cycle.

    Transport failures are judged rather than blindly escalated, because
    the engine polls more than one ECU and a fault against one says nothing
    about the link carrying the rest. One failed exchange is skipped;
    enough failures in a row mean the link, not the ECU, and the error is
    re-raised for the application's reconnect logic. A request that keeps
    failing is then rested for a while, so an ECU that is simply absent
    stops costing its full timeout every time it comes due.

    All of it is decided from behaviour, never from an address: the engine
    has no notion of a "primary" ECU.

    **The OBD path is deliberately exempt.** `ObdSession` already retires
    PIDs an ECU ignores, after its own three strikes, because standard OBD
    batches several PIDs into one exchange and the retry policy has to live
    where that batching is understood. Duplicating it here would mean two
    layers backing off against each other for the same silence.
    """

    def __init__(
        self,
        profile: ResolvedProfile,
        transport: Any = None,
        obd_reader: Any = None,
        targets: Optional[Dict[str, int]] = None,
        on_error: Optional[Any] = None,
    ):
        self.profile = profile
        self.transport = transport
        self.obd_reader = obd_reader
        self.targets = dict(targets or profile.targets)
        self.on_error = on_error
        self.last_responses: Dict[str, bytes] = {}
        #
        # The setup sequence currently armed on each destination. A single
        # request polled repeatedly arms once; several requests that share
        # one dynamic DID (the F303 pattern) re-arm as they take turns, so
        # a define is always the one matching the poll that follows it. An
        # executor lives for one connection, so a reconnect re-arms
        # everything automatically.
        #
        self._armed: Dict[int, tuple] = {}
        #
        # Per-request health, so "which channels are actually answering?"
        # is a lookup rather than an inference from missing rows. A
        # request that has never succeeded and one nobody asked for look
        # identical in the sample table; here they do not.
        #
        self._stats: Dict[str, Dict[str, Any]] = {}
        #: channel key -> {quality label: count}. Signal-level, unlike
        #: _stats which is request-level; see _record_quality().
        self._quality: Dict[str, Dict[str, int]] = {}
        #
        # Consecutive per-request transport faults. One ECU that is slow or
        # absent must not tear down a link that is otherwise fine, but a link
        # that has genuinely died has to reach the reconnect logic - and it
        # looks the same from a single request. So faults are tolerated per
        # request and counted; enough of them in a row means the link, not
        # the ECU, and the error is re-raised. Any success resets it.
        #
        self._transport_faults = 0
        #
        # Per-request fault history, and how long each is currently sitting
        # out. Per-connection state like everything else here: the
        # application builds a fresh executor after a reconnect, which
        # clears every count and every rest.
        #
        self._request_faults: Dict[str, int] = {}
        self._rest_len: Dict[str, float] = {}
        self._rested_until: Dict[str, float] = {}
        #
        # The fault count that triggered the current rest. Kept separately
        # because the live count is zeroed when a rest starts, and the
        # diagnostics view still needs to say what the rest was FOR.
        #
        self._rested_after: Dict[str, int] = {}

    # -- helpers ----------------------------------------------------

    def _stat(self, request_id: str) -> Dict[str, Any]:
        return self._stats.setdefault(request_id, {
            "sent": 0, "ok": 0, "failed": 0,
            "kinds": {}, "last_ok": None, "last_error": None,
            "last_error_at": None,
            #: Answers that arrived after this request had already been
            #: given up on, and were discarded by the transport. NOT a
            #: failure - the timeout was already counted as one - and
            #: not an `ok`: the value never reached the decoder. A
            #: channel with many of these has a timeout that is too
            #: short for the ECU, which is a different fix from one
            #: that never answers at all.
            "late": 0,
        })

    def note_late_response(self, request_id: str, message: str = "") -> None:
        """
        A transport discarded a late answer attributed to `request_id`.

        The transport cannot see request ids; the application bridges
        its orphan report to this. Only counted against requests this
        executor knows - a late answer to an ad-hoc probe is the
        transport's business, not a channel's.
        """
        if request_id in self._stats:
            self._stats[request_id]["late"] += 1

    def _rest_fields(self, request_id: str) -> Dict[str, Any]:
        """
        Why a request is quiet, for the diagnostics view.

        Folded into `stats()` rather than exposed separately: that view is
        already per-request and already what the Car link tab consumes, and
        two overlapping introspection APIs on one object is a trap.
        """
        left = self._rest_left(request_id)

        return {
            "resting_for": round(left, 1) if left else 0.0,
            #
            # While resting, report the count that caused it - the live
            # count is zero by then, and "resting, 40s left after 3
            # timeouts" is the sentence the view needs to be able to write.
            #
            "consecutive_faults": (
                self._rested_after.get(request_id, 0) if left
                else self._request_faults.get(request_id, 0)
            ),
        }

    def stats(self) -> Dict[str, Dict[str, Any]]:
        """
        Per-request counters, for the diagnostics view. Copied, not shared.

        Read from the HTTP thread while the poll loop writes. Value
        updates are safe under the GIL; only INSERTING a new request id
        can resize the dict mid-iteration, and the set of ids is fixed
        after the first cycle or two. So retry rather than lock - a lock
        here would sit on the hot path for a race that closes on its own
        within a second of startup.
        """
        for _ in range(3):
            try:
                return {
                    rid: {
                        **st,
                        "kinds": dict(st["kinds"]),
                        **self._rest_fields(rid),
                    }
                    for rid, st in self._stats.items()
                }
            except RuntimeError:                # changed size during iteration
                continue

        return {}

    def _record_quality(self, readings: Dict[str, Any]) -> None:
        """
        Count how each signal's quality came out, per channel.

        Request-level counters answer "did the exchange work". They cannot
        answer "did anything usable come back", because a positive
        response can still decode to a sentinel or sit on a sensor's rail.
        Those are different questions and the diagnostics view needs both:
        a channel at 100% request success and 100% sentinel is broken in a
        way no request counter can show.
        """
        for key, reading in readings.items():
            counts = self._quality.setdefault(key, {})
            counts[reading.quality] = counts.get(reading.quality, 0) + 1

    def quality_stats(self) -> Dict[str, Dict[str, int]]:
        """Per-channel quality counters, for the diagnostics view. Copied."""
        for _ in range(3):
            try:
                return {
                    key: dict(counts)
                    for key, counts in self._quality.items()
                }
            except RuntimeError:                # changed size during iteration
                continue

        return {}

    def _record_sent(self, request_id: str) -> None:
        self._stat(request_id)["sent"] += 1

    def _record_ok(self, request_id: str, when: float) -> None:
        stat = self._stat(request_id)
        stat["ok"] += 1
        stat["last_ok"] = when

    def _record_fault(self, request_id: str, kind: str, message: str,
                      exc: Optional[Exception] = None) -> None:
        """
        Count a fault AND report it. Both, always.

        These used to be separate: the OBD path incremented the counters
        directly and never called `on_error`, so `/api/diagnostics` said
        six failures while `telemetry.channel_errors` held three. Two
        views of the same drive disagreeing about how many faults it had
        is worse than either number alone - and the table is the one
        analysis queries, so it was the under-reporting one.
        """
        stat = self._stat(request_id)
        stat["failed"] += 1
        stat["kinds"][kind] = stat["kinds"].get(kind, 0) + 1
        stat["last_error"] = f"{kind}: {message}"
        stat["last_error_at"] = time.time()

        if self.on_error is not None:
            #: A PID the reader simply dropped has no exception of its
            #: own; synthesise one so the recorder's contract - which
            #: takes an exception - holds for every path.
            self.on_error(request_id, exc if exc is not None else NoResponse(message))

    def _note(self, request_id: str, exc: Exception) -> None:
        self._record_fault(request_id, fault_kind(exc), str(exc), exc)

    def _rest_left(self, request_id: str) -> float:
        """
        Seconds of rest still owed by this request; 0 if it is due.

        **Monotonic, not wall time.** A rest is a DURATION, and this host
        has no RTC: its clock is corrected forward at boot and can step
        backwards on an NTP overshoot or a fake-hwclock save from a fast
        clock. Against `time.time()` a backward step of 30 minutes turns a
        5-second rest into a 30-minute one and strands the channel.
        `PollingPlan` already schedules on `time.monotonic()` for the same
        reason.

        Note the deliberate split with `last_ok` / `last_error_at` a few
        lines up, which stay on `time.time()`: those are TIMESTAMPS for
        display, and `/api/diagnostics` ages them against wall time. So -
        durations monotonic, timestamps wall. Do not "fix" the
        inconsistency by making them match.
        """
        until = self._rested_until.get(request_id)

        if until is None:
            return 0.0

        return max(0.0, until - time.monotonic())

    def _rest_request(self, request_id: str) -> None:
        """Stand a repeatedly-failing request down, for longer each time."""
        rest = min(
            max(REQUEST_REST_SECONDS, self._rest_len.get(request_id, 0.0) * 2),
            REQUEST_REST_MAX_SECONDS,
        )
        self._rest_len[request_id] = rest
        self._rested_until[request_id] = time.monotonic() + rest
        self._rested_after[request_id] = self._request_faults.get(request_id, 0)
        #
        # Start counting again from zero so the request gets a genuine
        # retry when its rest ends, rather than being stood down again on
        # the very next fault.
        #
        self._request_faults[request_id] = 0

    def _request_recovered(self, request_id: str) -> None:
        self._request_faults.pop(request_id, None)
        self._rest_len.pop(request_id, None)
        self._rested_until.pop(request_id, None)
        self._rested_after.pop(request_id, None)

    def bind(self, request: RequestDef) -> DiagnosticRequest:
        return build_request(request, self.targets)

    # -- execution --------------------------------------------------

    def execute(
        self, requests: Sequence[RequestDef]
    ) -> Dict[str, Any]:
        """
        Run every request and merge the usable decoded signals.

        Unchanged: only measurements come back, and a reading the ECU
        flagged is simply absent. `execute_readings` is the view that
        keeps it, for callers that can record why.
        """
        out: Dict[str, Any] = {}

        for decoded in self.execute_detailed(requests):
            out.update(decoded.values)

        return out

    def execute_readings(
        self, requests: Sequence[RequestDef]
    ) -> Dict[str, Any]:
        """Run every request and merge the signals as key -> Reading."""
        return self.execute_readings_at(requests)[0]

    def execute_readings_at(
        self, requests: Sequence[RequestDef]
    ) -> Tuple[Dict[str, Any], Dict[str, float]]:
        """
        As `execute_readings`, plus when each signal was actually read.

        Requests in one cycle are executed SEQUENTIALLY. Stamping them
        all with one cycle timestamp would make a paired actual/setpoint
        report a gap of exactly zero however far apart the two exchanges
        really were - which is measuring the recorder, not the car.
        """
        readings: Dict[str, Any] = {}
        stamps: Dict[str, float] = {}

        for decoded in self.execute_detailed(requests):
            readings.update(decoded.readings)

            for key in decoded.readings:
                stamps[key] = decoded.at

        return readings, stamps

    def execute_detailed(
        self, requests: Sequence[RequestDef]
    ) -> List[DecodedResponse]:
        """As `execute`, but keeps the raw bytes alongside each result."""
        obd = [r for r in requests if r.protocol == "obd" and r.payload is None]
        other = [r for r in requests if r not in obd]

        results: List[DecodedResponse] = []
        results.extend(self._run_obd(obd))
        results.extend(self._run_generic(other))

        return results

    def _run_obd(self, requests: Sequence[RequestDef]) -> List[DecodedResponse]:
        if not requests:
            return []

        if self.obd_reader is None:
            raise MappingError("no OBD reader configured for obd requests")

        by_pid: Dict[int, RequestDef] = {}
        pids: List[int] = []

        for request in requests:
            if request.pid is None:
                continue

            #
            # One request per PID, so a PID never goes on the wire twice
            # even if two mappings both want a signal out of it.
            #
            if request.pid not in by_pid:
                by_pid[request.pid] = request
                pids.append(request.pid)
                self._record_sent(request.id)

        got = self.obd_reader.read(pids)
        out: List[DecodedResponse] = []

        #
        # A PID the reader dropped is not an exception - the session
        # retires PIDs the ECU ignores - so it would otherwise leave no
        # trace at all. Count it: "asked 400 times, answered 0" is
        # exactly what identifies a channel the car does not really have.
        #
        for pid, request in by_pid.items():
            if pid not in got:
                self._record_fault(
                    request.id, "no_response",
                    "the ECU did not return this PID",
                )

        for pid, data in got.items():
            request = by_pid.get(pid)

            if request is None:
                continue

            response = obd_logical_response(request, data)
            self.last_responses[request.id] = response

            try:
                readings = read_response(request, response)
            except (DecodeError, MappingError) as exc:
                self._note(request.id, exc)
                continue
            except Exception as exc:            # defensive: never kill the loop
                self._note(request.id, exc)
                continue

            completed = time.time()
            self._record_ok(request.id, completed)
            self._record_quality(readings)
            out.append(DecodedResponse(
                request.id, response, _usable(readings), readings, completed,
            ))

        return out

    def _run_generic(self, requests: Sequence[RequestDef]) -> List[DecodedResponse]:
        if not requests:
            return []

        if self.transport is None:
            raise MappingError("no diagnostic transport configured")

        out: List[DecodedResponse] = []

        for request in requests:
            #
            # A request that has failed repeatedly sits out for a while.
            # Checked before bind() AND before _record_sent: resting is not
            # the same as failing, so it must not land in the diagnostics
            # view as asked-and-unanswered and collapse the success rate.
            #
            # Known property: a resting member of a staggered (round-robin)
            # class still consumes its slot, so that firing does nothing and
            # the other members' effective rate drops slightly. With 22 DDE
            # members and one or two resting it is noise. It would only be
            # worth handling if a whole ECU's worth of a staggered class
            # rested at once.
            #
            if self._rest_left(request.id) > 0:
                continue

            bound = self.bind(request)
            self._record_sent(request.id)

            #
            # Setup frames (e.g. the 2C clear+define of a dynamic DID) go
            # out in declared order immediately before the poll, but only
            # when the currently-armed define is not already this
            # request's - so a repeatedly-polled channel arms once, while
            # channels sharing one dynamic DID re-arm each time they take
            # a turn.
            #
            # Transport faults here are handled with the poll below: one
            # unreachable ECU is skipped, a dead link still propagates.
            #
            try:
                if request.setup and self._armed.get(bound.dst) != request.setup:
                    for frame in request.setup:
                        self.transport.request(
                            bytes(frame), dst=bound.dst, timeout=bound.timeout
                        )

                    self._armed[bound.dst] = request.setup

                #
                # The transport is told what the answer must look like -
                # service id, echoed identifier, minimum length - and
                # returns nothing that does not fit. Without this a late
                # answer to the PREVIOUS request with the same service
                # was handed back as this one's; the decoder caught the
                # cases where the identifier differed and mislabelled
                # them as decode faults, and could not catch the F303
                # case at all, where it does not.
                #
                response = self.transport.request(
                    bound.payload, dst=bound.dst, timeout=bound.timeout,
                    expect=bound.expectation(),
                )
            except Exception as exc:
                #
                # A fault anywhere in a dynamic-identifier sequence means
                # the ECU's definition can no longer be trusted to be
                # this request's: the define may never have been
                # processed, or a late answer to it may still be in
                # flight. Disarm, so the next read of that identifier
                # re-sends its clear and define - two exchanges the ECU
                # answers in order, which then sit between the old poll
                # and the new one. See bmwdiag/protocol/correlate.py for
                # the three layers this is one of.
                #
                if request.setup:
                    self._armed.pop(bound.dst, None)

                if not _is_request_fault(exc):
                    #
                    # The socket itself is gone (closed, reset, never
                    # connected). That is the reconnect logic's job, not
                    # something to skip - every subsequent request would
                    # fail the same way.
                    #
                    raise

                #
                # This ONE exchange failed: the gateway refused to route to
                # that ECU, or it did not answer in time. Skipping it keeps
                # the other ~45 channels flowing. Before this, a single
                # `HsfzNack: gateway will not route to 0x18` tore down the
                # whole link and split the drive into a new run - 1.35% of
                # wall time lost, but every drive needing to be stitched
                # back together before it could be analysed.
                #
                self._note(request.id, exc)

                #
                # A negative acknowledgement is the gateway ANSWERING, in
                # order to refuse one target, and a negative response is
                # the ECU answering to refuse one request. Both are
                # positive evidence the link is alive, so neither may count
                # towards concluding it is dead - only silence can do that.
                #
                if fault_kind(exc) not in ("transport_nack", "negative_response"):
                    self._transport_faults += 1

                faults = self._request_faults.get(request.id, 0) + 1
                self._request_faults[request.id] = faults

                if faults >= REQUEST_FAULT_LIMIT:
                    #
                    # This one request keeps failing. Stand it down for a
                    # while so an ECU that is simply absent stops costing a
                    # full timeout every time it comes due - at 2 Hz that
                    # is otherwise a permanent tax on the whole loop.
                    #
                    self._rest_request(request.id)

                if self._transport_faults >= TRANSPORT_FAULT_BUDGET:
                    #
                    # Too many in a row to be individual ECUs - the link is
                    # the common factor. Let it reach the reconnect logic.
                    #
                    raise

                continue

            #
            # A completed exchange means the link is healthy, whatever
            # individual ECUs are doing - and that this particular request
            # is answering again, so its fault history goes too.
            #
            self._transport_faults = 0
            self._request_recovered(request.id)
            self.last_responses[request.id] = bytes(response)

            try:
                readings = read_response(request, bytes(response))
            except (DecodeError, MappingError) as exc:
                self._note(request.id, exc)
                continue
            except Exception as exc:            # defensive: never kill the loop
                self._note(request.id, exc)
                continue

            completed = time.time()
            self._record_ok(request.id, completed)
            self._record_quality(readings)
            out.append(DecodedResponse(
                request.id, bytes(response), _usable(readings), readings,
                completed,
            ))

        return out
