"""
Polling plan.

The plan schedules REQUESTS, never signal names. Several signals decoded
from one reply therefore cost one exchange, and adding a signal to an
existing request adds no traffic at all.

Scheduling is wall-clock, in seconds, and that is the only unit. A class
declares `{seconds: N}`; a request is due when N seconds have passed
since it last went out. The poll loop's own rate sets the granularity -
asking for a period shorter than one loop cycle just means "every cycle".

A drive mode sits on top as a scaling of those classes (see modes.py).
It is applied here rather than beside here, so there remains exactly one
place that decides whether a request is due.
"""

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import PollingError
from .model import PollingClassDef, RequestDef
from .modes import DriveMode, apply_mode

#: What a plan uses when the caller names no mode: scales nothing, so a
#: plan built without one behaves exactly as the mappings declare. The
#: scheduler deliberately does NOT load config/modes.yaml itself - a
#: caller that wants a named mode passes it in, which keeps this module
#: free of file I/O and keeps the table's version in the caller's hands
#: where it can be recorded.
UNSCALED = DriveMode("normal", "the rates the mappings declare")

__all__ = ["DEFAULT_POLLING_CLASSES", "resolve_classes", "PollingPlan"]

#: Tolerance when deciding a wall-clock request is due, in seconds. See
#: the comment at its use in `due()`: without it a class asking for the
#: loop rate itself silently runs at half that rate.
SCHEDULE_SLACK = 0.001

#
# PHASE SPREADING WAS TRIED HERE AND REJECTED ON MEASUREMENT.
#
# Issue #19 asks for it: several non-staggered classes share a period, so
# they all become due on the same wall-clock instant - 26 logical
# requests at every minute boundary against a baseline of 4.
#
# Measured in the unit that actually matters, that burst is not what it
# looks like. `MappingExecutor._run_obd` hands every OBD request due in
# one cycle to `ObdSession.read`, which packs SIX PIDs into one Mode 01
# exchange. The 26-request cycle is SEVEN physical exchanges; batching
# had already absorbed it.
#
# Spreading the requests apart therefore trades a rare worst cycle for a
# smaller one, and pays for it by breaking batches that used to be free.
# Measured over 30 simulated minutes per drive mode, counting F303 setup
# frames the way _run_generic actually sends them:
#
#     mode       exchanges/min       worst cycle (physical)
#     normal     1133 -> 1115         11 -> 8     (-1.6%)
#     long        357 ->  418         11 -> 7     (+16.9%)
#     sampling    552 ->  582         11 -> 9     (+5.3%)
#
# Note where the worst cycle actually comes from: a paired dde_dyn slot
# is two setup-plus-poll sequences, six exchanges, and phasing the OBD
# side does not touch it. So spreading buys less than the logical count
# suggests AND costs continuously.
#
# `long` exists to reduce link load on a motorway drive, and phasing
# would add a quarter to its wire traffic to shave three exchanges off a
# cycle that happens once a minute. So the burst is left alone: it is
# already cheap, and the fix costs more than the problem.
#
# Recorded rather than silently dropped because "the issue asked for it"
# is a reason to measure, not a reason to ship.
#

#: Fallback classes for a mapping that declares none of its own. Every
#: shipped mapping declares its own, so these are a safety net rather
#: than a default anyone relies on.
DEFAULT_POLLING_CLASSES: Tuple[PollingClassDef, ...] = (
    PollingClassDef("fast", 0.1, priority=0),
    PollingClassDef("slow", 10.0, priority=1),
)


