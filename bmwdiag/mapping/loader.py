"""
Mapping file loader and validator.

Everything a mapping file can get wrong is caught here, with a message
naming the file and the path inside it. Nothing downstream re-checks:
by the time the registry sees a `MappingFile`, offsets fit their response,
decoder types exist, derived inputs resolve and keys are unique.
"""

import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import yamlsubset
from .decoder import PRIMITIVES, primitive_width
from .errors import (
    DuplicateRequestError,
    DuplicateSignalError,
    InvalidEnumError,
    InvalidFieldError,
    InvalidLengthError,
    InvalidOffsetError,
    MappingError,
    MissingFieldError,
    UnknownDecoderError,
    UnknownDerivedInputError,
    UnsupportedSchemaVersion,
)
from .model import (
    DERIVED_OPERATIONS,
    PROTOCOLS,
    SCHEMA_VERSIONS,
    SOURCE_TYPES,
    TRANSPORTS,
    VERIFICATION_STATUS,
    Capability,
    Decode,
    DerivedDef,
    Display,
    EcuDef,
    MappingFile,
    PollingClassDef,
    Provenance,
    RequestDef,
    ResponseSpec,
    SignalDef,
    Target,
    Verification,
)

__all__ = ["load_file", "load_text", "load_tree", "iter_mapping_files"]

MAPPING_SUFFIXES = (".yaml", ".yml")

#: Late-bound targets the application is expected to supply at runtime.
KNOWN_DYNAMIC_TARGETS = ("discovered_engine", "discovered_gateway")


# ------------------------------------------------------------ primitives


def _require(
    data: Dict[str, Any], key: str, source: str, path: str
) -> Any:
    if not isinstance(data, dict) or key not in data or data[key] is None:
        raise MissingFieldError(f"missing required field {key!r}", source, path)

    return data[key]


def _as_dict(value: Any, source: str, path: str) -> Dict[str, Any]:
    if value is None:
        return {}

    if not isinstance(value, dict):
        raise InvalidFieldError(
            f"expected a mapping, got {type(value).__name__}", source, path
        )

    return value


