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

from typing import Any, Dict, List, Optional, Sequence

from ..protocol.request import (
    DecodedResponse,
    DiagnosticRequest,
    build_request,
)
from .decoder import decode_response
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

    # A negative acknowledgement: the gateway is alive and refused to route
    # to one target (e.g. "gateway will not route to 0x18" for the EGS).
    return exc.__class__.__name__.endswith("Nack")


class MappingExecutor:
    """
    Runs a set of due requests and returns normalised signal values.

    Decode failures are swallowed per request - a garbled reply costs one
    channel for one cycle, exactly as before. Transport failures are not:
    they belong to the application's reconnect logic.
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
        # Consecutive per-request transport faults. One ECU that is slow or
        # absent must not tear down a link that is otherwise fine, but a link
        # that has genuinely died has to reach the reconnect logic - and it
        # looks the same from a single request. So faults are tolerated per
        # request and counted; enough of them in a row means the link, not
        # the ECU, and the error is re-raised. Any success resets it.
        #
        self._transport_faults = 0

    # -- helpers ----------------------------------------------------

    def _note(self, request_id: str, exc: Exception) -> None:
        if self.on_error is not None:
            self.on_error(request_id, exc)

    def bind(self, request: RequestDef) -> DiagnosticRequest:
        return build_request(request, self.targets)

    # -- execution --------------------------------------------------

    def execute(
        self, requests: Sequence[RequestDef]
    ) -> Dict[str, Any]:
        """Run every request and merge the decoded signals."""
        out: Dict[str, Any] = {}

        for decoded in self.execute_detailed(requests):
            out.update(decoded.values)

        return out

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

        got = self.obd_reader.read(pids)
        out: List[DecodedResponse] = []

        for pid, data in got.items():
            request = by_pid.get(pid)

            if request is None:
                continue

            response = obd_logical_response(request, data)
            self.last_responses[request.id] = response

            try:
                values = decode_response(request, response)
            except (DecodeError, MappingError) as exc:
                self._note(request.id, exc)
                continue
            except Exception as exc:            # defensive: never kill the loop
                self._note(request.id, exc)
                continue

            out.append(DecodedResponse(request.id, response, values))

        return out

    def _run_generic(self, requests: Sequence[RequestDef]) -> List[DecodedResponse]:
        if not requests:
            return []

        if self.transport is None:
            raise MappingError("no diagnostic transport configured")

        out: List[DecodedResponse] = []

        for request in requests:
            bound = self.bind(request)

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

                response = self.transport.request(
                    bound.payload, dst=bound.dst, timeout=bound.timeout
                )
            except Exception as exc:
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
                self._transport_faults += 1
                self._note(request.id, exc)

                if self._transport_faults >= TRANSPORT_FAULT_BUDGET:
                    #
                    # Too many in a row to be individual ECUs - the link is
                    # the common factor. Let it reach the reconnect logic.
                    #
                    raise

                continue

            #
            # A completed exchange means the link is healthy, whatever
            # individual ECUs are doing.
            #
            self._transport_faults = 0
            self.last_responses[request.id] = bytes(response)

            try:
                values = decode_response(request, bytes(response))
            except (DecodeError, MappingError) as exc:
                self._note(request.id, exc)
                continue
            except Exception as exc:            # defensive: never kill the loop
                self._note(request.id, exc)
                continue

            out.append(DecodedResponse(request.id, bytes(response), values))

        return out
