"""
The runtime mapping model.

Everything here is a frozen dataclass built by the loader and then only
read. There is no behaviour on these types beyond trivial accessors: the
decoder, the polling planner and the executor all take model objects as
input, which keeps the data format and the code that acts on it separable.

Vocabulary
----------
mapping file    one YAML document; metadata + one ECU + requests + derived
request         one thing put on the wire; owns 1..n signals
signal          one normalised telemetry channel decoded from a response
derived signal  a channel computed from other signals, never requested
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSIONS = (1,)

#: Where a mapping came from. Extend deliberately - importers key off this.
SOURCE_TYPES = (
    "obd_standard",
    "prg",
    "ediabas",
    "tool32",
    "ista",
    "trace",
    "manual",
    "synthetic",
)

#: Lifecycle state. See docs/MAPPING_RESEARCH.md.
VERIFICATION_STATUS = ("discovered", "candidate", "verified", "rejected")

#: Transports the mapping layer can name. Only `diagnostic` exists today.
TRANSPORTS = ("diagnostic",)

#: Request-level protocols. `obd` is Mode 01 style (service + PID), `uds`
#: is service + 2-byte identifier, `raw` is an explicit payload.
PROTOCOLS = ("obd", "uds", "raw")


@dataclass(frozen=True)
class Provenance:
    """Where a mapping or signal came from. Never affects decoding."""

    type: str = "manual"
    file: Optional[str] = None
    sgbd: Optional[str] = None
    job: Optional[str] = None
    result: Optional[str] = None
    notes: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type, "file": self.file, "sgbd": self.sgbd,
            "job": self.job, "result": self.result, "notes": self.notes,
        }


@dataclass(frozen=True)
class Verification:
    """How far along the discovered -> verified pipeline a mapping is."""

    status: str = "discovered"
    method: Optional[str] = None
    vehicle: Optional[str] = None
    notes: Optional[str] = None

    @property
    def is_verified(self) -> bool:
        return self.status == "verified"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status, "method": self.method,
            "vehicle": self.vehicle, "notes": self.notes,
        }


@dataclass(frozen=True)
class Display:
    """Presentation hints. The dashboard consumes these verbatim."""

    digits: int = 0
    lo: float = 0
    hi: float = 100
    #: role -> config key, e.g. {"hi": "tank"} takes the max from runtime config
    from_config: Tuple[Tuple[str, str], ...] = ()

    def resolve(self, config: Dict[str, Any]) -> "Display":
        if not self.from_config:
            return self

        lo, hi = self.lo, self.hi

        for role, name in self.from_config:
            if name not in config:
                continue

            if role == "lo":
                lo = float(config[name])
            elif role == "hi":
                hi = float(config[name])

        return Display(digits=self.digits, lo=lo, hi=hi)


@dataclass(frozen=True)
class Capability:
    """One thing an ECU must be able to do for a mapping to apply."""

    kind: str
    value: Any

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "value": self.value}


@dataclass(frozen=True)
class Target:
    """
    Where a request goes.

    `address` is a fixed diagnostic address taken from the mapping file.
    `name` is a late-bound target (`discovered_engine`) resolved by the
    application once the ECU scan has run.
    """

    address: Optional[int] = None
    name: Optional[str] = None

    @property
    def is_dynamic(self) -> bool:
        return self.address is None

    def resolve(self, targets: Dict[str, int]) -> Optional[int]:
        if self.address is not None:
            return self.address

        if self.name is None:
            return None

        return targets.get(self.name)

    def describe(self) -> str:
        if self.address is not None:
            return f"0x{self.address:02X}"

        return self.name or "?"


@dataclass(frozen=True)
class EcuDef:
    """Identity of the ECU a mapping file targets."""

    family: str = "unknown"
    target: Target = field(default_factory=Target)
    sgbd: Optional[str] = None
    variant: Optional[str] = None
    hardware: Optional[str] = None
    software: Optional[str] = None
    match: Tuple[Capability, ...] = ()


@dataclass(frozen=True)
class Decode:
    """
    A primitive read plus the data-only transformations applied to it.

    Evaluation order is fixed and documented, because preserving it byte
    for byte is what lets existing OBD formulas move into data without the
    last float digit moving with them:

        raw = primitive(window)
        raw is in `invalid`               -> None
        enum                              -> string
        lookup                            -> piecewise-linear interpolation
        otherwise  ((raw + pre_add) * scale) / divide + add
        result outside valid_min/max      -> None
        round to `round` digits
    """

    type: str
    offset: int = 0
    length: Optional[int] = None
    bit: Optional[int] = None
    mask: Optional[int] = None
    shift: Optional[int] = None
    pre_add: float = 0.0
    scale: float = 1.0
    divide: float = 1.0
    add: float = 0.0
    round: Optional[int] = 3
    enum: Optional[Tuple[Tuple[int, str], ...]] = None
    enum_default: Optional[str] = None
    lookup: Optional[Tuple[Tuple[float, float], ...]] = None
    invalid: Tuple[int, ...] = ()
    valid_min: Optional[float] = None
    valid_max: Optional[float] = None
    encoding: str = "ascii"

    @property
    def is_numeric(self) -> bool:
        return self.enum is None and self.type not in ("bytes", "ascii")


@dataclass(frozen=True)
class SignalDef:
    """One normalised telemetry channel."""

    key: str
    request_id: str
    label: str
    unit: str
    decode: Decode
    display: Display = field(default_factory=Display)
    source_name: Optional[str] = None
    provenance: Provenance = field(default_factory=Provenance)
    verification: Verification = field(default_factory=Verification)
    order: int = 0

    @property
    def is_numeric(self) -> bool:
        return self.decode.is_numeric


@dataclass(frozen=True)
class ResponseSpec:
    """
    What a valid positive response to a request looks like.

    `payload_offset` is where signal offsets are measured from, so a signal
    never has to know how long the service echo in front of it was.
    """

    prefix: Tuple[int, ...] = ()
    payload_offset: int = 0
    data_length: Optional[int] = None
    min_length: int = 0

    @property
    def total_length(self) -> Optional[int]:
        if self.data_length is None:
            return None

        return self.payload_offset + self.data_length


@dataclass(frozen=True)
class RequestDef:
    """One diagnostic exchange, owning every signal decoded from its reply."""

    id: str
    mapping_id: str
    protocol: str
    target: Target
    response: ResponseSpec
    signals: Tuple[SignalDef, ...]
    transport: str = "diagnostic"
    service: Optional[int] = None
    pid: Optional[int] = None
    did: Optional[int] = None
    payload: Optional[Tuple[int, ...]] = None
    polling_class: str = "slow"
    requires: Tuple[Capability, ...] = ()
    timeout: Optional[float] = None
    provenance: Provenance = field(default_factory=Provenance)
    verification: Verification = field(default_factory=Verification)
    order: int = 0

    @property
    def signal_keys(self) -> Tuple[str, ...]:
        return tuple(s.key for s in self.signals)


#: Derived operations. Deliberately a closed set of named arithmetic - not
#: an expression language, and never eval.
DERIVED_OPERATIONS = (
    "linear",          # value * scale / divide + add
    "subtract_scale",  # (value - reference) * scale / divide + add
    "divide_scale",    # (value / divide) * scale + add
    "sum",             # sum(inputs) * scale / divide + add
    "product",         # value * reference * scale / divide + add
    "ratio",           # (value / reference) * scale + add
)


@dataclass(frozen=True)
class DerivedDef:
    """A channel computed from other signals rather than read from an ECU."""

    key: str
    mapping_id: str
    label: str
    unit: str
    operation: str
    inputs: Tuple[Tuple[str, str], ...]          # role -> source signal key
    display: Display = field(default_factory=Display)
    fallback: Tuple[Tuple[str, float], ...] = () # role -> value if absent
    scale: float = 1.0
    scale_config: Optional[str] = None           # scale taken from runtime config
    divide: float = 1.0
    add: float = 0.0
    pre_add: float = 0.0
    round: Optional[int] = None
    trigger: Tuple[str, ...] = ()                # recompute when these are fresh
    position: str = "last"                       # ordering hint for the UI
    provenance: Provenance = field(default_factory=Provenance)
    verification: Verification = field(default_factory=Verification)
    order: int = 0

    def input_map(self) -> Dict[str, str]:
        return dict(self.inputs)

    def fallback_map(self) -> Dict[str, float]:
        return dict(self.fallback)


@dataclass(frozen=True)
class PollingClassDef:
    """
    How often requests in a class run.

    `kind` is one of:
        cycles   - every Nth poll-loop cycle (the legacy fast/slow model)
        hz       - N times per second, wall clock
        seconds  - once every N seconds, wall clock
    """

    name: str
    kind: str = "cycles"
    value: float = 1.0
    priority: int = 0

    @property
    def period(self) -> Optional[float]:
        if self.kind == "hz":
            return 1.0 / self.value if self.value else None

        if self.kind == "seconds":
            return self.value

        return None


@dataclass(frozen=True)
class MappingFile:
    """One loaded mapping document."""

    schema_version: int
    id: str
    source_path: str
    description: str = ""
    version: str = "0"
    production: bool = True
    ecu: EcuDef = field(default_factory=EcuDef)
    requests: Tuple[RequestDef, ...] = ()
    derived: Tuple[DerivedDef, ...] = ()
    polling_classes: Tuple[PollingClassDef, ...] = ()
    provenance: Provenance = field(default_factory=Provenance)
    verification: Verification = field(default_factory=Verification)

    @property
    def signals(self) -> List[SignalDef]:
        return [s for r in self.requests for s in r.signals]
