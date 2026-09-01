"""
What this car physically is, as an input to analytics.

A health system must know whether the component it is evaluating exists.
This one did not: the target car's particulate filter was removed years
ago, and the analysis layer went on reporting DPF restriction baselines,
soot accumulation and differential-pressure health as though a filter
were fitted. Those conclusions are not merely uncertain, they are
impossible - `n47d_dpf_dp` measures an empty pipe.

That cost weeks once already. The soot decode was chased as a scaling bug
through several sessions because nobody had written down that the
hardware was absent; see `docs/DPF_SOOT.md`.

So vehicle configuration becomes a first-class input rather than
tribal knowledge. Analytics asks the profile whether a subsystem is
present, and a conclusion about hardware that is absent - or unknown - is
not drawn at all.

Three states, and the third is the point:

    present   the hardware is fitted; conclusions may be drawn
    absent    the hardware is not fitted; physical conclusions are VOID
    unknown   nobody has said; conclusions are NOT EVALUATED

`unknown` deliberately behaves like `absent` for the purpose of drawing
conclusions, and unlike it for the purpose of reporting: an unconfigured
checkout must not silently start asserting DPF health, and must not
silently claim the filter was removed either.

The profile lives OUTSIDE the repository, under `local/`, because it
describes one specific car. `config/vehicle-profile.example.yaml` is the
committed template. No VIN goes in either: the car is identified by its
stable label, `F10-520d-dev`.
"""

import os
from typing import Any, Dict, Optional, Tuple

from .mapping import yamlsubset

__all__ = [
    "PRESENT",
    "ABSENT",
    "UNKNOWN",
    "VehicleProfile",
    "load_profile",
    "DEFAULT_PROFILE_PATH",
]

PRESENT = "present"
ABSENT = "absent"
UNKNOWN = "unknown"

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Gitignored, because it describes one car. Absent is a valid state.
DEFAULT_PROFILE_PATH = os.path.join(_ROOT, "local", "vehicle-profile.yaml")


class VehicleProfile:
    """
    The hardware configuration of one vehicle, by stable label.

    Deliberately dumb: a label, a few descriptive fields, a subsystem map
    and a list of modifications. It answers one question - does this part
    exist - and does not model the part.
    """

    def __init__(
        self,
        label: str = "",
        model: str = "",
        engine: str = "",
        hardware: Optional[Dict[str, Any]] = None,
        modifications: Tuple[Dict[str, Any], ...] = (),
        source: str = "",
    ):
        self.label = label
        self.model = model
        self.engine = engine
        self._hardware = dict(hardware or {})
        self.modifications = tuple(modifications)
        #: Where this came from, or "" when nothing was loaded. Reports
        #: quote it, so a reader can tell a configured claim from a
        #: default.
        self.source = source

    # -- capability -------------------------------------------------

    def state(self, subsystem: str) -> str:
        """`present`, `absent` or `unknown` for one subsystem."""
        if subsystem not in self._hardware:
            return UNKNOWN

        value = self._hardware[subsystem]

        if isinstance(value, bool):
            return PRESENT if value else ABSENT

        text = str(value).strip().lower()

        if text in ("present", "true", "yes", "fitted"):
            return PRESENT

        if text in ("absent", "false", "no", "removed", "deleted"):
            return ABSENT

        return UNKNOWN

    def has(self, subsystem: str) -> bool:
        """
        True only when the hardware is known to be fitted.

        Fails closed on `unknown`: an unconfigured checkout must not start
        asserting the health of a part nobody has confirmed exists.
        """
        return self.state(subsystem) == PRESENT

    def is_absent(self, subsystem: str) -> bool:
        """True only when the hardware is known NOT to be fitted."""
        return self.state(subsystem) == ABSENT

    def why_not(self, subsystem: str) -> str:
        """One line explaining why a conclusion was withheld."""
        if self.state(subsystem) == ABSENT:
            note = self.modification_note(subsystem)

            return (
                f"VOID: this vehicle has no {subsystem}"
                + (f" ({note})" if note else "")
                + " - the channels still report, but they describe hardware "
                "that is not there"
            )

        return (
            f"NOT EVALUATED: whether this vehicle has a {subsystem} is not "
            "recorded" + (
                f" in {os.path.relpath(self.source, _ROOT)}" if self.source
                else " (no vehicle profile loaded)"
            )
            + " - unknown is not treated as present"
        )

    def modification_note(self, subsystem: str) -> str:
        """The recorded modification touching `subsystem`, if any."""
        for mod in self.modifications:
            kind = str(mod.get("type", ""))

            if subsystem in kind:
                when = mod.get("at") or "date unknown"

                return f"{kind}, {when}"

        return ""

    def fingerprint(self) -> str:
        """
        Deterministic, VIN-free summary of the hardware configuration.

        `subsystem=state,...`, sorted, e.g. `dpf=absent,egr=present`. This
        string is what gets snapshotted onto a run, so it has to be stable
        across processes and orderings - two runs recorded under the same
        configuration must produce byte-identical fingerprints, or a
        change of nothing would look like a change of something.

        Only declared subsystems appear. An empty string means nothing was
        declared, which is different from declaring everything unknown.
        """
        return ",".join(
            f"{name}={self.state(name)}" for name in sorted(self._hardware)
        )

    @classmethod
    def from_fingerprint(cls, label: str, fingerprint: str,
                         source: str = "") -> "VehicleProfile":
        """
        Rebuild a profile from a snapshot taken when a run was recorded.

        The inverse of `fingerprint()`, and the reason it is a flat string
        rather than a nested structure: what is stored on a run has to be
        readable back without carrying a schema along with it.
        """
        hardware: Dict[str, Any] = {}

        for item in (fingerprint or "").split(","):
            if "=" not in item:
                continue

            name, _, state = item.partition("=")
            hardware[name.strip()] = state.strip()

        return cls(label=label, hardware=hardware, source=source)

    @property
    def configured(self) -> bool:
        return bool(self.source)

    def describe(self) -> str:
        if not self.configured:
            return "no vehicle profile loaded - hardware configuration unknown"

        bits = [b for b in (self.label, self.model, self.engine) if b]

        return ", ".join(bits) or self.label or "unlabelled vehicle"

    def __repr__(self) -> str:
        return f"VehicleProfile({self.describe()!r}, hardware={self._hardware})"


def load_profile(path: Optional[str] = None) -> VehicleProfile:
    """
    Read a vehicle profile, or return an empty one.

    A missing file is NOT an error. It is the ordinary state of a fresh
    checkout, of CI, and of anyone analysing someone else's drive file -
    and it produces a profile whose every subsystem is `unknown`, which
    suppresses hardware conclusions rather than inventing them.
    """
    source = path or DEFAULT_PROFILE_PATH

    if not os.path.exists(source):
        return VehicleProfile()

    document = yamlsubset.load(source)

    if not isinstance(document, dict):
        raise ValueError(f"{source}: expected a mapping at the top level")

    vehicle = document.get("vehicle") or {}
    hardware = document.get("hardware") or {}
    mods = document.get("modifications") or []

    if not isinstance(hardware, dict):
        raise ValueError(f"{source}: `hardware` must be a mapping")

    return VehicleProfile(
        label=str(vehicle.get("label", "")),
        model=str(vehicle.get("model", "")),
        engine=str(vehicle.get("engine", "")),
        hardware=hardware,
        modifications=tuple(m for m in mods if isinstance(m, dict)),
        source=source,
    )
