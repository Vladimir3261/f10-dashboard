"""
Mapping-specific exceptions.

Loading and decoding raise these rather than bare KeyError/TypeError so a
malformed mapping file reports where the problem is instead of surfacing
somewhere deep in the poll loop.
"""

from typing import Optional


class MappingError(Exception):
    """Base class for every mapping failure."""

    def __init__(
        self,
        message: str,
        source: Optional[str] = None,
        path: Optional[str] = None,
    ):
        self.message = message
        self.source = source
        self.path = path

        where = ""

        if source:
            where += f" [{source}"
            where += f":{path}]" if path else "]"
        elif path:
            where += f" [{path}]"

        super().__init__(message + where)


class MappingSyntaxError(MappingError):
    """The file is not parseable as the supported YAML subset."""


class UnsupportedSchemaVersion(MappingError):
    """schema_version is missing or newer than this build understands."""


class MissingFieldError(MappingError):
    """A required key is absent."""


class InvalidFieldError(MappingError):
    """A key is present but holds a value of the wrong shape or type."""


class UnknownFieldError(MappingError):
    """
    A key this schema object does not define.

    Raised rather than ignored: a misspelt `prodution: false` that fell
    back to the default would put a candidate file into the production
    set in silence, and a mapping decides which bytes reach the car.
    """


class DuplicateRequestError(MappingError):
    """Two requests claim the same id."""


class DuplicateSignalError(MappingError):
    """Two signals claim the same normalised key."""


class UnknownDecoderError(MappingError):
    """decode.type names a primitive the decoder does not implement."""


class InvalidLengthError(MappingError):
    """A length or bit width is impossible for the declared response."""


class InvalidOffsetError(MappingError):
    """A signal reads outside the declared response window."""


class InvalidEnumError(MappingError):
    """An enum/lookup table or a fixed-vocabulary field is malformed."""


class UnknownDerivedInputError(MappingError):
    """A derived signal names an input that no mapping provides."""


class DecodeError(MappingError):
    """A response could not be decoded."""


class ResponseMismatchError(DecodeError):
    """A response is too short or does not carry the expected prefix."""


class PollingError(MappingError):
    """A request names a polling class nobody defines."""
