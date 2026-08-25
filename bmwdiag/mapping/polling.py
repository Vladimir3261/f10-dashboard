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
"""

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import PollingError
from .model import PollingClassDef, RequestDef

__all__ = ["DEFAULT_POLLING_CLASSES", "resolve_classes", "PollingPlan"]

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
    ):
        self.classes = classes if classes is not None else resolve_classes()
        self.requests: List[RequestDef] = list(requests)
        self._last: Dict[str, float] = {}

        for request in self.requests:
            if request.polling_class not in self.classes:
                raise PollingError(
                    f"request {request.id!r} uses polling class "
                    f"{request.polling_class!r}, which is not defined"
                )

        self.requests.sort(key=lambda r: (
            self.classes[r.polling_class].priority, r.order, r.id
        ))

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
        out: List[RequestDef] = []

        for request in self.requests:
            cls = self.classes[request.polling_class]

            if cls.kind == "cycles":
                every = max(1, int(cls.value))

                if cycle % every == 0:
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

            if last is None or now - last >= period:
                self._last[request.id] = now
                out.append(request)

        return out