def resolve_classes(
    declared: Iterable[PollingClassDef] = (),
    overrides: Optional[Dict[str, PollingClassDef]] = None,
) -> Dict[str, PollingClassDef]:
    """
    Merge built-in classes, mapping-declared classes and runtime overrides.

    Precedence is runtime > mapping file > built-in.
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

    Requests come back ordered by class priority then declaration order,
    which is what keeps the OBD multi-PID batching deterministic.
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
        self.mode = mode if mode is not None else UNSCALED
        self.classes = apply_mode(self.declared, self.mode)
        self.requests: List[RequestDef] = list(requests)
        self._last: Dict[str, float] = {}
        #: Round-robin cursor per staggered class.
        self._rotation: Dict[str, int] = {}
        #: Rotation slots per staggered class. A slot is the set of
        #: requests sent on ONE firing: normally a single request, but a
        #: declared pair is one slot holding both members. Built once,
        #: after the sort, so the order is deterministic.
        self._slots: Dict[str, List[List[RequestDef]]] = {}
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

        self._validate_pairs()

        for name, cls in self.classes.items():
            if cls.stagger:
                self._slots[name] = self._rotation_slots(name)

    def _validate_pairs(self) -> None:
        """
        Reject a pair declaration that cannot mean what it says.

        Fail early and loudly. Every one of these used to be accepted and
        silently do nothing, which for a scheduling primitive is the
        worst outcome: the mapping would look co-scheduled, the data
        would not be, and the alignment coverage would quietly sit at
        zero exactly as it did before pairing existed.
        """
        groups: Dict[str, List[RequestDef]] = {}

        for request in self.requests:
            if request.polling_pair:
                groups.setdefault(request.polling_pair, []).append(request)

        for tag, members in groups.items():
            classes = {r.polling_class for r in members}

            if len(members) != 2:
                raise PollingError(
                    f"polling pair {tag!r} has {len(members)} members "
                    f"({sorted(r.id for r in members)!r}); a pair is exactly "
                    "two. One member schedules nothing and is usually a "
                    "misspelled tag on its partner; three or more would "
                    "quietly make one firing cost three re-arm sequences, "
                    "which is a group, not a pair - if that is wanted it "
                    "should be named and costed as one"
                )

            if len(classes) > 1:
                raise PollingError(
                    f"polling pair {tag!r} spans polling classes "
                    f"{sorted(classes)!r}. Members are co-scheduled inside "
                    "ONE class rotation, so a pair cannot cross classes"
                )

            name = classes.pop()

            if not self.classes[name].stagger:
                raise PollingError(
                    f"polling pair {tag!r} is on class {name!r}, which is "
                    "not staggered. Requests in an unstaggered class are "
                    "already all due together, so the tag would do nothing"
                )

    def _rotation_slots(self, name: str) -> List[List["RequestDef"]]:
        """
        Group a staggered class into firing slots, pairs kept together.

        A pair group takes the rotation position of its FIRST member, so
        the order is a function of the sorted request list and nothing
        else - not of which member happens to be declared first.

        This is why pairing is an explicit tag rather than an accident of
        ordering. Before it, `n47d_boost_act` and `n47d_boost_set` landed
        in adjacent slots and were 0.5 s apart, while `n47d_rail_act` and
        `n47d_rail_set` landed three slots apart and were 1.5 s apart -
        outside the 1.0 s window their alignment contract declares, so
        rail act-vs-setpoint had ~0% usable coverage. Neither outcome was
        chosen; both fell out of how the loader happened to order
        requests across files. A reordering could have silently swapped
        which pair worked.
        """
        slots: List[List[RequestDef]] = []
        index: Dict[str, int] = {}

        for request in self.requests:
            if request.polling_class != name:
                continue

            tag = request.polling_pair

            if tag:
                if tag in index:
                    slots[index[tag]].append(request)

                    continue

                index[tag] = len(slots)

            slots.append([request])

        return slots

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
        #: Staggered classes due this cycle, resolved to a single
        #: round-robin slot after the eager pass so ordering and the
        #: byte-pinned OBD behaviour are untouched.
        staggered: List[str] = []

        #
        # A staggered class is timed as a WHOLE, not per request: the
        # period is the gap between firings of the CLASS, and exactly one
        # member goes out per firing, so a member's own refresh interval
        # is period x members. Its due-check therefore runs ONCE, here -
        # asking per member would let the first member consume the slot
        # and the rest would never be considered.
        #
        stagger_due = {
            name: self._is_due(name, cls.period, now)
            for name, cls in self.classes.items()
            if cls.stagger and not (asleep and name not in self.mode.duty_exempt)
        }

        for request in self.requests:
            cls = self.classes[request.polling_class]

            #: During a duty-cycle sleep only the exempt classes run, so
            #: slow-moving events are never lost in a quiet window.
            if asleep and cls.name not in self.mode.duty_exempt:
                continue

            if cls.stagger:
                if stagger_due.get(cls.name) and cls.name not in staggered:
                    staggered.append(cls.name)

                continue

            if self._is_due(request.id, cls.period, now):
                out.append(request)

        #
        # One SLOT per staggered class per firing, cycling through them in
        # sorted order. A slot is usually one request; a declared pair is
        # one slot holding both members, so an actual/setpoint pair goes
        # out in the same cycle - and therefore under the same recorded
        # timestamp - instead of seconds apart.
        #
        # The class still fires once per period, so pairing costs no
        # extra firings. It shortens the full rotation (one slot instead
        # of two for a pair), which raises that class's request rate in
        # proportion - measured and reported in the PR, not hand-waved.
        #
        for name in staggered:
            slots = self._slots.get(name) or []

            if not slots:
                continue

            cursor = self._rotation.get(name, 0)
            out.extend(slots[cursor % len(slots)])
            self._rotation[name] = cursor + 1

        return out

    def _is_due(self, key: str, period: float, now: Optional[float]) -> bool:
        """
        Has `period` elapsed since `key` last fired?

        With no clock, everything is due - callers that pass no `now`
        want the full set (startup, introspection, tests).

        Slack matters at the top of the range. A class asking for exactly
        the loop rate (motion is 0.1 s against a 10 Hz loop) can never
        satisfy a strict `elapsed >= period`: each cycle arrives a few
        microseconds short, so the request is deferred to the next one
        and the channel runs at HALF its declared rate. A millisecond of
        slack absorbs that without meaningfully advancing anything
        slower - 1% of the fastest period we use, 0.002% of the slowest.
        """
        if now is None:
            return True

        last = self._last.get(key)

        if last is None or now - last >= period - SCHEDULE_SLACK:
            self._last[key] = now

            return True

        return False
