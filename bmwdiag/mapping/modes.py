"""
Drive modes: one policy knob over the polling plan.

A mode does NOT introduce a second scheduling system. It rescales the
periods of the polling classes the mappings already declare, so there
stays exactly one mechanism deciding when a request is due. A mapping
says what a channel *is* (rpm is fast, coolant is slow); a mode says how
much of that the operator wants right now.

    effective_period = declared_period * multiplier

A multiplier > 1 polls less often, < 1 more often. `normal` is all-ones
by definition: it IS the rates the mapping files declare. That matters
for provenance - "what rate was this recorded at?" is answered by the
mapping version plus the mode, with no third source of truth.

Two modes need something a multiplier cannot express:

  * `off` polls nothing at all. It is still connected (the link stays
    up), which is deliberately different from not running: it lets the
    parked-battery question be tested with the cable in and the ECUs
    left alone.
  * `sampling` duty-cycles - awake for a window, silent for a longer
    one - for multi-hour drives where most samples are redundant.
    Slow classes are exempt from the sleep, because the events worth
    catching on a long drive (a regeneration, a thermal excursion) are
    exactly the ones that would start and finish inside a sleep window.

Modes are runtime policy, not vehicle knowledge, so they live in code
rather than in `mappings/`. They are still plain data - a frozen table,
no expression language, nothing that executes - so an embedded runtime
can compile them the same way it compiles a mapping.
"""

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Mapping, Optional, Tuple

from .errors import PollingError
from .model import PollingClassDef

__all__ = [
    "DriveMode",
    "DRIVE_MODES",
    "DEFAULT_MODE",
    "get_mode",
    "mode_names",
    "apply_mode",
]


@dataclass(frozen=True)
class DriveMode:
    """
    How aggressively to poll, as a scaling of the declared classes.

    `multipliers` is keyed by polling-class name; a class not named is
    left at its declared rate. There is deliberately no wildcard: a mode
    that wants to touch a class must say so, so adding a class to a
    mapping cannot silently change what every mode does.
    """

    name: str
    description: str
    #: class name -> multiplier on the class period (2.0 = half as often)
    multipliers: Mapping[str, float] = field(default_factory=dict)
    #: False = send nothing; the link stays up but no request is due
    polls: bool = True
    #: (awake_seconds, asleep_seconds), or None for continuous
    duty: Optional[Tuple[float, float]] = None
    #: classes that keep polling through a duty-cycle sleep
    duty_exempt: FrozenSet[str] = frozenset()

    @property
    def duty_period(self) -> Optional[float]:
        if self.duty is None:
            return None

        return self.duty[0] + self.duty[1]

    def awake_at(self, elapsed: float) -> bool:
        """Is the duty cycle in its awake window `elapsed` seconds in?"""
        if self.duty is None:
            return True

        period = self.duty_period

        if not period:
            return True

        return (elapsed % period) < self.duty[0]


#
# The declared rates (what `normal` means) live in the mapping files:
#
#   motion   10 Hz    rpm, speed, map, pedal
#   context  1/10 s   load, throttle, relthr, torque, maf, rail, lambda
#   slow     1/10 s   temps, voltage, fuel rate, cat, EGR
#   rare     1/60 s   ambient, baro, fuel level, runtime, distance
#   dde_dyn  ~1/11 s each (22 requests, round-robin, one per 0.5 s)
#   egs      2 Hz     engaged gear
#
# The multipliers below are annotated with the rate they produce, since
# a bare number like 0.01 says nothing on its own.
#
DRIVE_MODES: Dict[str, DriveMode] = {
    "off": DriveMode(
        name="off",
        description="connected but silent - no request is sent",
        polls=False,
    ),
    "debug": DriveMode(
        name="debug",
        description="everything, fast - for investigating a problem",
        multipliers={
            #: back to the pre-2026-08-30 behaviour: the whole OBD set
            #: at the loop rate, the DDE reads five times faster.
            "context": 0.01,        # 1/10 s  -> 10 Hz
            "slow": 0.1,            # 1/10 s  -> 1 Hz
            "rare": 0.1,            # 1/60 s  -> 1/6 s
            "dde_dyn": 0.2,         # ~1/11 s -> ~1/2.2 s
            "egs": 0.5,             # 2 Hz    -> 4 Hz
        },
    ),
    "normal": DriveMode(
        name="normal",
        description="the rates the mappings declare",
    ),
    "long": DriveMode(
        name="long",
        description="motorway cruising - most samples are redundant",
        multipliers={
            #: On cruise control the car can hold one gear and speed for
            #: kilometres, so the fast tier is nearly all duplicate rows.
            "motion": 5.0,          # 10 Hz   -> 2 Hz
            "context": 3.0,         # 1/10 s  -> 1/30 s
            "egs": 4.0,             # 2 Hz    -> 0.5 Hz
            "dde_dyn": 2.0,         # ~1/11 s -> ~1/22 s
        },
    ),
    "sampling": DriveMode(
        name="sampling",
        description="duty-cycled bursts for multi-hour drives",
        #: Two minutes of full-rate data every twelve. The slow classes
        #: never sleep, so a regeneration or a thermal event that starts
        #: inside a quiet window is still recorded - only the fast,
        #: highly-redundant channels are duty-cycled.
        duty=(120.0, 600.0),
        duty_exempt=frozenset({"slow", "rare", "dde_dyn"}),
    ),
}

DEFAULT_MODE = "normal"


def mode_names() -> Tuple[str, ...]:
    """
    Modes in the order they should be offered: quietest first.

    The order is measured, not assumed - see
    tests/test_drive_modes.py::test_the_modes_are_monotonically_quieter,
    which counts requests over a full duty period. `sampling` lands below
    `long` because it silences the fast tiers entirely for ten minutes in
    twelve, where `long` merely slows them. They are different trades
    (full resolution in bursts vs. coarse resolution throughout), not
    just different amounts.
    """
    return ("off", "sampling", "long", "normal", "debug")


def get_mode(name: Optional[str]) -> DriveMode:
    if not name:
        return DRIVE_MODES[DEFAULT_MODE]

    try:
        return DRIVE_MODES[name]
    except KeyError:
        raise PollingError(
            f"unknown drive mode {name!r}; known modes are "
            + ", ".join(sorted(DRIVE_MODES))
        )


def apply_mode(
    classes: Mapping[str, PollingClassDef], mode: DriveMode
) -> Dict[str, PollingClassDef]:
    """
    Rescale polling classes for a mode. Pure - returns new objects.

    Cycle-based classes scale their cycle count (and never drop below 1
    cycle, which is as fast as the loop can go); wall-clock classes scale
    their period, which for an `hz` class means dividing the rate.
    """
    out: Dict[str, PollingClassDef] = {}

    for name, cls in classes.items():
        factor = mode.multipliers.get(name)

        if factor is None or factor == 1.0:
            out[name] = cls
            continue

        if factor <= 0:
            raise PollingError(
                f"mode {mode.name!r} gives class {name!r} a non-positive "
                f"multiplier {factor!r}"
            )

        if cls.kind == "hz":
            #: period *= factor  =>  rate /= factor
            value = cls.value / factor
        elif cls.kind == "seconds":
            value = cls.value * factor
        else:                                   # cycles
            value = max(1.0, round(cls.value * factor))

        out[name] = PollingClassDef(
            name=cls.name,
            kind=cls.kind,
            value=value,
            priority=cls.priority,
            stagger=cls.stagger,
        )

    return out
