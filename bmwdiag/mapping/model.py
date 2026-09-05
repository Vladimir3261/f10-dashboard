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
    #: Request ids in this file that PROVE the `diagnostic_profile` the
    #: match requires: the ECU is compatible when any one of them answers
    #: in its declared shape. Explicit, in order - never "the first
    #: request in the file", which made a reorder change what was probed.
    probe: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Decode:
    """
    A primitive read plus the data-only transformations applied to it.

    Evaluation order is fixed and documented, because preserving it byte
    for byte is what lets existing OBD formulas move into data without the
    last float digit moving with them:

        raw = primitive(window)
        raw is in `invalid`               -> quality 'sentinel'
        raw is in `saturated`             -> quality 'saturated'
        enum                              -> string
        lookup                            -> piecewise-linear interpolation
        otherwise  ((raw + pre_add) * scale) / divide + add
        result outside valid_min/max      -> quality 'clipped'
        round to `round` digits

    `invalid` and `saturated` are raw-domain: they list bit patterns, not
    decoded values, so a sentinel stays a sentinel if the scale is ever
    corrected. They label the reading rather than discard it - the narrow
    `decode_value` still returns None for anything not usable.
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
    saturated: Tuple[int, ...] = ()
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
    #: Whether recorded runs should store this channel.
    #:
    #: False means "decode and display, but do not persist". The channel is
    #: still read (it usually shares a request with one that matters, so it
    #: costs nothing on the wire) and still appears on the dashboard, but no
    #: row is written for it. For a channel whose finding is that it never
    #: changes, storing millions of identical rows adds no information.
    log: bool = True
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
    #: Payloads sent once per session before this request is first polled.
    #: This is how a BMW F-series dynamic measurement is represented: the
    #: `2C 03 F3 03` clear and the `2C 01 F3 03 <src> <pos> <width>` define
    #: are setup frames, and the polled request is the plain `22 F3 03`.
    #: A sequence is data, never one fabricated identifier.
    setup: Tuple[Tuple[int, ...], ...] = ()
    polling_class: str = "slow"
    #: Requests sharing a pair tag inside a STAGGERED class are sent in
    #: the same firing instead of consecutive round-robin slots, so an
    #: actual/setpoint pair lands in one poll cycle rather than seconds
    #: apart. "" means unpaired. See bmwdiag/mapping/polling.py.
    polling_pair: str = ""
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
    #: As SignalDef.log - compute and display, but do not persist.
    log: bool = True
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

    ONE unit: `period`, in seconds of wall clock. There used to be three
    interchangeable spellings - `hz`, `seconds` and `every` (poll-loop
    cycles) - which was two too many. `hz` and `seconds` were the same
    thing written differently and both collapsed to a period internally;
    `every` was worse than redundant, because it silently rescaled every
    class when --rate changed, so the same mapping file meant different
    sample rates on different launches.

    Seconds rather than Hz because the declared rates are mostly slow:
    `{seconds: 60}` reads cleanly where `{hz: 0.0166...}` does not, and
    for a staggered class "0.5 s per member" is honest where "2 Hz"
    invites reading it as the per-channel rate, which it is not.
    """

    name: str
    #: Seconds between firings. The fastest useful value is the poll
    #: loop's own interval - asking for less just means "every cycle".
    period: float = 1.0
    priority: int = 0
    #: When true, the class round-robins its members: at most one member
    #: is due per firing, cycling through them. This bounds the per-cycle
    #: cost of an expensive group (e.g. the multi-frame F303 dynamic
    #: reads) to a single member, instead of firing all of them at once
    #: and stalling the fast channels.
    #:
    #: NOTE the arithmetic: `period` is then the gap between FIRINGS OF
    #: THE CLASS, so one member refreshes every period x members.
    stagger: bool = False

    @property
    def hz(self) -> float:
        """Firings per second - for display, never for scheduling."""
        return 1.0 / self.period if self.period else 0.0


@dataclass(frozen=True)
class MappingFile:
    """One loaded mapping document."""

    schema_version: int
    id: str
    source_path: str
    description: str = ""
    #: Data version of THIS mapping file. A positive integer that starts
    #: at 1 and is incremented by one on every change to the file's
    #: content (a decode, scale, DID, added/removed signal - anything that
    #: alters what the file produces). It tracks the *mapping data*, never
    #: the code: editing the loader or live.py never changes it. The
    #: version is stamped onto every recorded sample the file decodes, so
    #: a dataset can always be tied back to the exact mapping revision that
    #: produced it. See docs/DATA_VERSIONING.md. The loader requires it.
    version: int = 1
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
