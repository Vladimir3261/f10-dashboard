"""
Mapping file loader and validator.

Everything a mapping file can get wrong is caught here, with a message
naming the file and the path inside it. Nothing downstream re-checks:
by the time the registry sees a `MappingFile`, offsets fit their response,
decoder types exist, derived inputs resolve and keys are unique.
"""

import difflib
import math
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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
    UnknownFieldError,
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

#
# The field vocabulary of every schema object. A key outside its object's
# set is a load error, not a no-op: the loader used to read the fields it
# knew and ignore the rest, so `prodution: false` loaded a candidate file
# into the production set and `defaults.request.timeout` was carried by
# two tracked files for a week without ever reaching the wire. Anything
# the format grows has to be added here, which is the point.
#
FIELDS_DOCUMENT = (
    "schema_version", "mapping", "source", "verification", "ecu",
    "defaults", "polling_classes", "requests", "derived",
)
FIELDS_MAPPING = ("id", "version", "description", "production")
FIELDS_ECU = (
    "family", "target", "sgbd", "variant", "hardware", "software", "match",
)
FIELDS_MATCH = ("capability", "probe")
FIELDS_TARGET = ("address", "name")
FIELDS_SOURCE = ("type", "file", "sgbd", "job", "result", "notes")
FIELDS_VERIFICATION = ("status", "method", "vehicle", "notes")
FIELDS_DEFAULTS = ("request",)
#: What a request may inherit from `defaults.request`. Deliberately not
#: everything a request accepts: `setup`, `response` and `signals` are
#: per-request by nature.
FIELDS_REQUEST_DEFAULTS = (
    "protocol", "transport", "service", "pid", "did", "payload", "target",
    "polling", "timeout",
)
FIELDS_REQUEST = FIELDS_REQUEST_DEFAULTS + (
    "setup", "response", "requires", "signals", "source", "verification",
)
FIELDS_RESPONSE = ("prefix", "payload_offset", "data_length", "min_length")
FIELDS_POLLING = ("class", "pair")
FIELDS_REQUIRES = ("capability",)
FIELDS_SIGNAL = (
    "label", "unit", "decode", "display", "source_name", "log", "source",
    "verification",
)
FIELDS_DECODE = (
    "type", "offset", "length", "bit", "mask", "shift", "pre_add", "scale",
    "divide", "add", "round", "enum", "enum_default", "lookup", "invalid",
    "saturated", "valid_min", "valid_max", "encoding",
)
FIELDS_DISPLAY = ("digits", "min", "max")
FIELDS_CONFIG_BOUND = ("config", "default")
FIELDS_DERIVED = (
    "label", "unit", "operation", "inputs", "display", "fallback", "scale",
    "divide", "add", "pre_add", "round", "log", "trigger", "position",
    "source", "verification",
)
FIELDS_DERIVED_SCALE = ("config", "default")
FIELDS_POLLING_CLASS = ("seconds", "priority", "stagger")

#: Spellings that were once accepted and are now refused by name, with a
#: pointer to the one that replaced them. A retired key silently ignored
#: is the same bug as a typo silently ignored.
RETIRED_FIELDS = {
    "response": {"length": "data_length"},
    "display": {"lo": "min", "hi": "max"},
    "polling_class": {"hz": "seconds", "every": "seconds", "cycles": "seconds"},
    #: `sgbd_variant` conflated two claims - "the ECU answers this family
    #: of reads" and "the ECU is exactly this SGBD" - and one probe can
    #: only ever prove the first. Retired 2026-09-05 (issue #10).
    "capability": {"sgbd_variant": "diagnostic_profile"},
}

#: The capability kind a probe can prove (behavioural compatibility) and
#: the one it never can (exact SGBD identity). Mirrored in
#: bmwdiag/variant.py, which answers them; declared here because the
#: loader checks a `probe:` nomination against the first.
PROFILE_CAPABILITY = "diagnostic_profile"
SGBD_CAPABILITY = "exact_sgbd"


# ------------------------------------------------------------ primitives


def _require(
    data: Dict[str, Any], key: str, source: str, path: str
) -> Any:
    if not isinstance(data, dict) or key not in data or data[key] is None:
        raise MissingFieldError(f"missing required field {key!r}", source, path)

    return data[key]


