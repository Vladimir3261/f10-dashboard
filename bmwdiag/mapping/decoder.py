"""
Primitive decoders and the response -> signal-values step.

Nothing here is vehicle-specific. It takes a `RequestDef`, a raw response
and produces readings for the signals that request owns.

There are two views of the same decode, and the difference is the whole
point of the data-quality layer:

  * `read_value` / `read_response` return a `Reading` - the decoded
    number *plus* a quality label saying whether to trust it. A value the
    ECU flagged as unavailable still comes back, carrying `sentinel`, so
    "the sensor said no-value" is recorded rather than inferred from an
    absence.
  * `decode_value` / `decode_response` are the older, narrower view: only
    usable readings, with None (or an omitted key) for everything else.
    They are wrappers over the first pair and their behaviour has not
    changed - a caller that has no way to carry a label is better off
    dropping the value than silently treating a sentinel as a reading.

Before this layer existed only the second view was available, which made
"the sensor answered 2.0 meaning no-value" and "we never asked" the same
row in storage: none at all.
"""

import struct
from typing import Any, Callable, Dict, NamedTuple, Optional, Tuple

from .errors import (
    DecodeError,
    InvalidLengthError,
    InvalidOffsetError,
    ResponseMismatchError,
    UnknownDecoderError,
)
from .model import Decode, RequestDef, SignalDef

__all__ = [
    "PRIMITIVES",
    "QUALITIES",
    "Reading",
    "primitive_width",
    "read_value",
    "read_response",
    "decode_value",
    "decode_signal",
    "decode_response",
    "match_prefix",
]

#: Quality labels, in the ECU's own terms.
#:
#: This tuple is a contract with the lake's schema, not a local choice:
#: `telemetry.samples.quality` is an Enum8 over exactly these six names,
#: and ClickHouse rejects an unknown enum value by failing the entire
#: insert batch (unlike an unknown column, which it drops silently).
#: Adding a label here without the matching ALTER would break sync.
OK = "ok"
SATURATED = "saturated"
SENTINEL = "sentinel"
STALE = "stale"
CLIPPED = "clipped"
DECODE_FAIL = "decode_fail"

QUALITIES = (OK, SATURATED, SENTINEL, STALE, CLIPPED, DECODE_FAIL)


class Reading(NamedTuple):
    """
    One decoded signal value and how much to trust it.

    `value` is always the number the bytes actually decoded to, even when
    the quality says not to use it: a sentinel row whose value is the real
    2.0 is honest, and a placeholder would not be. Suppression is the
    caller's job - see `usable`.
    """

    value: Any
    quality: str = OK

    @property
    def usable(self) -> bool:
        """True when this reading may be used as a measurement."""
        return self.quality == OK


def _int_be(data: bytes) -> int:
    return int.from_bytes(data, "big", signed=False)


def _int_le(data: bytes) -> int:
    return int.from_bytes(data, "little", signed=False)


def _sint_be(data: bytes) -> int:
    return int.from_bytes(data, "big", signed=True)


def _sint_le(data: bytes) -> int:
    return int.from_bytes(data, "little", signed=True)


#: name -> (fixed width in bytes or None, reader)
PRIMITIVES: Dict[str, Tuple[Optional[int], Callable[[bytes], Any]]] = {
    "uint8": (1, _int_be),
    "int8": (1, _sint_be),
    "uint16_be": (2, _int_be),
    "uint16_le": (2, _int_le),
    "int16_be": (2, _sint_be),
    "int16_le": (2, _sint_le),
    "uint24_be": (3, _int_be),
    "uint24_le": (3, _int_le),
    "uint32_be": (4, _int_be),
    "uint32_le": (4, _int_le),
    "int32_be": (4, _sint_be),
    "int32_le": (4, _sint_le),
    "float32_be": (4, lambda d: struct.unpack(">f", d)[0]),
    "float32_le": (4, lambda d: struct.unpack("<f", d)[0]),
    "bytes": (None, bytes),
    "ascii": (None, None),      # handled inline, needs the encoding field
    "bit": (None, None),        # width comes from decode.length (default 1)
    "bitfield": (None, None),   # width comes from decode.length (default 1)
}

