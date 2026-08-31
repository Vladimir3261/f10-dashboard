"""
Declarative diagnostic mapping.

Vehicle knowledge - which request produces which telemetry channel, and
how to turn its bytes into a number - lives in versioned mapping files,
not in Python. This package loads those files, validates them, decodes
responses against them, and schedules the requests they describe.

    from bmwdiag.mapping import MappingRegistry
    from bmwdiag.mapping.polling import PollingPlan

    registry = MappingRegistry.from_tree("mappings")
    profile = registry.resolve(capabilities, config={"tank": 70.0},
                               targets={"discovered_engine": 0x12})
    plan = PollingPlan(profile.requests)

See docs/MAPPING_ARCHITECTURE.md.
"""

from .decoder import (
    QUALITIES,
    Reading,
    decode_response,
    decode_signal,
    decode_value,
    read_response,
    read_value,
)
from .derive import apply_derived, compute_derived
from .errors import MappingError
from .execute import fault_kind, MappingExecutor
from .loader import load_file, load_text, load_tree
from .model import (
    Decode,
    DerivedDef,
    MappingFile,
    PollingClassDef,
    Provenance,
    RequestDef,
    SignalDef,
    Verification,
)
from .modes import (
    DEFAULT_MODE_CONFIG,
    DriveMode,
    ModeTable,
    apply_mode,
    load_modes,
)
from .polling import PollingPlan, resolve_classes
from .registry import AllCapabilities, CapabilitySet, MappingRegistry, ResolvedProfile

__all__ = [
    "fault_kind",
    "AllCapabilities",
    "CapabilitySet",
    "DEFAULT_MODE_CONFIG",
    "Decode",
    "DerivedDef",
    "DriveMode",
    "ModeTable",
    "MappingError",
    "MappingExecutor",
    "MappingFile",
    "MappingRegistry",
    "PollingClassDef",
    "PollingPlan",
    "Provenance",
    "RequestDef",
    "ResolvedProfile",
    "SignalDef",
    "Verification",
    "apply_derived",
    "apply_mode",
    "compute_derived",
    "load_modes",
    "QUALITIES",
    "Reading",
    "decode_response",
    "decode_signal",
    "decode_value",
    "read_response",
    "read_value",
    "load_file",
    "load_text",
    "load_tree",
    "resolve_classes",
]
