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

import math
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ..errors import DiagnosticError, RequestTimeout, classify_exception
from ..protocol.request import (
    DecodedResponse,
    DiagnosticRequest,
    ObdExchange,
    ObdReadReport,
    build_request,
)
from .decoder import OK, STALE, Reading, read_response
from .errors import DecodeError, MappingError
from .model import RequestDef
from .registry import ResolvedProfile

__all__ = [
    "BatchOmitted", "MappingExecutor", "NoResponse", "Retired", "fault_detail",
    "fault_kind", "obd_logical_response",
]


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


class NoResponse(DiagnosticError):
    """
    A PID the OBD reader asked for and did not get back.

    Not raised by anything - `ObdSession` absorbs these under its own
    three-strikes policy and simply omits the PID from the reply. It
    exists so the no-response case can travel down the same `on_error`
    path as a real transport fault, rather than being counted in one
    place and reported in another.

    Since the reader started reporting its exchanges (issue #16) this is
    precisely "the batch was answered and this PID was not in it" - a
    timed-out or refused exchange keeps its own kind.
    """

    kind = "no_response"
    scope = "request"
    answered = False


class BatchOmitted(NoResponse):
    """
    A PID the ECU left out of an answered batch and then delivered to
    the single re-read in the same cycle.

    The same wire event as `NoResponse` - one answered frame without
    this PID - counted on the same `no_response` outcome, but its own
    kind in the error stream: it happens once per connection, when the
    reader learns the ECU will not batch, and recovers immediately. A
    row that says `batch_omitted` is "the reader adapted"; a row that
    says `no_response` is a PID the ECU is not delivering at all.
    """

    kind = "batch_omitted"


class Retired(DiagnosticError):
    """
    The OBD reader gave up on a PID: it will not be asked again this
    connection.

    Reported ONCE, at the moment of retirement, so the persistent error
    stream carries the state change with the strike count that caused
    it. Before this, a retired PID was still counted as sent and
    "unanswered" on every cycle it came due - 600 phantom faults an hour
    for a PID the car was never being asked about.
    """

    kind = "retired"
    scope = "request"
    answered = False

    def __init__(self, pid: int, strikes: int):
        super().__init__(
            f"PID 0x{pid:02X} retired after {strikes} consecutive faults; "
            f"not asked again this connection"
        )
        self.pid = pid
        self.strikes = strikes

    def detail(self) -> Dict[str, Any]:
        return {"pid": self.pid, "strikes": self.strikes}


#: Which stage counter a fault kind lands on. `failed` and `kinds` keep
#: counting everything; these are the unambiguous names the diagnostics
#: view is built from. A kind outside the table (`transport_link`,
#: `other`) counts in `failed` only.
_OUTCOME_OF_KIND = {
    "negative_response": "negative_response",
    "transport_timeout": "timeout",
    "pending_timeout": "timeout",
    "transport_nack": "nack",
    "no_response": "no_response",
    "batch_omitted": "no_response",
    "decode": "decode_failed",
}

#: Ring sizes for the per-request latency and per-request refresh
#: series. Small and fixed: the hot path writes one float per exchange,
#: and the percentile is computed only when a report is built.
LATENCY_WINDOW = 32
REFRESH_WINDOW = 16