#: Types whose window length must be given explicitly by the mapping.
VARIABLE_WIDTH = ("bytes", "ascii")

#: Types whose window defaults to one byte when unspecified.
BIT_TYPES = ("bit", "bitfield")


def primitive_width(decode: Decode) -> int:
    """Bytes a signal reads, after defaults are applied."""
    spec = PRIMITIVES.get(decode.type)

    if spec is None:
        raise UnknownDecoderError(f"unknown decoder type {decode.type!r}")

    fixed = spec[0]

    if fixed is not None:
        return fixed

    if decode.length is not None:
        return decode.length

    if decode.type in BIT_TYPES:
        return 1

    raise InvalidLengthError(
        f"decoder type {decode.type!r} requires an explicit length"
    )


def _read_primitive(decode: Decode, window: bytes) -> Any:
    kind = decode.type

    if kind == "ascii":
        text = window.decode(decode.encoding, errors="replace")

        return text.split("\0")[0].strip()

    if kind == "bytes":
        return bytes(window)

    if kind == "bit":
        whole = _int_be(window)
        index = decode.bit or 0

        return 1 if (whole >> index) & 1 else 0

    if kind == "bitfield":
        whole = _int_be(window)
        mask = decode.mask

        if mask is None:
            return whole

        shift = decode.shift

        if shift is None:
            #
            # Auto-shift by the mask's trailing zeros so a mapping only has
            # to state where the field is, not twice.
            #
            shift = (mask & -mask).bit_length() - 1 if mask else 0

        return (whole & mask) >> shift

    return PRIMITIVES[kind][1](window)


def _interpolate(table: Tuple[Tuple[float, float], ...], raw: float) -> float:
    """Piecewise-linear lookup; clamps outside the table."""
    if raw <= table[0][0]:
        return float(table[0][1])

    if raw >= table[-1][0]:
        return float(table[-1][1])

    for i in range(1, len(table)):
        x1, y1 = table[i]

        if raw <= x1:
            x0, y0 = table[i - 1]
            span = x1 - x0

            if span == 0:
                return float(y1)

            return float(y0) + (float(y1) - float(y0)) * (raw - x0) / span

    return float(table[-1][1])


def _transform(decode: Decode, raw: Any) -> Any:
    """
    Apply the data-only transformations.

    Each step is skipped when it is at its identity value. That is not an
    optimisation: it keeps `raw / 4.0` an exact float divide instead of
    `(raw + 0.0) * 1.0 / 4.0 + 0.0`, which is what makes the migrated OBD
    formulas produce bit-identical results.
    """
    value: Any = raw

    if decode.pre_add:
        value = value + decode.pre_add

    if decode.scale != 1.0:
        value = value * decode.scale

    if decode.divide != 1.0:
        value = value / decode.divide

    if decode.add:
        value = value + decode.add

    return float(value)


