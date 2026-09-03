"""
Drive modes: one policy knob over the polling plan.

A mode does NOT introduce a second scheduling system. It rescales the
periods of the polling classes the mappings already declare, so there
stays exactly one mechanism deciding when a request is due.

    effective_period = declared_period * multiplier

A multiplier > 1 polls less often, < 1 more often. `normal` is all-ones
by definition: it IS the rates the mapping files declare. That matters
for provenance - "what rate was this recorded at?" is answered by the
mapping version plus the mode, with no third source of truth.

Two modes need something a multiplier cannot express:

  * `off` polls nothing at all. It is still connected (the link stays
    up), which is deliberately different from not running.
  * `sampling` duty-cycles - awake for a window, silent for a longer
    one - for multi-hour drives where most samples are redundant. Slow
    classes are exempt from the sleep, because the events worth catching
    on a long drive are exactly the ones that would hide in a sleep
    window.

WHAT LIVES WHERE

The mode TABLE is data: `config/modes.yaml`, loaded through the same
dependency-free parser as the mappings. The arithmetic that applies it
is here, in Python, and stays here - putting it in the file would mean
adding an expression language to the config format, which is the one
thing that format must never have.

The table carries an `id` and a `version` and joins the same ledger as
the mapping files: it appears in VERSIONS.lock, and in the `id@version`
fingerprint recorded on every session as `drive-modes@1`. Without that a
mode name would not identify a rate - `long` in March and `long` in June
could differ and nothing would say so. The mode NAME is stored as plain
text beside it, so the common query stays readable.
"""

import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Optional, Tuple

from . import yamlsubset
from .errors import MappingError, PollingError
from .model import PollingClassDef

__all__ = [
    "DriveMode",
    "ModeTable",
    "DEFAULT_MODE_CONFIG",
    "load_modes",
    "apply_mode",
]

#: Shipped mode table. Overridable with --modes, so an experiment does
#: not have to edit the file the repository ships.
DEFAULT_MODE_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "modes.yaml",
)


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
    description: str = ""
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

    def classes_used(self) -> FrozenSet[str]:
        """Every polling class this mode names, for validation."""
        return frozenset(self.multipliers) | self.duty_exempt


@dataclass(frozen=True)
class ModeTable:
    """
    A loaded `config/modes.yaml`.

    Carries an `id` and a `version` for the same reason every mapping
    file does, and joins the same ledger: it appears in VERSIONS.lock and
    in the `id@version` fingerprint recorded on each session. That is
    what keeps a mode NAME honest - `long` before and after an edit to
    this table are different samplings, and the fingerprint is where the
    difference shows.

    It is deliberately NOT folded into the mapping files' versions: this
    table is not owned by any one of them, so bumping theirs would
    falsely signal that ten decode definitions changed.
    """

    id: str
    version: int
    default: str
    modes: Mapping[str, DriveMode]
    source_path: str = ""

    def fingerprint(self) -> str:
        """`id@version`, the form used in the session's mapping set."""
        return f"{self.id}@{self.version}"

    def get(self, name: Optional[str]) -> DriveMode:
        if not name:
            return self.modes[self.default]

        try:
            return self.modes[name]
        except KeyError:
            raise PollingError(
                f"unknown drive mode {name!r}; known modes are "
                + ", ".join(sorted(self.modes))
            )

    def names(self) -> Tuple[str, ...]:
        """Modes in declaration order - the file orders them quietest first."""
        return tuple(self.modes)

    def unknown_classes(self, declared_classes: Iterable[str]) -> Dict[str, Tuple[str, ...]]:
        """
        Mode -> the polling classes it names that nothing declares.

        A multiplier for a class nobody declares is dead configuration:
        it silently does nothing while reading as if it were working.

        Deliberately NOT an exception. Which mappings load is a per-run
        choice - a bare `live.py` loads standard OBD only, so `dde_dyn`
        and `egs` are legitimately absent and their multipliers correctly
        do nothing that run. Refusing to start would be wrong. The caller
        decides: the runtime warns, and a test asserts this is empty
        against the FULL mapping tree, which is where a real typo shows.
        """
        known = set(declared_classes)
        out: Dict[str, Tuple[str, ...]] = {}

        for mode in self.modes.values():
            missing = tuple(sorted(mode.classes_used() - known))

            if missing:
                out[mode.name] = missing

        return out


# ------------------------------------------------------------- loading