class _Series:
    """
    A fixed ring of the last N values plus a running count and sum.

    Push is one list assignment and two additions - cheap enough for
    every exchange. Anything that needs a sort (p95, median) happens in
    `summary()`, which only the diagnostics report calls.
    """

    __slots__ = ("ring", "idx", "n", "total", "last")

    def __init__(self, size: int):
        self.ring = [0.0] * size
        self.idx = 0
        self.n = 0
        self.total = 0.0
        self.last: Optional[float] = None

    def push(self, value: float) -> None:
        self.ring[self.idx] = value
        self.idx = (self.idx + 1) % len(self.ring)
        self.n += 1
        self.total += value
        self.last = value

    def summary(self, digits: int = 1) -> Optional[Dict[str, Any]]:
        if not self.n:
            return None

        recent = sorted(self.ring[:min(self.n, len(self.ring))])
        rank = max(0, math.ceil(0.95 * len(recent)) - 1)

        return {
            #: Over the whole session.
            "avg": round(self.total / self.n, digits),
            #: Over the last `window` values only - what the link is
            #: doing NOW, not what it averaged since the driveway.
            "p95": round(recent[rank], digits),
            "median": round(recent[len(recent) // 2], digits),
            #: The largest value still in the window: a pause stays
            #: visible here for `window` more refreshes, where `last`
            #: forgets it on the very next one.
            "max": round(recent[-1], digits),
            "last": round(self.last, digits),
            "n": self.n,
            "window": len(recent),
        }


def fault_kind(exc: BaseException) -> str:
    """
    A stable, structured name for what went wrong.

    Returned so callers can record and aggregate faults without parsing
    exception messages - a message is prose that changes; a kind is data you
    can group by. Used to attribute errors per request in the lake, which is
    what makes "this channel fails 8% of the time" answerable at all.

    The names are the taxonomy's (bmwdiag.errors): `transport_link`,
    `transport_nack`, `transport_timeout`, `negative_response`, `decode`,
    `no_response`, `other`. They are already in `telemetry.channel_errors`
    and are not renamed; finer distinctions travel in `fault_detail()`.
    `retired` (issue #16) is the one state change recorded through the
    same stream: an OBD PID struck out and will not be asked again.
    `batch_omitted` (issue #16) is `no_response`'s one-off sibling: the
    batch left the PID out but the same-cycle re-read delivered it - the
    reader adapting to an ECU that will not batch, not a PID the car is
    withholding. Both count on the `no_response` outcome; only the kind
    tells them apart, which is why it exists.
    A mapping error that is not a decode failure (a loader problem
    surfacing at poll time) is still reported as `decode`: it is our data,
    not the car, and that is what the kind has always meant.
    """
    if isinstance(exc, DiagnosticError):
        return exc.kind

    if isinstance(exc, MappingError):
        return "decode"

    return classify_exception(exc)[0]


def fault_detail(exc: BaseException) -> Dict[str, Any]:
    """
    The structured part of a fault: the NRC and service of a negative
    response, the target of a routing NACK, the elapsed time and pending
    count of a timeout. Empty for exceptions outside the taxonomy. Always
    JSON-safe, so it can be stored next to the kind and shipped as-is.
    """
    if isinstance(exc, DiagnosticError):
        return dict(exc.detail())

    return {}


def _is_request_fault(exc: BaseException) -> bool:
    """
    Did ONE exchange fail, or has the link died?

    Skipping a request is only safe while the link is still good; otherwise
    every later request fails identically and the reconnect never happens.

    Decided by the exception's category, never by its name or text: the
    transport raises into the taxonomy (`LinkError`, `RoutingNack`,
    `RequestTimeout`, `NegativeResponse`), and `bmwdiag` - which knows
    nothing about HSFZ, imports nothing outside the standard library and
    opens no sockets - reads the category. A bare socket exception is
    classified by its stdlib type. Anything unrecognised counts as a link
    fault, which is the conservative direction - a needless reconnect costs
    a few seconds, whereas mistaking a dead link for a slow ECU means polling
    a closed socket forever.
    """
    return classify_exception(exc)[1] == "request"


def _answered(exc: BaseException) -> bool:
    """
    Did the far side demonstrably reply? A routing NACK is the gateway
    answering to refuse one target; a negative response is the ECU
    answering to refuse one request. Both are positive evidence the link
    is alive, so neither may count towards concluding it is dead - only
    silence can do that.

    Liveness, not accounting: what a failed exchange put on the wire is
    `_rx_of`. A pending timeout is `answered` (the ECU said "wait", so
    the link is alive) and yet has no answer to time.
    """
    return classify_exception(exc)[2]


def _rx_of(exc: BaseException) -> Tuple[int, bool]:
    """
    What a failed exchange received: (frames, timed answer?).

    The single place both accounting paths (the generic loop and the OBD
    report) get this from, so they cannot disagree:

    * negative response - one frame from the ECU, and it IS the answer:
      its latency is the ECU's latency;
    * routing NACK - one frame, from the gateway, refusing the target.
      Nothing the ECU did, so no latency sample;
    * pending timeout - every `responsePending` the ECU sent is a
      received frame, but the final answer never came. The elapsed time
      is the deadline, not a latency: recording it would put the full
      wait into `latency_ms.p95` for exactly the 0x78-then-silent
      identifiers the view exists to expose;
    * timeout - nothing received.
    """
    kind, _scope, answered = classify_exception(exc)

    if isinstance(exc, RequestTimeout) or kind in ("transport_timeout",
                                                    "pending_timeout"):
        return int(fault_detail(exc).get("pending") or 0), False

    if kind == "transport_nack":
        return 1, False

    if answered:
        return 1, True

    return 0, False


def _mark_stale(readings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Downgrade every `ok` reading to `stale`: the bytes decoded fine, but
    the transport could not tell whether they answer THIS request or the
    previous, timed-out one with the same content - they may be one
    period old. A reading already flagged for another reason keeps its
    own label; `stale` is not usable, so the display and the derived
    channels drop it and the lake keeps the number with the label.
    """
    return {
        key: Reading(reading.value, STALE) if reading.quality == OK else reading
        for key, reading in readings.items()
    }


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
        #: Per-request answered-exchange latency, ms (`_Series`).
        self._latency: Dict[str, _Series] = {}
        #: Per-request interval between successful decodes, seconds,
        #: on the monotonic clock - the EFFECTIVE refresh period, as
        #: opposed to the one the polling class declares.
        self._refresh: Dict[str, _Series] = {}
        self._last_ok_mono: Dict[str, float] = {}
        #
        # The physical wire, counted once per frame regardless of how
        # many logical requests a frame carried. The per-request
        # counters ATTRIBUTE a shared OBD batch to each member, so their
        # sum is not this; both are reported and named.
        #
        self._wire: Dict[str, int] = {
            "exchanges": 0, "tx_frames": 0, "rx_frames": 0,
            "setup_tx_frames": 0, "setup_rx_frames": 0, "setup_faults": 0,
            "obd_batches": 0, "obd_batched_pids": 0,
        }
        #: PIDs the OBD reader has retired, mirrored here so a request
        #: keeps reading `retired` even between reads.
        self._retired_pids: set = set()
        #: request id -> PID, for the OBD requests seen so far.
        self._pid_of: Dict[str, int] = {}
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
        stat = self._stats.get(request_id)

        if stat is None:
            stat = self._stats[request_id] = self._new_stat()

        return stat

    @staticmethod
    def _new_stat() -> Dict[str, Any]:
        #
        # Built once per request, not once per lookup: `setdefault` with
        # a literal default evaluates the literal every call, and this
        # one is ~30 keys on the hot path.
        #
        return {
            #: The stage counters (issue #16). Each is one unambiguous
            #: point in the pipeline; `sent` is kept as the historical
            #: name for `submitted`.
            #:
            #:   scheduled   the plan handed the request to the executor
            #:   submitted   it was actually put on the wire (not resting,
            #:               not retired)
            #:   skipped_*   scheduled but not submitted, and why
            "scheduled": 0, "submitted": 0, "sent": 0,
            "skipped_resting": 0, "skipped_retired": 0,
            #: Frames, attributed: a shared OBD batch counts once for
            #: EACH logical request it carried. Setup frames (the 2C
            #: clear/define before an F303 poll) are separate from the
            #: poll itself.
            "exchanges": 0, "tx_frames": 0, "rx_frames": 0,
            "setup_tx_frames": 0, "setup_rx_frames": 0, "setup_faults": 0,
            #: What came back, by outcome. `positive_response` is counted
            #: before decoding, so it can exceed `ok`.
            "positive_response": 0, "negative_response": 0, "timeout": 0,
            "nack": 0, "no_response": 0, "decode_failed": 0,
            #: Signals: produced by the decoder, usable by quality, and
            #: cycles where a positive response decoded to nothing usable.
            "decoded_signals": 0, "accepted_signals": 0, "all_rejected": 0,
            "last_rejection": None,
            "last_tx": None, "last_rx": None,
            "ok": 0, "failed": 0,
            "kinds": {}, "last_ok": None, "last_error": None,
            "last_error_at": None,
            #: The structured fields of the last fault (`fault_detail`):
            #: for a negative response the service and NRC, for a NACK
            #: the target. What the message says in prose, as data.
            "last_detail": None,
            #: Answers that arrived after this request had already been
            #: given up on, and were discarded by the transport. NOT a
            #: failure - the timeout was already counted as one - and
            #: not an `ok`: the value never reached the decoder. A
            #: channel with many of these has a timeout that is too
            #: short for the ECU, which is a different fix from one
            #: that never answers at all.
            "late": 0,
            #: Answers accepted but marked `stale` because the transport
            #: could not tell them from the previous, timed-out request
            #: of the same content (an ambiguous re-poll). Counted as
            #: `ok` at the request level - the exchange worked - with
            #: the readings themselves carrying the flag.
            "ambiguous": 0,
        }

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
                    rid: self._snapshot(rid, st)
                    for rid, st in self._stats.items()
                }
            except RuntimeError:                # changed size during iteration
                continue

        return {}

    def _snapshot(self, request_id: str, st: Dict[str, Any]) -> Dict[str, Any]:
        rest = self._rest_fields(request_id)
        latency = self._latency.get(request_id)
        refresh = self._refresh.get(request_id)
        pid = self._pid_of.get(request_id)

        if pid is not None and pid in self._retired_pids:
            state = "retired"
        elif rest["resting_for"]:
            state = "resting"
        elif st["scheduled"]:
            state = "active"
        else:
            state = "idle"

        return {
            **st,
            "kinds": dict(st["kinds"]),
            **rest,
            #: Where the request stands right now - one word, so the
            #: view never has to infer "retired" from a frozen counter.
            "state": state,
            "latency_ms": latency.summary() if latency else None,
            "refresh_s": refresh.summary(2) if refresh else None,
        }

    def wire_stats(self) -> Dict[str, int]:
        """
        The physical frame counts, once per frame. Copied.

        Distinct from the per-request counters, which attribute a shared
        OBD batch to every logical request it carried: 19 PIDs in four
        batches is `exchanges: 4` here and `exchanges: 1` on each of the
        19 requests.
        """
        return dict(self._wire)

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
        stat = self._stat(request_id)
        stat["submitted"] += 1
        stat["sent"] = stat["submitted"]
        stat["last_tx"] = time.time()

    def _record_scheduled(self, request_id: str) -> Dict[str, Any]:
        stat = self._stat(request_id)
        stat["scheduled"] += 1

        return stat

    def _record_latency(self, request_id: str, seconds: float) -> None:
        series = self._latency.get(request_id)

        if series is None:
            series = self._latency[request_id] = _Series(LATENCY_WINDOW)

        series.push(seconds * 1000.0)

    def _record_rx(self, stat: Dict[str, Any], frames: int = 1) -> None:
        stat["rx_frames"] += frames
        stat["last_rx"] = time.time()

    def _record_signals(self, request_id: str, readings: Dict[str, Any]) -> None:
        """
        The decode -> accept boundary, per request: how many signals the
        decoder produced and how many survived quality. A positive
        response whose every signal was rejected is the case the
        request counters could never show - it looks like a clean `ok`
        - so it gets its own count and the labels that caused it.
        """
        stat = self._stat(request_id)
        accepted = sum(1 for r in readings.values() if r.usable)
        stat["decoded_signals"] += len(readings)
        stat["accepted_signals"] += accepted

        if readings and not accepted:
            stat["all_rejected"] += 1
            stat["last_rejection"] = sorted({
                r.quality for r in readings.values()
            })

    def _record_ambiguous(self, request_id: str) -> None:
        self._stat(request_id)["ambiguous"] += 1

    def _record_ok(self, request_id: str, when: float,
                   mono: Optional[float] = None) -> None:
        stat = self._stat(request_id)
        stat["ok"] += 1
        stat["last_ok"] = when

        #
        # The effective refresh interval, from the acquisition clock. A
        # staggered class declares 0.5 s and delivers one member every
        # ~11 s; `sampling` mode delivers nothing for ten minutes at a
        # time. Only a measured interval can say what a channel's real
        # cadence was.
        #
        mono = time.monotonic() if mono is None else mono
        previous = self._last_ok_mono.get(request_id)
        self._last_ok_mono[request_id] = mono

        if previous is not None:
            series = self._refresh.get(request_id)

            if series is None:
                series = self._refresh[request_id] = _Series(REFRESH_WINDOW)

            series.push(mono - previous)

    def _record_fault(self, request_id: str, kind: str, message: str,
                      exc: Optional[BaseException] = None) -> None:
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
        outcome = _OUTCOME_OF_KIND.get(kind)

        if outcome is not None:
            stat[outcome] += 1

        stat["last_error"] = f"{kind}: {message}"
        stat["last_error_at"] = time.time()
        stat["last_detail"] = fault_detail(exc) if exc is not None else None

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

        #
        # What the reader will no longer ask for. Consulted BEFORE the
        # request is counted as submitted: a retired PID is scheduled
        # (the plan does not know) but never attempted, and must not
        # appear in the diagnostics as asked-and-unanswered. Until #16
        # it did, every cycle, with a phantom `no_response` fault each.
        #
        retired = set(getattr(self.obd_reader, "retired", ()) or ())
        self._retired_pids |= retired

        by_pid: Dict[int, RequestDef] = {}
        pids: List[int] = []

        for request in requests:
            if request.pid is None:
                continue

            #
            # One request per PID, so a PID never goes on the wire twice
            # even if two mappings both want a signal out of it.
            #
            if request.pid in by_pid:
                continue

            by_pid[request.pid] = request
            self._pid_of[request.id] = request.pid
            stat = self._record_scheduled(request.id)

            if request.pid in retired:
                stat["skipped_retired"] += 1
                continue

            pids.append(request.pid)
            self._record_sent(request.id)

        if not pids:
            return []

        started = time.monotonic()
        got = self.obd_reader.read(pids)
        finished = time.monotonic()
        report = getattr(self.obd_reader, "last_report", None)

        if report is None:
            #
            # A reader that does not account for its wire (the test
            # fakes; any minimal reader) is taken to have made one
            # answered exchange carrying everything it asked for.
            #
            report = ObdReadReport(exchanges=[ObdExchange(
                tuple(pids), started, finished, True, tuple(got),
            )])

        self._account_obd(report, by_pid)
        self._retired_pids |= set(report.retired)

        #
        # Retirement is reported once, as the state change it is, with
        # the strike count - not as another fault. The faults that
        # caused it were each reported as they happened.
        #
        for pid in report.retired_now:
            request = by_pid.get(pid)

            if request is not None and self.on_error is not None:
                self.on_error(
                    request.id, Retired(pid, report.strikes.get(pid, 0))
                )

        out: List[DecodedResponse] = []
        #
        # PIDs whose bytes came back under correlation ambiguity (the
        # reader's transport re-polled the same batch after a timeout
        # and could not tell the two answers apart). Optional on the
        # reader; a reader without it never flags.
        #
        ambiguous = set(getattr(self.obd_reader, "ambiguous_pids", ()) or ())

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

            if pid in ambiguous:
                readings = _mark_stale(readings)
                self._record_ambiguous(request.id)

            completed = time.time()
            self._record_ok(request.id, completed, finished)
            self._record_signals(request.id, readings)
            self._record_quality(readings)
            out.append(DecodedResponse(
                request.id, response, _usable(readings), readings, completed,
            ))

        return out

    def _account_obd(
        self, report: ObdReadReport, by_pid: Dict[int, RequestDef]
    ) -> None:
        """
        Turn the reader's exchange report into stage counters.

        The wire counts each frame once. Each logical request in a batch
        is attributed the frame - so a six-PID batch is one exchange on
        the wire and one exchange on each of six requests. A failed
        exchange is a fault on every request it carried, with the
        exception's OWN kind; an answered exchange missing a PID is a
        `no_response` on that PID alone - the batch was answered, the
        ECU just did not include it. When a later frame in the same
        read carried the PID (the reader's single re-read), the omission
        is labelled `batch_omitted` instead: same outcome counter, a
        distinct row, so the once-per-connection "ECU does not batch"
        event is not read as a PID going unanswered.
        """
        delivered: Set[int] = set()

        for exchange in report.exchanges:
            delivered.update(exchange.returned)

        for exchange in report.exchanges:
            self._wire["exchanges"] += 1
            self._wire["tx_frames"] += 1
            self._wire["obd_batches"] += 1
            self._wire["obd_batched_pids"] += len(exchange.pids)

            #
            # What came back is decided by the SAME rule as the generic
            # path (`_rx_of`): the reader's `answered` flag says the far
            # side spoke, which is liveness, not a frame count - a
            # pending timeout is "answered" and has no answer to time.
            #
            if exchange.error is not None:
                rx, timed = _rx_of(exchange.error)
            elif exchange.answered:
                rx, timed = 1, True
            else:
                rx, timed = 0, False

            self._wire["rx_frames"] += rx
            latency = exchange.finished - exchange.started
            returned = set(exchange.returned)

            for pid in exchange.pids:
                request = by_pid.get(pid)

                if request is None:
                    continue

                stat = self._stat(request.id)
                stat["exchanges"] += 1
                stat["tx_frames"] += 1

                if rx:
                    self._record_rx(stat, rx)

                if timed:
                    self._record_latency(request.id, latency)

                if exchange.error is not None:
                    self._note(request.id, exchange.error)
                elif pid in returned:
                    stat["positive_response"] += 1
                elif pid in delivered:
                    self._record_fault(
                        request.id, BatchOmitted.kind,
                        "the ECU answered the batch without this PID; "
                        "the single re-read delivered it",
                        BatchOmitted("batch answered without this PID"),
                    )
                else:
                    self._record_fault(
                        request.id, "no_response",
                        "the ECU answered the batch without this PID",
                    )

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
            stat = self._record_scheduled(request.id)

            if self._rest_left(request.id) > 0:
                stat["skipped_resting"] += 1
                continue

            bound = self.bind(request)
            self._record_sent(request.id)
            in_setup = False
            started = finished = 0.0

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
                    #
                    # Counted apart from the poll: an F303 read is two
                    # setup frames plus one poll on the wire, and a
                    # request that only ever fails in its define is a
                    # different problem from one whose poll times out.
                    #
                    in_setup = True

                    for frame in request.setup:
                        stat["setup_tx_frames"] += 1
                        self._wire["setup_tx_frames"] += 1
                        self.transport.request(
                            bytes(frame), dst=bound.dst, timeout=bound.timeout
                        )
                        stat["setup_rx_frames"] += 1
                        self._wire["setup_rx_frames"] += 1

                    self._armed[bound.dst] = request.setup
                    in_setup = False

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
                stat["exchanges"] += 1
                stat["tx_frames"] += 1
                self._wire["exchanges"] += 1
                self._wire["tx_frames"] += 1
                started = time.monotonic()
                response = self.transport.request(
                    bound.payload, dst=bound.dst, timeout=bound.timeout,
                    expect=bound.expectation(),
                )
                finished = time.monotonic()
            except Exception as exc:
                #
                # The wire side of the fault, before policy: a NACK or a
                # negative response IS a received frame (and has a
                # latency); a timeout is not, though the responsePending
                # frames that preceded it were.
                #
                rx, timed = _rx_of(exc)

                if in_setup:
                    #
                    # An NRC or a NACK to a `2C` define is a frame the
                    # setup exchange received, and the fault; a timed-out
                    # define is the fault alone.
                    #
                    stat["setup_faults"] += 1
                    stat["setup_rx_frames"] += rx
                    self._wire["setup_faults"] += 1
                    self._wire["setup_rx_frames"] += rx
                else:
                    if rx:
                        self._record_rx(stat, rx)
                        self._wire["rx_frames"] += rx

                    if timed:
                        self._record_latency(request.id, time.monotonic() - started)
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
                if not _answered(exc):
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
            stat["positive_response"] += 1
            self._record_rx(stat)
            self._wire["rx_frames"] += 1
            self._record_latency(request.id, finished - started)

            try:
                readings = read_response(request, bytes(response))
            except (DecodeError, MappingError) as exc:
                self._note(request.id, exc)
                continue
            except Exception as exc:            # defensive: never kill the loop
                self._note(request.id, exc)
                continue

            #
            # The transport says whether the answer it just returned
            # could equally be the previous request's (same content,
            # re-polled after a timeout, nothing arrived in the quiet
            # window to break the tie). A valid answer, possibly one
            # period old: recorded, but as `stale`, never as `ok`.
            #
            if getattr(self.transport, "last_answer_ambiguous", False):
                readings = _mark_stale(readings)
                self._record_ambiguous(request.id)

            completed = time.time()
            self._record_ok(request.id, completed, finished)
            self._record_signals(request.id, readings)
            self._record_quality(readings)
            out.append(DecodedResponse(
                request.id, bytes(response), _usable(readings), readings,
                completed,
            ))

        return out