def read_value(decode: Decode, payload: bytes) -> Reading:
    """
    Decode one signal out of `payload` (already offset to the data area).

    Always returns the number the bytes decoded to. The quality label says
    whether that number is a measurement:

        raw listed in `invalid`     -> sentinel   (the ECU said no-value)
        raw listed in `saturated`   -> saturated  (the sensor hit its rail)
        value outside valid_min/max -> clipped    (outside the declared range)

    The first two are raw-domain tests, deliberately: a sentinel is a bit
    pattern (0xFFFF), not a float, and comparing after the transform would
    make the test depend on the scale. The label survives the transform, so
    a sentinel still reports the value it decodes to.
    """
    width = primitive_width(decode)
    start = decode.offset
    end = start + width

    if start < 0:
        raise InvalidOffsetError(f"negative offset {start}")

    if end > len(payload):
        raise ResponseMismatchError(
            f"need bytes {start}..{end} but payload is {len(payload)} long"
        )

    raw = _read_primitive(decode, payload[start:end])
    quality = OK

    if isinstance(raw, int):
        if decode.invalid and raw in decode.invalid:
            quality = SENTINEL
        elif decode.saturated and raw in decode.saturated:
            quality = SATURATED

    if decode.enum is not None:
        if not isinstance(raw, int):
            raise DecodeError("enum decoding needs an integer primitive")

        return Reading(dict(decode.enum).get(raw, decode.enum_default), quality)

    if isinstance(raw, (bytes, str)):
        return Reading(raw, quality)

    if decode.lookup is not None:
        value = _interpolate(decode.lookup, raw)
    else:
        value = _transform(decode, raw)

    #
    # A range violation only *labels* a reading that nothing more specific
    # has claimed. A sentinel usually decodes outside the sane range too
    # (lambda's 0xFFFF lands on exactly 2.0, the top of its scale), and
    # "the ECU said no-value" is the more useful of the two facts.
    #
    if quality == OK:
        if decode.valid_min is not None and value < decode.valid_min:
            quality = CLIPPED
        elif decode.valid_max is not None and value > decode.valid_max:
            quality = CLIPPED

    if decode.round is not None:
        value = round(value, decode.round)

    return Reading(value, quality)


def decode_value(decode: Decode, payload: bytes) -> Any:
    """
    Decode one usable value out of `payload`, or None.

    The narrow view: anything the ECU flagged, or that fell outside the
    declared range, reads as None. Callers that can carry a quality label
    should use `read_value` instead and keep the value.
    """
    reading = read_value(decode, payload)

    return reading.value if reading.usable else None


def match_prefix(request: RequestDef, response: bytes) -> bytes:
    """
    Validate a response and return the payload area signals index into.

    Raises ResponseMismatchError for a short response or a wrong prefix, so
    a stale or negative reply can never be silently decoded as data.
    """
    spec = request.response

    if spec.prefix:
        if len(response) < len(spec.prefix):
            raise ResponseMismatchError(
                f"{request.id}: response {response.hex(' ')} shorter than "
                f"expected prefix {bytes(spec.prefix).hex(' ')}"
            )

        if tuple(response[: len(spec.prefix)]) != spec.prefix:
            raise ResponseMismatchError(
                f"{request.id}: expected prefix {bytes(spec.prefix).hex(' ')}, "
                f"got {response[: len(spec.prefix)].hex(' ')}"
            )

    need = max(spec.min_length, spec.total_length or 0)

    if len(response) < need:
        raise ResponseMismatchError(
            f"{request.id}: response is {len(response)} bytes, need {need}"
        )

    payload = response[spec.payload_offset:]

    if spec.data_length is not None:
        payload = payload[: spec.data_length]

    return payload


def decode_signal(signal: SignalDef, request: RequestDef, response: bytes) -> Any:
    """Decode a single named signal out of a full response."""
    return decode_value(signal.decode, match_prefix(request, response))


def read_response(request: RequestDef, response: bytes) -> Dict[str, Reading]:
    """
    Decode every signal a request owns out of one response, with quality.

    Every signal the response carried is present, including the ones the
    ECU flagged as unavailable. That is the difference that lets storage
    tell "the sensor reported no-value" apart from "we never asked".
    """
    payload = match_prefix(request, response)

    return {
        signal.key: read_value(signal.decode, payload)
        for signal in request.signals
    }


def decode_response(request: RequestDef, response: bytes) -> Dict[str, Any]:
    """
    Decode every usable signal a request owns out of one response.

    A signal that is not usable (sentinel, saturated, out of range) is left
    out rather than reported as a value. See `read_response` to keep it.
    """
    return {
        key: reading.value
        for key, reading in read_response(request, response).items()
        if reading.usable
    }