def _only(
    data: Dict[Any, Any],
    allowed: Iterable[str],
    source: str,
    path: str,
    retired: Optional[Dict[str, str]] = None,
) -> None:
    """
    Reject any key of `data` outside `allowed`, naming the key's path.

    Walks the keys in declaration order and stops at the first stray
    one, so the error points at a field the author actually wrote. A
    close match in the vocabulary is offered as a hint (`prodution` ->
    did you mean `production`?) because that is what the typo case
    looks like.
    """
    allowed = tuple(allowed)
    retired = retired or {}

    for key in data:
        if key in allowed:
            continue

        where = f"{path}.{key}" if path else str(key)

        if key in retired:
            raise InvalidFieldError(
                f"{key!r} was retired; write {retired[key]!r} instead",
                source, where,
            )

        hint = ""

        if isinstance(key, str):
            close = difflib.get_close_matches(key, allowed, n=1, cutoff=0.6)

            if close:
                hint = f" (did you mean {close[0]!r}?)"

        raise UnknownFieldError(
            f"unknown field {key!r}{hint}; allowed fields: "
            + ", ".join(allowed),
            source, where,
        )


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


def _as_number(value: Any, source: str, path: str) -> Any:
    """
    A finite number, keeping int as int so display bounds round-trip
    verbatim.

    NaN and the infinities parse (`.nan`, `.inf`) but never mean anything
    in this format: a NaN scale poisons every sample, an infinite polling
    period is a request that never runs, and neither is something an
    author writes on purpose.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidFieldError(
            f"expected a number, got {value!r}", source, path
        )

    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidFieldError(
            f"expected a finite number, got {value!r}", source, path
        )

    return value


def _as_float(value: Any, source: str, path: str) -> float:
    return float(_as_number(value, source, path))


def _as_opt_str(value: Any, source: str, path: str) -> Optional[str]:
    """A free-text field: absent or a string, never a number or a list."""
    return None if value is None else _as_str(value, source, path)


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

    _only(data, FIELDS_SOURCE, source, path)

    kind = data.get("type", base.type)

    def text(name: str, fallback: Optional[str]) -> Optional[str]:
        return _as_opt_str(data.get(name, fallback), source, f"{path}.{name}")

    return Provenance(
        type=_enum_value(kind, SOURCE_TYPES, source, f"{path}.type"),
        file=text("file", base.file),
        sgbd=text("sgbd", base.sgbd),
        job=text("job", base.job),
        result=text("result", base.result),
        notes=text("notes", base.notes),
    )


def _verification(
    raw: Any, source: str, path: str, base: Verification
) -> Verification:
    data = _as_dict(raw, source, path)

    if not data:
        return base

    _only(data, FIELDS_VERIFICATION, source, path)

    status = data.get("status", base.status)

    def text(name: str, fallback: Optional[str]) -> Optional[str]:
        return _as_opt_str(data.get(name, fallback), source, f"{path}.{name}")

    return Verification(
        status=_enum_value(
            status, VERIFICATION_STATUS, source, f"{path}.status"
        ),
        method=text("method", base.method),
        vehicle=text("vehicle", base.vehicle),
        notes=text("notes", base.notes),
    )


def _display(raw: Any, source: str, path: str) -> Display:
    data = _as_dict(raw, source, path)
    _only(data, FIELDS_DISPLAY, source, path, RETIRED_FIELDS["display"])
    digits = data.get("digits", 0)
    from_config: List[Tuple[str, str]] = []
    bounds: Dict[str, float] = {"lo": 0.0, "hi": 100.0}

    for role, name in (("lo", "min"), ("hi", "max")):
        if name not in data or data[name] is None:
            continue

        value = data[name]

        if isinstance(value, dict):
            _only(value, FIELDS_CONFIG_BOUND, source, f"{path}.{name}")
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

    return Display(
        digits=_as_int(digits, source, f"{path}.digits"),
        lo=bounds["lo"],
        hi=bounds["hi"],
        from_config=tuple(from_config),
    )


def _capabilities(raw: Any, source: str, path: str) -> Tuple[Capability, ...]:
    """
    Capability kinds are deliberately open - an unknown kind fails closed
    at resolution, when no provider claims it - so this is not `_only`.
    A retired kind is still refused by name, for the same reason a
    retired field is: it would otherwise resolve to nothing in silence.
    """
    data = _as_dict(raw, source, path)
    out: List[Capability] = []

    for kind, value in data.items():
        kind = str(kind)
        replacement = RETIRED_FIELDS["capability"].get(kind)

        if replacement is not None:
            raise InvalidFieldError(
                f"'{kind}' was retired; write '{replacement}' (behavioural "
                f"compatibility, proven by a nominated probe) or "
                f"'{SGBD_CAPABILITY}' (exact identity, proven by identity "
                f"evidence) instead",
                source, f"{path}.{kind}",
            )

        if kind in (PROFILE_CAPABILITY, SGBD_CAPABILITY):
            value = _as_str(value, source, f"{path}.{kind}")

            if not value.strip():
                raise InvalidFieldError(
                    f"{kind} must name something", source, f"{path}.{kind}"
                )

        out.append(Capability(kind=kind, value=value))

    return tuple(out)


def _probe_nominations(
    raw: Any, match: Tuple[Capability, ...], source: str, path: str
) -> Tuple[str, ...]:
    """
    `ecu.match.probe`: which of this file's requests prove its profile.

    Only meaningful next to a `diagnostic_profile` requirement - a probe
    with nothing to prove is a mistake worth stopping on. Whether the
    ids exist is checked once the requests are parsed.
    """
    if raw is None:
        return ()

    if isinstance(raw, str):
        raw = [raw]

    if not isinstance(raw, list) or not raw:
        raise InvalidFieldError(
            "expected a request id or a non-empty list of them", source, path
        )

    if not any(c.kind == PROFILE_CAPABILITY for c in match):
        raise InvalidFieldError(
            f"probe nominates requests but the match requires no "
            f"{PROFILE_CAPABILITY} for them to prove",
            source, path,
        )

    out: List[str] = []

    for index, item in enumerate(raw):
        request_id = _as_str(item, source, f"{path}[{index}]")

        if request_id in out:
            raise InvalidFieldError(
                f"request {request_id!r} is nominated twice",
                source, f"{path}[{index}]",
            )

        out.append(request_id)

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
    _only(data, FIELDS_TARGET, source, path)
    address = data.get("address")
    name = data.get("name")

    if address is not None and name is not None:
        raise InvalidFieldError(
            "target takes an address or a name, not both", source, path
        )

    if address is not None:
        address = _as_int(address, source, f"{path}.address")

        if not 0 <= address <= 0xFF:
            raise InvalidFieldError(
                f"diagnostic address out of range: {address}",
                source, f"{path}.address",
            )

        return Target(address=address)

    if name is not None:
        return Target(name=_as_str(name, source, f"{path}.name"))

    raise MissingFieldError("target needs an address or a name", source, path)


# --------------------------------------------------------------- decoder


def _decode(raw: Any, source: str, path: str) -> Decode:
    data = _as_dict(raw, source, path)
    _only(data, FIELDS_DECODE, source, path)
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

    #
    # `is None`, not truthiness: `invalid: 0` declares zero as the
    # sentinel and must not collapse to "no sentinel".
    #
    invalid = data.get("invalid")
    invalid = () if invalid is None else invalid

    if not isinstance(invalid, (list, tuple)):
        invalid = [invalid]

    saturated = data.get("saturated")
    saturated = () if saturated is None else saturated

    if not isinstance(saturated, (list, tuple)):
        saturated = [saturated]

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
        enum_default=_as_opt_str(
            data.get("enum_default"), source, f"{path}.enum_default"
        ),
        lookup=lookup,
        invalid=tuple(
            _as_int(v, source, f"{path}.invalid") for v in invalid
        ),
        saturated=tuple(
            _as_int(v, source, f"{path}.saturated") for v in saturated
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
    _only(data, FIELDS_SIGNAL, source, path)

    return SignalDef(
        key=key,
        request_id=request_id,
        label=_as_str(data.get("label", key), source, f"{path}.label"),
        unit=_as_str(data.get("unit", ""), source, f"{path}.unit"),
        decode=_decode(
            _require(data, "decode", source, path), source, f"{path}.decode"
        ),
        display=_display(data.get("display"), source, f"{path}.display"),
        source_name=_as_opt_str(
            data.get("source_name"), source, f"{path}.source_name"
        ),
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


#
# One validator per wire-level field, shared by the request path and the
# `defaults.request` block. A value written into a mapping file is
# validated wherever it is written: a `timeout: .nan` under defaults must
# fail at load even when every request overrides it, or the file's
# validity would depend on which request happens to inherit what.
#


def _protocol(value: Any, source: str, path: str) -> str:
    return _enum_value(value, PROTOCOLS, source, path)


def _transport(value: Any, source: str, path: str) -> str:
    return _enum_value(value, TRANSPORTS, source, path)


def _byte_id(name: str, top: int):
    def check(value: Any, source: str, path: str) -> int:
        number = _as_int(value, source, path)

        if not 0 <= number <= top:
            raise InvalidFieldError(
                f"{name} out of range: {number}", source, path
            )

        return number

    return check


_service = _byte_id("service", 0xFF)
_pid = _byte_id("pid", 0xFF)
_did = _byte_id("did", 0xFFFF)


def _payload(value: Any, source: str, path: str) -> Tuple[int, ...]:
    """
    An explicit payload. `payload: []` is not "no payload", it is a
    request to send nothing, which the transport would only discover on
    the wire.
    """
    payload = _byte_seq(value, source, path)

    if not payload:
        raise InvalidFieldError("an explicit payload cannot be empty", source, path)

    return payload


def _polling_spec(value: Any, source: str, path: str) -> Tuple[str, str]:
    """(class, pair) from `polling: name` or `polling: {class, pair}`."""
    if isinstance(value, dict):
        _only(value, FIELDS_POLLING, source, path)

        return (
            _as_str(value.get("class", "slow"), source, f"{path}.class"),
            #: Optional. Requests sharing a tag inside a staggered class
            #: are sent together rather than in consecutive rotation slots.
            _as_str(value.get("pair", ""), source, f"{path}.pair"),
        )

    if isinstance(value, str):
        return value, ""

    raise InvalidFieldError(
        "polling must be a class name or a mapping", source, path
    )


def _timeout(value: Any, source: str, path: str) -> float:
    timeout = _as_float(value, source, path)

    if timeout <= 0:
        raise InvalidFieldError(
            f"timeout must be positive, got {timeout}", source, path
        )

    return timeout


#: Field -> validator, for every field a request can inherit.
REQUEST_FIELD_VALIDATORS = {
    "protocol": _protocol,
    "transport": _transport,
    "service": _service,
    "pid": _pid,
    "did": _did,
    "payload": _payload,
    "target": _target,
    "polling": _polling_spec,
    "timeout": _timeout,
}

assert tuple(REQUEST_FIELD_VALIDATORS) == FIELDS_REQUEST_DEFAULTS


def _request_defaults(raw: Any, source: str, path: str) -> Dict[str, Any]:
    """
    The `defaults.request` block, every explicit value validated on its
    own. Returns the raw values (a request re-runs the same validator on
    whichever it inherits, which is cheap and keeps one code path).
    """
    defaults = _as_dict(raw, source, path)
    _only(defaults, FIELDS_REQUEST_DEFAULTS, source, path)

    for name, value in defaults.items():
        if value is not None:
            REQUEST_FIELD_VALIDATORS[name](value, source, f"{path}.{name}")

    return defaults


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
    _only(data, FIELDS_RESPONSE, source, path, RETIRED_FIELDS["response"])

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

    data_length = data.get("data_length")

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

    if min_length < 0:
        raise InvalidLengthError(
            f"min_length must not be negative, got {min_length}",
            source, f"{path}.min_length",
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
    _only(data, FIELDS_REQUEST, source, path)

    def field(name: str, fallback: Any = None) -> Any:
        if name in data and data[name] is not None:
            return data[name]

        if name in defaults and defaults[name] is not None:
            return defaults[name]

        return fallback

    def checked(name: str, fallback: Any = None) -> Any:
        value = field(name, fallback)

        if value is None:
            return None

        return REQUEST_FIELD_VALIDATORS[name](value, source, f"{path}.{name}")

    protocol = checked("protocol", "obd")
    transport = checked("transport", "diagnostic")
    pid = checked("pid")
    did = checked("did")
    service = checked("service", 0x01 if protocol == "obd" else None)
    payload = checked("payload")

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

    #
    # An identifier the protocol does not use would be carried on the
    # RequestDef and never sent: `did:` on an obd request looks like it
    # selects something and selects nothing.
    #
    if protocol == "obd" and did is not None:
        raise InvalidFieldError(
            "an obd request is addressed by pid, not did", source, f"{path}.did"
        )

    if protocol == "uds" and pid is not None:
        raise InvalidFieldError(
            "a uds request is addressed by did, not pid", source, f"{path}.pid"
        )

    if protocol == "raw" and did is not None:
        raise InvalidFieldError(
            "a raw request carries its identifier in the payload, not did",
            source, f"{path}.did",
        )

    target = checked("target") or Target()

    if target.address is None and target.name is None:
        raise MissingFieldError("request has no target", source, f"{path}.target")

    response = _response_spec(
        data.get("response"), protocol, service, pid, did, source, f"{path}.response"
    )

    polling_class, polling_pair = checked("polling") or ("slow", "")

    provenance = _provenance(
        data.get("source"), source, f"{path}.source", base_provenance
    )
    verification = _verification(
        data.get("verification"), source, f"{path}.verification", base_verification
    )

    if "requires" in data and data["requires"] is not None:
        requires_raw = _as_dict(data["requires"], source, f"{path}.requires")
        _only(requires_raw, FIELDS_REQUIRES, source, f"{path}.requires")
        requires = _capabilities(
            requires_raw.get("capability"), source, f"{path}.requires.capability",
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

    #
    # Inherited like the other wire-level fields. It was read from the
    # request block alone until 2026-09-03, so `defaults.request.timeout:
    # 0.4` in the EGS and KOMBI candidates never applied and those ECUs
    # were polled on the transport's 3 s default.
    #
    timeout = checked("timeout")

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
        polling_pair=polling_pair,
        requires=requires,
        timeout=timeout,
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
    _only(data, FIELDS_DERIVED, source, path)
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
        _only(scale_raw, FIELDS_DERIVED_SCALE, source, f"{path}.scale")
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
        _only(
            spec, FIELDS_POLLING_CLASS, source, f"{path}.{name}",
            RETIRED_FIELDS["polling_class"],
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
            stagger=_as_bool(
                spec.get("stagger"), source, f"{path}.{name}.stagger", False
            ),
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

    #: `True == 1` in Python; `schema_version: true` is not version 1.
    if isinstance(version, bool) or version not in SCHEMA_VERSIONS:
        raise UnsupportedSchemaVersion(
            f"schema_version {version!r} is not supported; this build "
            f"understands {', '.join(str(v) for v in SCHEMA_VERSIONS)}",
            source, "schema_version",
        )

    _only(document, FIELDS_DOCUMENT, source, "")

    meta = _as_dict(_require(document, "mapping", source, ""), source, "mapping")
    _only(meta, FIELDS_MAPPING, source, "mapping")
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
    _only(ecu_raw, FIELDS_ECU, source, "ecu")
    match_raw = _as_dict(ecu_raw.get("match"), source, "ecu.match")
    _only(match_raw, FIELDS_MATCH, source, "ecu.match")
    match = _capabilities(
        match_raw.get("capability"), source, "ecu.match.capability",
    )
    ecu = EcuDef(
        family=_as_str(ecu_raw.get("family", "unknown"), source, "ecu.family"),
        target=_target(ecu_raw.get("target"), source, "ecu.target"),
        sgbd=_as_opt_str(ecu_raw.get("sgbd"), source, "ecu.sgbd"),
        variant=_as_opt_str(ecu_raw.get("variant"), source, "ecu.variant"),
        hardware=_as_opt_str(ecu_raw.get("hardware"), source, "ecu.hardware"),
        software=_as_opt_str(ecu_raw.get("software"), source, "ecu.software"),
        match=match,
        probe=_probe_nominations(
            match_raw.get("probe"), match, source, "ecu.match.probe",
        ),
    )

    defaults_raw = _as_dict(document.get("defaults"), source, "defaults")
    _only(defaults_raw, FIELDS_DEFAULTS, source, "defaults")
    defaults = _request_defaults(
        defaults_raw.get("request"), source, "defaults.request"
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

    for index, request_id in enumerate(ecu.probe):
        if request_id not in seen_requests:
            raise InvalidFieldError(
                f"probe nominates {request_id!r}, which this file does not "
                f"define",
                source, f"ecu.match.probe[{index}]",
            )

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
        production=_as_bool(
            meta.get("production"), source, "mapping.production", True
        ),
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
