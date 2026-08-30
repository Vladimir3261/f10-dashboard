"""
Polling plan.

The plan schedules REQUESTS, never signal names. Several signals decoded
from one reply therefore cost one exchange, and adding a signal to an
existing request adds no traffic at all.

Two scheduling kinds coexist:

    cycles   every Nth poll-loop iteration - the legacy fast/slow model,
             kept because --rate/--slow-every are defined in those terms
    hz/seconds
             wall-clock periods, so a future mapping can ask for 5 Hz or
             0.2 Hz without the loop cadence having to change

A drive mode sits on top as a scaling of those classes (see modes.py).
It is applied here rather than beside here, so there remains exactly one
place that decides whether a request is due.
"""

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import PollingError
from .model import PollingClassDef, RequestDef
from .modes import DriveMode, apply_mode, get_mode

__all__ = ["DEFAULT_POLLING_CLASSES", "resolve_classes", "PollingPlan"]

#: Tolerance when deciding a wall-clock request is due, in seconds. See
#: the comment at its use in `due()`: without it a class asking for the
#: loop rate itself silently runs at half that rate.
SCHEDULE_SLACK = 0.001

#: The two classes the current application defines. `slow.value` is
#: replaced at runtime by --slow-every.
DEFAULT_POLLING_CLASSES: Tuple[PollingClassDef, ...] = (
    PollingClassDef("fast", "cycles", 1.0, priority=0),
    PollingClassDef("slow", "cycles", 10.0, priority=1),
)


def resolve_classes(
    declared: Iterable[PollingClassDef] = (),
    overrides: Optional[Dict[str, PollingClassDef]] = None,
) -> Dict[str, PollingClassDef]:
    """
    Merge built-in classes, mapping-declared classes and runtime overrides.

    Precedence is runtime > mapping file > built-in, so --slow-every keeps
    the last word over whatever a mapping happens to declare.
    """
    out: Dict[str, PollingClassDef] = {c.name: c for c in DEFAULT_POLLING_CLASSES}

    for cls in declared:
        out[cls.name] = cls

    for name, cls in (overrides or {}).items():
        out[name] = cls

    return out


class PollingPlan:
    """
    Decides which requests are due.

    Cycle-based classes reproduce the previous behaviour exactly: `fast`
    runs every cycle, `slow` runs when `cycle % slow_every == 0`. Requests
    come back ordered by class priority then declaration order, which is
    what keeps the OBD multi-PID batching byte-identical to before.
    """

    def __init__(
        self,
        requests: Sequence[RequestDef],
        classes: Optional[Dict[str, PollingClassDef]] = None,
        mode: Optional[DriveMode] = None,
    ):
        #: What the mappings (plus CLI overrides) declared, before any
        #: mode scaling. Kept so switching modes always rescales from the
        #: declared rates rather than compounding on the previous mode.
        self.declared = dict(
            classes if classes is not None else resolve_classes()
        )
        self.mode = mode if mode is not None else get_mode(None)
        self.classes = apply_mode(self.declared, self.mode)
        self.requests: List[RequestDef] = list(requests)
        self._last: Dict[str, float] = {}
        #: Round-robin cursor per staggered class.
        self._rotation: Dict[str, int] = {}
        #: Wall clock the duty cycle is measured from; set on first use.
        self._duty_origin: Optional[float] = None

        for request in self.requests:
            if request.polling_class not in self.classes:
                raise PollingError(
                    f"request {request.id!r} uses polling class "
                    f"{request.polling_class!r}, which is not defined"
                )

        self.requests.sort(key=lambda r: (
            self.classes[r.polling_class].priority, r.order, r.id
        ))

    # -- modes ------------------------------------------------------

    def set_mode(self, mode: DriveMode) -> None:
        """
        Switch drive mode in place.

        Rescales from `self.declared`, never from the current (already
        scaled) classes, so debug -> long -> normal returns exactly to
        the declared rates. Request order is untouched: priorities are a
        property of the class, and a mode never changes them.
        """
        self.mode = mode
        self.classes = apply_mode(self.declared, mode)
        #: A slower class must not stay "overdue" from the faster mode,
        #: and a faster one should not wait out the old period.
        self._last.clear()
        self._duty_origin = None

    def duty_state(self, now: Optional[float] = None) -> str:
        """'continuous', 'awake' or 'asleep' - for the dashboard."""
        if self.mode.duty is None:
            return "continuous"

        if now is None or self._duty_origin is None:
            return "awake"

        return "awake" if self.mode.awake_at(now - self._duty_origin) else "asleep"

    # -- introspection ----------------------------------------------

    def counts(self) -> Dict[str, int]:
        """Requests per polling class, for startup logging."""
        out: Dict[str, int] = {}

        for request in self.requests:
            out[request.polling_class] = out.get(request.polling_class, 0) + 1

        return out

    def by_class(self, name: str) -> List[RequestDef]:
        return [r for r in self.requests if r.polling_class == name]

    def signal_keys(self) -> List[str]:
        return [s.key for r in self.requests for s in r.signals]

    # -- scheduling -------------------------------------------------

    def due(self, cycle: int, now: Optional[float] = None) -> List[RequestDef]:
        """Requests that should be sent this iteration."""
        #: `off` keeps the link up and sends nothing. That is not the same
        #: as not running: it is how the parked-battery question gets
        #: tested with the cable connected.
        if not self.mode.polls:
            return []

        if self.mode.duty is not None and now is not None:
            if self._duty_origin is None:
                self._duty_origin = now

            asleep = not self.mode.awake_at(now - self._duty_origin)
        else:
            asleep = False

        out: List[RequestDef] = []
        #: Members of a staggered class that are due this cycle, resolved
        #: to a single round-robin pick after the eager pass so ordering
        #: and the byte-pinned OBD behaviour are untouched.
        staggered: Dict[str, List[RequestDef]] = {}

        for request in self.requests:
            cls = self.classes[request.polling_class]

            #: During a duty-cycle sleep only the exempt classes run, so
            #: slow-moving events are never lost in a quiet window.
            if asleep and cls.name not in self.mode.duty_exempt:
                continue

            if cls.kind == "cycles":
                every = max(1, int(cls.value))

                if cycle % every != 0:
                    continue

                if cls.stagger:
                    staggered.setdefault(cls.name, []).append(request)
                else:
                    out.append(request)

                continue

            period = cls.period

            if period is None:
                out.append(request)
                continue

            if now is None:
                out.append(request)
                continue

            last = self._last.get(request.id)

            #
            # Slack matters at the top of the range. A class asking for
            # exactly the loop rate (motion is 10 Hz against a 10 Hz
            # loop) can never satisfy a strict `elapsed >= period`: each
            # cycle arrives a few microseconds short, so the request is
            # deferred to the next one and the channel runs at HALF its
            # declared rate. A millisecond of slack absorbs that without
            # meaningfully advancing anything slower: it is 1% of the
            # fastest period we use and 0.002% of the slowest.
            #
            if last is None or now - last >= period - SCHEDULE_SLACK:
                self._last[request.id] = now
                out.append(request)

        #
        # One member per staggered class per firing, cycling through them
        # in declaration order. `self.requests` is already priority-sorted,
        # so members come out in a stable order and the cursor advances
        # only on cycles the class actually fires.
        #
        for name, members in staggered.items():
            cursor = self._rotation.get(name, 0)
            out.append(members[cursor % len(members)])
            self._rotation[name] = cursor + 1

        return out