def _positive_int(raw: Any, source: str, path: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise MappingError(
            f"{source}: {path} must be a positive integer, got {raw!r}"
        )

    return raw


def _positive_finite(raw: Any) -> bool:
    """A number that scales a period: not a bool, not NaN, not infinite."""
    return (
        not isinstance(raw, bool)
        and isinstance(raw, (int, float))
        and math.isfinite(raw)
        and raw > 0
    )


def _multiplier(raw: Any, source: str, mode: str, cls: str) -> float:
    if not _positive_finite(raw):
        raise MappingError(
            f"{source}: modes.{mode}.multipliers.{cls} must be a positive "
            f"finite number, got {raw!r}"
        )

    return float(raw)


def _duty(raw: Any, source: str, mode: str) -> Optional[Tuple[float, float]]:
    if raw is None:
        return None

    if not isinstance(raw, dict):
        raise MappingError(
            f"{source}: modes.{mode}.duty must be a mapping with "
            f"`awake` and `asleep`, got {raw!r}"
        )

    out = []

    for key in ("awake", "asleep"):
        value = raw.get(key)

        if not _positive_finite(value):
            raise MappingError(
                f"{source}: modes.{mode}.duty.{key} must be a positive "
                f"finite number of seconds, got {value!r}"
            )

        out.append(float(value))

    return (out[0], out[1])


def _mode(name: str, raw: Any, source: str) -> DriveMode:
    #: `normal:` with nothing under it is a legitimate mode - it scales
    #: nothing. The parser gives None for an empty block.
    body = {} if raw is None else raw

    if not isinstance(body, dict):
        raise MappingError(
            f"{source}: modes.{name} must be a mapping, got {raw!r}"
        )

    unknown = set(body) - {"description", "multipliers", "polls", "duty",
                           "exempt"}

    if unknown:
        raise MappingError(
            f"{source}: modes.{name} has unknown key(s) "
            f"{', '.join(sorted(unknown))}"
        )

    multipliers = body.get("multipliers") or {}

    if not isinstance(multipliers, dict):
        raise MappingError(
            f"{source}: modes.{name}.multipliers must be a mapping"
        )

    exempt = body.get("exempt") or []

    if not isinstance(exempt, list):
        raise MappingError(f"{source}: modes.{name}.exempt must be a list")

    polls = body.get("polls", True)

    if not isinstance(polls, bool):
        raise MappingError(
            f"{source}: modes.{name}.polls must be true or false, "
            f"got {polls!r}"
        )

    return DriveMode(
        name=name,
        description=str(body.get("description") or ""),
        multipliers={
            cls: _multiplier(value, source, name, cls)
            for cls, value in multipliers.items()
        },
        polls=polls,
        duty=_duty(body.get("duty"), source, name),
        duty_exempt=frozenset(str(c) for c in exempt),
    )


def load_modes(path: Optional[str] = None) -> ModeTable:
    """Read a mode table. Raises MappingError on anything malformed."""
    source = path or DEFAULT_MODE_CONFIG

    try:
        document = yamlsubset.load(source)
    except OSError as exc:
        raise MappingError(f"cannot read mode table {source}: {exc}")

    if not isinstance(document, dict):
        raise MappingError(f"{source}: expected a mapping at the top level")

    unknown = set(document) - {"schema_version", "id", "version", "default",
                               "modes"}

    if unknown:
        raise MappingError(
            f"{source}: unknown top-level key(s) "
            f"{', '.join(sorted(str(k) for k in unknown))}"
        )

    raw_modes = document.get("modes")

    if not isinstance(raw_modes, dict) or not raw_modes:
        raise MappingError(f"{source}: `modes` must be a non-empty mapping")

    modes = {
        name: _mode(name, body, source) for name, body in raw_modes.items()
    }
    default = str(document.get("default") or "normal")

    if default not in modes:
        raise MappingError(
            f"{source}: default mode {default!r} is not defined; "
            f"defined modes are {', '.join(sorted(modes))}"
        )

    return ModeTable(
        id=str(document.get("id") or "drive-modes"),
        version=_positive_int(document.get("version"), source, "version"),
        default=default,
        modes=modes,
        source_path=source,
    )


# ---------------------------------------------------------- arithmetic


def apply_mode(
    classes: Mapping[str, PollingClassDef], mode: DriveMode
) -> Dict[str, PollingClassDef]:
    """
    Rescale polling classes for a mode. Pure - returns new objects.

    One unit means one operation: multiply the period. A multiplier above
    1 polls less often, below 1 more often.
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

        out[name] = PollingClassDef(
            name=cls.name,
            period=cls.period * factor,
            priority=cls.priority,
            stagger=cls.stagger,
        )

    return out