def _as_int(value: Any, source: str, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidFieldError(
            f"expected an integer, got {value!r}", source, path
        )

    return value


def _mapping_version(meta: Dict[str, Any], source: str) -> int:
    """
    The mapping file's data version: a required positive integer.

    Accepts an int (`version: 1`) or a digit string (`version: "1"`) so
    existing files migrate cleanly, but the stored value is always an int.
    Missing, non-integer, or non-positive versions are a load error - every
    mapping must declare where it sits in its own revision history, because
    that number is stamped onto every sample it decodes. See
    docs/DATA_VERSIONING.md.
    """
    raw = _require(meta, "version", source, "mapping")

    if isinstance(raw, bool):
        raise InvalidFieldError(
            f"mapping.version must be a positive integer, got {raw!r}",
            source, "mapping.version",
        )

    if isinstance(raw, str) and raw.isdigit():
        raw = int(raw)

    if not isinstance(raw, int) or raw < 1:
        raise InvalidFieldError(
            f"mapping.version must be a positive integer, got {raw!r}",
            source, "mapping.version",
        )

    return raw


def _as_bool(value: Any, source: str, path: str, default: bool) -> bool:
    """A YAML boolean, strictly. `log: maybe` is a mistake, not a truthy value."""
    if value is None:
        return default

    if not isinstance(value, bool):
        raise InvalidFieldError(
            f"expected true or false, got {value!r}", source, path
        )

    return value


def _as_float(value: Any, source: str, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidFieldError(
            f"expected a number, got {value!r}", source, path
        )

    return float(value)


def _as_number(value: Any, source: str, path: str) -> Any:
    """A number, keeping int as int so display bounds round-trip verbatim."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidFieldError(
            f"expected a number, got {value!r}", source, path
        )

    return value


def _as_str(value: Any, source: str, path: str) -> str:
    if not isinstance(value, str):
        raise InvalidFieldError(
            f"expected a string, got {value!r}", source, path
        )

    return value


def _byte_seq(value: Any, source: str, path: str) -> Tuple[int, ...]:
    """Accept [0x41, 0x0C] or the hex-string shorthand "41 0C"."""
    if isinstance(value, str):
        text = value.replace(",", " ").split()

        try:
            return tuple(int(part, 16) for part in text)
        except ValueError:
            raise InvalidFieldError(
                f"not a hex byte string: {value!r}", source, path
            )

    if not isinstance(value, (list, tuple)):
        raise InvalidFieldError(
            f"expected a byte list or hex string, got {value!r}", source, path
        )

    out: List[int] = []

    for i, item in enumerate(value):
        byte = _as_int(item, source, f"{path}[{i}]")

        if not 0 <= byte <= 0xFF:
            raise InvalidFieldError(
                f"byte out of range: {byte}", source, f"{path}[{i}]"
            )

        out.append(byte)

    return tuple(out)


def _enum_value(
    value: Any, allowed: Sequence[str], source: str, path: str
) -> str:
    text = _as_str(value, source, path)

    if text not in allowed:
        raise InvalidEnumError(
            f"{text!r} is not one of {', '.join(allowed)}", source, path
        )

    return text


# --------------------------------------------------------- shared blocks


def _provenance(raw: Any, source: str, path: str, base: Provenance) -> Provenance:
    data = _as_dict(raw, source, path)

    if not data:
        return base

    kind = data.get("type", base.type)

    return Provenance(
        type=_enum_value(kind, SOURCE_TYPES, source, f"{path}.type"),
        file=data.get("file", base.file),
        sgbd=data.get("sgbd", base.sgbd),
        job=data.get("job", base.job),
        result=data.get("result", base.result),
        notes=data.get("notes", base.notes),
    )


def _verification(
    raw: Any, source: str, path: str, base: Verification
) -> Verification:
    data = _as_dict(raw, source, path)

    if not data:
        return base

    status = data.get("status", base.status)

    return Verification(
        status=_enum_value(
            status, VERIFICATION_STATUS, source, f"{path}.status"
        ),
        method=data.get("method", base.method),
        vehicle=data.get("vehicle", base.vehicle),
        notes=data.get("notes", base.notes),
    )


def _display(raw: Any, source: str, path: str) -> Display:
    data = _as_dict(raw, source, path)
    digits = data.get("digits", 0)
    from_config: List[Tuple[str, str]] = []
    bounds: Dict[str, float] = {"lo": 0.0, "hi": 100.0}

    for role, names in (("lo", ("min", "lo")), ("hi", ("max", "hi"))):
        for name in names:
            if name not in data:
                continue

            value = data[name]

            if isinstance(value, dict):
                config_key = _require(value, "config", source, f"{path}.{name}")
                from_config.append((role, _as_str(
                    config_key, source, f"{path}.{name}.config"
                )))
                bounds[role] = _as_number(
                    value.get("default", bounds[role]), source,
                    f"{path}.{name}.default",
                )
            else:
                bounds[role] = _as_number(value, source, f"{path}.{name}")

            break

    return Display(
        digits=_as_int(digits, source, f"{path}.digits"),
        lo=bounds["lo"],
        hi=bounds["hi"],
        from_config=tuple(from_config),
    )


def _capabilities(raw: Any, source: str, path: str) -> Tuple[Capability, ...]:
    data = _as_dict(raw, source, path)
    out: List[Capability] = []

    for kind, value in data.items():
        out.append(Capability(kind=str(kind), value=value))

    return tuple(out)


def _target(raw: Any, source: str, path: str) -> Target:
    if raw is None:
        return Target()

    if isinstance(raw, int) and not isinstance(raw, bool):
        if not 0 <= raw <= 0xFF:
            raise InvalidFieldError(
                f"diagnostic address out of range: {raw}", source, path
            )

        return Target(address=raw)

    if isinstance(raw, str):
        return Target(name=raw)

    data = _as_dict(raw, source, path)

    if "address" in data and data["address"] is not None:
        return Target(address=_as_int(data["address"], source, f"{path}.address"))

    if "name" in data and data["name"] is not None:
        return Target(name=_as_str(data["name"], source, f"{path}.name"))

    raise MissingFieldError("target needs an address or a name", source, path)


# --------------------------------------------------------------- decoder


def _decode(raw: Any, source: str, path: str) -> Decode:
    data = _as_dict(raw, source, path)
    kind = _as_str(_require(data, "type", source, path), source, f"{path}.type")

    if kind not in PRIMITIVES:
        raise UnknownDecoderError(
            f"unknown decoder type {kind!r}; known types: "
            + ", ".join(sorted(PRIMITIVES)),
            source, f"{path}.type",
        )

    offset = _as_int(data.get("offset", 0), source, f"{path}.offset")

    if offset < 0:
        raise InvalidOffsetError(f"negative offset {offset}", source, f"{path}.offset")

    length = data.get("length")

    if length is not None:
        length = _as_int(length, source, f"{path}.length")

        if length <= 0:
            raise InvalidLengthError(
                f"length must be positive, got {length}", source, f"{path}.length"
            )

    enum: Optional[Tuple[Tuple[int, str], ...]] = None

    if "enum" in data and data["enum"] is not None:
        table = data["enum"]

        if not isinstance(table, dict):
            raise InvalidEnumError(
                "enum must be a mapping of integer -> name", source, f"{path}.enum"
            )

        pairs: List[Tuple[int, str]] = []

        for key, name in table.items():
            if isinstance(key, bool) or not isinstance(key, int):
                raise InvalidEnumError(
                    f"enum keys must be integers, got {key!r}",
                    source, f"{path}.enum",
                )

            if not isinstance(name, str):
                raise InvalidEnumError(
                    f"enum values must be strings, got {name!r}",
                    source, f"{path}.enum[{key}]",
                )

            pairs.append((key, name))

        if not pairs:
            raise InvalidEnumError("enum table is empty", source, f"{path}.enum")

        enum = tuple(pairs)

    lookup: Optional[Tuple[Tuple[float, float], ...]] = None

    if "lookup" in data and data["lookup"] is not None:
        table = data["lookup"]

        if not isinstance(table, list) or len(table) < 2:
            raise InvalidEnumError(
                "lookup must be a list of at least two [raw, value] pairs",
                source, f"{path}.lookup",
            )

        points: List[Tuple[float, float]] = []

        for i, item in enumerate(table):
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise InvalidEnumError(
                    f"lookup entry {i} must be a [raw, value] pair",
                    source, f"{path}.lookup",
                )

            points.append((
                _as_float(item[0], source, f"{path}.lookup[{i}][0]"),
                _as_float(item[1], source, f"{path}.lookup[{i}][1]"),
            ))

        if any(points[i][0] >= points[i + 1][0] for i in range(len(points) - 1)):
            raise InvalidEnumError(
                "lookup raw values must be strictly increasing",
                source, f"{path}.lookup",
            )

        lookup = tuple(points)

    if enum is not None and lookup is not None:
        raise InvalidFieldError(
            "a signal cannot use both enum and lookup", source, path
        )

    bit = data.get("bit")

    if bit is not None:
        bit = _as_int(bit, source, f"{path}.bit")

    mask = data.get("mask")

    if mask is not None:
        mask = _as_int(mask, source, f"{path}.mask")

        if mask <= 0:
            raise InvalidFieldError(
                f"mask must be positive, got {mask}", source, f"{path}.mask"
            )

    shift = data.get("shift")

    if shift is not None:
        shift = _as_int(shift, source, f"{path}.shift")

    invalid = data.get("invalid") or ()

    if invalid and not isinstance(invalid, (list, tuple)):
        invalid = [invalid]

    rounding = data.get("round", 3)

    if rounding is not None:
        rounding = _as_int(rounding, source, f"{path}.round")

    decode = Decode(
        type=kind,
        offset=offset,
        length=length,
        bit=bit,
        mask=mask,
        shift=shift,
        pre_add=_as_float(data.get("pre_add", 0.0), source, f"{path}.pre_add"),
        scale=_as_float(data.get("scale", 1.0), source, f"{path}.scale"),
        divide=_as_float(data.get("divide", 1.0), source, f"{path}.divide"),
        add=_as_float(data.get("add", 0.0), source, f"{path}.add"),
        round=rounding,
        enum=enum,
        enum_default=data.get("enum_default"),
        lookup=lookup,
        invalid=tuple(
            _as_int(v, source, f"{path}.invalid") for v in invalid
        ),
        valid_min=(
            None if data.get("valid_min") is None
            else _as_float(data["valid_min"], source, f"{path}.valid_min")
        ),
        valid_max=(
            None if data.get("valid_max") is None
            else _as_float(data["valid_max"], source, f"{path}.valid_max")
        ),
        encoding=_as_str(data.get("encoding", "ascii"), source, f"{path}.encoding"),
    )

    if decode.divide == 0.0:
        raise InvalidFieldError("divide must not be zero", source, f"{path}.divide")

    #
    # primitive_width raises for a variable-width type with no length, so
    # an unusable decoder is rejected at load time, not mid-drive.
    #
    try:
        width = primitive_width(decode)
    except MappingError as exc:
        raise type(exc)(exc.message, source, path)

    if decode.type == "bit":
        index = decode.bit or 0

        if not 0 <= index < width * 8:
            raise InvalidLengthError(
                f"bit {index} is outside a {width}-byte window",
                source, f"{path}.bit",
            )

    if decode.type == "bitfield" and decode.mask is not None:
        if decode.mask >= 1 << (width * 8):
            raise InvalidLengthError(
                f"mask 0x{decode.mask:X} does not fit in a {width}-byte window",
                source, f"{path}.mask",
            )

    return decode


# ---------------------------------------------------------------- signal


def _signal(
    key: str,
    raw: Any,
    request_id: str,
    source: str,
    path: str,
    base_provenance: Provenance,
    base_verification: Verification,
    order: int,
) -> SignalDef:
    data = _as_dict(raw, source, path)

    return SignalDef(
        key=key,
        request_id=request_id,
        label=_as_str(data.get("label", key), source, f"{path}.label"),
        unit=_as_str(data.get("unit", ""), source, f"{path}.unit"),
        decode=_decode(
            _require(data, "decode", source, path), source, f"{path}.decode"
        ),
        display=_display(data.get("display"), source, f"{path}.display"),
        source_name=data.get("source_name"),
        log=_as_bool(data.get("log"), source, f"{path}.log", True),
        provenance=_provenance(
            data.get("source"), source, f"{path}.source", base_provenance
        ),
        verification=_verification(
            data.get("verification"), source, f"{path}.verification",
            base_verification,
        ),
        order=order,
    )


# --------------------------------------------------------------- request


def _response_spec(
    raw: Any,
    protocol: str,
    service: Optional[int],
    pid: Optional[int],
    did: Optional[int],
    source: str,
    path: str,
) -> ResponseSpec:
    data = _as_dict(raw, source, path)

    if "prefix" in data and data["prefix"] is not None:
        prefix = _byte_seq(data["prefix"], source, f"{path}.prefix")
    elif protocol == "obd" and service is not None and pid is not None:
        prefix = ((service + 0x40) & 0xFF, pid)
    elif protocol == "uds" and service is not None and did is not None:
        prefix = ((service + 0x40) & 0xFF, (did >> 8) & 0xFF, did & 0xFF)
    elif service is not None:
        prefix = (((service + 0x40) & 0xFF),)
    else:
        prefix = ()

    if "payload_offset" in data and data["payload_offset"] is not None:
        payload_offset = _as_int(
            data["payload_offset"], source, f"{path}.payload_offset"
        )
    else:
        payload_offset = len(prefix)

    if payload_offset < 0:
        raise InvalidOffsetError(
            f"negative payload_offset {payload_offset}",
            source, f"{path}.payload_offset",
        )

    data_length = data.get("data_length", data.get("length"))

    if data_length is not None:
        data_length = _as_int(data_length, source, f"{path}.data_length")

        if data_length <= 0:
            raise InvalidLengthError(
                f"data_length must be positive, got {data_length}",
                source, f"{path}.data_length",
            )

    min_length = _as_int(
        data.get("min_length", 0), source, f"{path}.min_length"
    )

    return ResponseSpec(
        prefix=prefix,
        payload_offset=payload_offset,
        data_length=data_length,
        min_length=min_length,
    )


def _request(
    request_id: str,
    raw: Any,
    mapping_id: str,
    defaults: Dict[str, Any],
    source: str,
    path: str,
    base_provenance: Provenance,
    base_verification: Verification,
    order: int,
    seen_signals: Dict[str, str],
) -> RequestDef:
    data = _as_dict(raw, source, path)

    def field(name: str, fallback: Any = None) -> Any:
        if name in data and data[name] is not None:
            return data[name]

        if name in defaults and defaults[name] is not None:
            return defaults[name]

        return fallback

    protocol = _enum_value(
        field("protocol", "obd"), PROTOCOLS, source, f"{path}.protocol"
    )
    transport = _enum_value(
        field("transport", "diagnostic"), TRANSPORTS, source, f"{path}.transport"
    )

    pid = field("pid")
    pid = None if pid is None else _as_int(pid, source, f"{path}.pid")

    did = field("did")
    did = None if did is None else _as_int(did, source, f"{path}.did")

    service = field("service", 0x01 if protocol == "obd" else None)
    service = None if service is None else _as_int(service, source, f"{path}.service")

    payload = field("payload")
    payload = (
        None if payload is None else _byte_seq(payload, source, f"{path}.payload")
    )

    #
    # `setup:` is an ordered list of payloads sent once per session before
    # the request itself is first polled - the representation for a
    # define-then-read sequence such as the F-series dynamic measurement
    # (`2C 03 F3 03`, `2C 01 F3 03 ...`, then poll `22 F3 03`).
    #
    setup_raw = data.get("setup")
    setup: Tuple[Tuple[int, ...], ...] = ()

    if setup_raw is not None:
        if not isinstance(setup_raw, (list, tuple)):
            raise InvalidFieldError(
                "setup must be a list of payloads", source, f"{path}.setup"
            )

        frames: List[Tuple[int, ...]] = []

        for i, frame in enumerate(setup_raw):
            frame_bytes = _byte_seq(frame, source, f"{path}.setup[{i}]")

            if not frame_bytes:
                raise InvalidFieldError(
                    "a setup payload cannot be empty", source, f"{path}.setup[{i}]"
                )

            frames.append(frame_bytes)

        setup = tuple(frames)

    if payload is None and service is None:
        raise MissingFieldError(
            "a request needs a service byte or an explicit payload", source, path
        )

    if protocol == "obd" and pid is None and payload is None:
        raise MissingFieldError(
            "an obd request needs a pid or an explicit payload", source, path
        )

    if protocol == "uds" and did is None and payload is None:
        raise MissingFieldError(
            "a uds request needs a did or an explicit payload", source, path
        )

    if pid is not None and not 0 <= pid <= 0xFF:
        raise InvalidFieldError(f"pid out of range: {pid}", source, f"{path}.pid")

    if did is not None and not 0 <= did <= 0xFFFF:
        raise InvalidFieldError(f"did out of range: {did}", source, f"{path}.did")

    target = _target(field("target"), source, f"{path}.target")

    if target.address is None and target.name is None:
        raise MissingFieldError("request has no target", source, f"{path}.target")

    response = _response_spec(
        data.get("response"), protocol, service, pid, did, source, f"{path}.response"
    )

    polling = data.get("polling", defaults.get("polling"))
    polling_class = "slow"

    if isinstance(polling, dict):
        polling_class = _as_str(
            polling.get("class", "slow"), source, f"{path}.polling.class"
        )
    elif isinstance(polling, str):
        polling_class = polling
    elif polling is not None:
        raise InvalidFieldError(
            "polling must be a class name or a mapping", source, f"{path}.polling"
        )

    provenance = _provenance(
        data.get("source"), source, f"{path}.source", base_provenance
    )
    verification = _verification(
        data.get("verification"), source, f"{path}.verification", base_verification
    )

    if "requires" in data and data["requires"] is not None:
        requires = _capabilities(
            _as_dict(data["requires"], source, f"{path}.requires").get("capability"),
            source, f"{path}.requires.capability",
        )
    elif protocol == "obd" and pid is not None:
        #
        # Standard OBD gates itself: a mapped PID is usable exactly when
        # the ECU advertises it in the Mode 01 support bitmask.
        #
        requires = (Capability("obd_mode01_pid", pid),)
    else:
        requires = ()

    signals_raw = _as_dict(
        _require(data, "signals", source, path), source, f"{path}.signals"
    )

    if not signals_raw:
        raise MissingFieldError(
            "a request must define at least one signal", source, f"{path}.signals"
        )

    signals: List[SignalDef] = []

    for index, (key, spec) in enumerate(signals_raw.items()):
        key = str(key)

        if key in seen_signals:
            raise DuplicateSignalError(
                f"signal key {key!r} is already defined by "
                f"{seen_signals[key]}",
                source, f"{path}.signals.{key}",
            )

        seen_signals[key] = f"{source}:{request_id}"

        signal = _signal(
            key, spec, request_id, source, f"{path}.signals.{key}",
            provenance, verification, index,
        )

        #
        # Offsets are validated against the declared response window here
        # so a truncated mapping fails at load, not on the first reply.
        #
        if response.data_length is not None:
            width = primitive_width(signal.decode)
            end = signal.decode.offset + width

            if end > response.data_length:
                raise InvalidOffsetError(
                    f"signal {key!r} reads bytes {signal.decode.offset}..{end} "
                    f"but the response carries only {response.data_length}",
                    source, f"{path}.signals.{key}.decode.offset",
                )

        signals.append(signal)

    timeout = data.get("timeout")

    return RequestDef(
        id=request_id,
        mapping_id=mapping_id,
        protocol=protocol,
        transport=transport,
        target=target,
        response=response,
        signals=tuple(signals),
        service=service,
        pid=pid,
        did=did,
        payload=payload,
        setup=setup,
        polling_class=polling_class,
        requires=requires,
        timeout=None if timeout is None else _as_float(
            timeout, source, f"{path}.timeout"
        ),
        provenance=provenance,
        verification=verification,
        order=order,
    )


# --------------------------------------------------------------- derived


def _derived(
    key: str,
    raw: Any,
    mapping_id: str,
    source: str,
    path: str,
    base_provenance: Provenance,
    base_verification: Verification,
    order: int,
) -> DerivedDef:
    data = _as_dict(raw, source, path)
    operation = _as_str(
        _require(data, "operation", source, path), source, f"{path}.operation"
    )

    if operation not in DERIVED_OPERATIONS:
        raise InvalidEnumError(
            f"unknown derived operation {operation!r}; known operations: "
            + ", ".join(DERIVED_OPERATIONS),
            source, f"{path}.operation",
        )

    inputs_raw = _as_dict(
        _require(data, "inputs", source, path), source, f"{path}.inputs"
    )

    if not inputs_raw:
        raise MissingFieldError(
            "a derived signal needs at least one input", source, f"{path}.inputs"
        )

    inputs = tuple(
        (str(role), _as_str(name, source, f"{path}.inputs.{role}"))
        for role, name in inputs_raw.items()
    )

    fallback_raw = _as_dict(data.get("fallback"), source, f"{path}.fallback")
    roles = {role for role, _ in inputs}

    for role in fallback_raw:
        if str(role) not in roles:
            raise UnknownDerivedInputError(
                f"fallback names role {role!r}, which is not an input",
                source, f"{path}.fallback",
            )

    fallback = tuple(
        (str(role), _as_float(value, source, f"{path}.fallback.{role}"))
        for role, value in fallback_raw.items()
    )

    scale_raw = data.get("scale", 1.0)
    scale_config: Optional[str] = None

    if isinstance(scale_raw, dict):
        scale_config = _as_str(
            _require(scale_raw, "config", source, f"{path}.scale"),
            source, f"{path}.scale.config",
        )
        scale = _as_float(scale_raw.get("default", 1.0), source, f"{path}.scale.default")
    else:
        scale = _as_float(scale_raw, source, f"{path}.scale")

    divide = _as_float(data.get("divide", 1.0), source, f"{path}.divide")

    if divide == 0.0:
        raise InvalidFieldError("divide must not be zero", source, f"{path}.divide")

    trigger_raw = data.get("trigger")

    if trigger_raw is None:
        #
        # By default a derived signal recomputes when every input that has
        # no fallback brought a fresh reading.
        #
        covered = {role for role, _ in fallback}
        trigger = tuple(
            name for role, name in inputs if role not in covered
        )
    elif isinstance(trigger_raw, str):
        trigger = (trigger_raw,)
    elif isinstance(trigger_raw, list):
        trigger = tuple(
            _as_str(item, source, f"{path}.trigger") for item in trigger_raw
        )
    else:
        raise InvalidFieldError(
            "trigger must be a signal key or a list of them",
            source, f"{path}.trigger",
        )

    position = data.get("position", "last")

    if position not in ("first", "last"):
        raise InvalidEnumError(
            f"position must be 'first' or 'last', got {position!r}",
            source, f"{path}.position",
        )

    rounding = data.get("round")

    if rounding is not None:
        rounding = _as_int(rounding, source, f"{path}.round")

    return DerivedDef(
        key=key,
        mapping_id=mapping_id,
        label=_as_str(data.get("label", key), source, f"{path}.label"),
        unit=_as_str(data.get("unit", ""), source, f"{path}.unit"),
        operation=operation,
        inputs=inputs,
        display=_display(data.get("display"), source, f"{path}.display"),
        fallback=fallback,
        scale=scale,
        scale_config=scale_config,
        divide=divide,
        add=_as_float(data.get("add", 0.0), source, f"{path}.add"),
        pre_add=_as_float(data.get("pre_add", 0.0), source, f"{path}.pre_add"),
        round=rounding,
        log=_as_bool(data.get("log"), source, f"{path}.log", True),
        trigger=trigger,
        position=position,
        provenance=_provenance(
            data.get("source"), source, f"{path}.source", base_provenance
        ),
        verification=_verification(
            data.get("verification"), source, f"{path}.verification",
            base_verification,
        ),
        order=order,
    )


# ------------------------------------------------------------ whole file


def _polling_classes(raw: Any, source: str, path: str) -> Tuple[PollingClassDef, ...]:
    data = _as_dict(raw, source, path)
    out: List[PollingClassDef] = []

    for index, (name, spec) in enumerate(data.items()):
        name = str(name)
        spec = _as_dict(spec, source, f"{path}.{name}")

        #
        # One unit, seconds. `hz`, `every` and `cycles` were accepted
        # until 2026-08-30; they are refused by name now rather than
        # ignored, because a file still carrying `hz: 10` would otherwise
        # load with a default period and poll at the wrong rate in
        # silence.
        #
        for retired in ("hz", "every", "cycles"):
            if retired in spec:
                raise InvalidFieldError(
                    f"polling class {name!r} uses {retired!r}, which was "
                    f"replaced by `seconds` (one unit, wall clock). "
                    f"Write the period in seconds instead.",
                    source, f"{path}.{name}.{retired}",
                )

        if "seconds" not in spec:
            raise InvalidFieldError(
                f"polling class {name!r} needs `seconds`",
                source, f"{path}.{name}",
            )

        value = _as_float(spec["seconds"], source, f"{path}.{name}.seconds")

        if value <= 0:
            raise InvalidFieldError(
                f"polling class {name!r} period must be positive, got {value}",
                source, f"{path}.{name}.seconds",
            )

        out.append(PollingClassDef(
            name=name,
            period=value,
            priority=_as_int(spec.get("priority", index), source,
                             f"{path}.{name}.priority"),
            stagger=bool(spec.get("stagger", False)),
        ))

    return tuple(out)


def load_text(text: str, source: str = "<string>") -> MappingFile:
    """Parse and validate one mapping document."""
    document = yamlsubset.loads(text, source=source)

    if document is None:
        raise MissingFieldError("mapping file is empty", source)

    if not isinstance(document, dict):
        raise InvalidFieldError(
            "a mapping file must be a YAML mapping at the top level", source
        )

    if "schema_version" not in document:
        raise UnsupportedSchemaVersion(
            "mapping file has no schema_version", source
        )

    version = document["schema_version"]

    if version not in SCHEMA_VERSIONS:
        raise UnsupportedSchemaVersion(
            f"schema_version {version!r} is not supported; this build "
            f"understands {', '.join(str(v) for v in SCHEMA_VERSIONS)}",
            source, "schema_version",
        )

    meta = _as_dict(_require(document, "mapping", source, ""), source, "mapping")
    mapping_id = _as_str(
        _require(meta, "id", source, "mapping"), source, "mapping.id"
    )

    provenance = _provenance(
        document.get("source"), source, "source", Provenance()
    )
    verification = _verification(
        document.get("verification"), source, "verification", Verification()
    )

    ecu_raw = _as_dict(document.get("ecu"), source, "ecu")
    ecu = EcuDef(
        family=_as_str(ecu_raw.get("family", "unknown"), source, "ecu.family"),
        target=_target(ecu_raw.get("target"), source, "ecu.target"),
        sgbd=ecu_raw.get("sgbd"),
        variant=ecu_raw.get("variant"),
        hardware=ecu_raw.get("hardware"),
        software=ecu_raw.get("software"),
        match=_capabilities(
            _as_dict(ecu_raw.get("match"), source, "ecu.match").get("capability"),
            source, "ecu.match.capability",
        ),
    )

    defaults = _as_dict(
        _as_dict(document.get("defaults"), source, "defaults").get("request"),
        source, "defaults.request",
    )

    if "target" not in defaults and (
        ecu.target.address is not None or ecu.target.name is not None
    ):
        defaults = dict(defaults)
        defaults["target"] = (
            ecu.target.address if ecu.target.address is not None else ecu.target.name
        )

    requests_raw = _as_dict(document.get("requests"), source, "requests")
    seen_signals: Dict[str, str] = {}
    seen_requests: Dict[str, str] = {}
    requests: List[RequestDef] = []

    for index, (request_id, spec) in enumerate(requests_raw.items()):
        request_id = str(request_id)

        if request_id in seen_requests:
            raise DuplicateRequestError(
                f"request id {request_id!r} is already defined",
                source, f"requests.{request_id}",
            )

        seen_requests[request_id] = source

        requests.append(_request(
            request_id, spec, mapping_id, defaults, source,
            f"requests.{request_id}", provenance, verification, index,
            seen_signals,
        ))

    derived_raw = _as_dict(document.get("derived"), source, "derived")
    derived: List[DerivedDef] = []

    for index, (key, spec) in enumerate(derived_raw.items()):
        key = str(key)

        if key in seen_signals:
            raise DuplicateSignalError(
                f"derived signal {key!r} collides with "
                f"{seen_signals[key]}",
                source, f"derived.{key}",
            )

        seen_signals[key] = f"{source}:derived"
        derived.append(_derived(
            key, spec, mapping_id, source, f"derived.{key}",
            provenance, verification, index,
        ))

    mapping = MappingFile(
        schema_version=version,
        id=mapping_id,
        source_path=source,
        description=_as_str(
            meta.get("description", ""), source, "mapping.description"
        ),
        version=_mapping_version(meta, source),
        production=bool(meta.get("production", True)),
        ecu=ecu,
        requests=tuple(requests),
        derived=tuple(derived),
        polling_classes=_polling_classes(
            document.get("polling_classes"), source, "polling_classes"
        ),
        provenance=provenance,
        verification=verification,
    )

    _check_derived_inputs(mapping)

    return mapping


def _check_derived_inputs(mapping: MappingFile) -> None:
    """
    Every derived input and trigger must name a signal this file provides.

    Cross-file derived signals are deliberately not allowed: a mapping file
    should be loadable and testable on its own.
    """
    known = {s.key for s in mapping.signals}
    known.update(d.key for d in mapping.derived)

    for definition in mapping.derived:
        for role, name in definition.inputs:
            if name not in known:
                raise UnknownDerivedInputError(
                    f"derived signal {definition.key!r} input {role!r} "
                    f"references unknown signal {name!r}",
                    mapping.source_path, f"derived.{definition.key}.inputs.{role}",
                )

        for name in definition.trigger:
            if name not in known:
                raise UnknownDerivedInputError(
                    f"derived signal {definition.key!r} triggers on unknown "
                    f"signal {name!r}",
                    mapping.source_path, f"derived.{definition.key}.trigger",
                )


def load_file(path: str) -> MappingFile:
    """Load and validate one mapping file from disk."""
    with open(path, "r", encoding="utf-8") as handle:
        return load_text(handle.read(), source=path)


def iter_mapping_files(root: str) -> List[str]:
    """Every mapping file under `root`, in a stable order."""
    if os.path.isfile(root):
        return [root]

    found: List[str] = []

    for base, dirs, names in os.walk(root):
        dirs.sort()

        for name in sorted(names):
            if name.startswith(".") or not name.endswith(MAPPING_SUFFIXES):
                continue

            found.append(os.path.join(base, name))

    return found


def load_tree(
    root: str, production_only: bool = False
) -> List[MappingFile]:
    """
    Load every mapping file under `root`.

    `production_only` drops files that mark themselves `production: false`,
    which is how the example/test fixtures stay out of the vehicle runtime
    while still being validated by the CLI and the test suite.
    """
    out: List[MappingFile] = []

    for path in iter_mapping_files(root):
        mapping = load_file(path)

        if production_only and not mapping.production:
            continue

        out.append(mapping)

    return out
