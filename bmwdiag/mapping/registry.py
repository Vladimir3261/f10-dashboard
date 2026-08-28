"""
Mapping registry and resolution.

The registry holds every loaded mapping file. Resolution takes a set of
ECU capabilities plus runtime configuration and produces a
`ResolvedProfile`: the requests this particular car can actually answer,
the signals they carry, the derived channels whose inputs exist, and the
display metadata the dashboard and the recorder consume.

Nothing in here knows what a PID is. Capability *matching* is generic -
"does the ECU satisfy this named capability" - and the OBD-specific answer
to that question lives in bmwdiag.obd.
"""

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .derive import apply_derived, compute_derived
from .errors import DuplicateRequestError, DuplicateSignalError
from .model import (
    Capability,
    DerivedDef,
    MappingFile,
    PollingClassDef,
    RequestDef,
    SignalDef,
)

__all__ = ["CapabilitySet", "AllCapabilities", "ResolvedProfile", "MappingRegistry"]


class CapabilitySet:
    """
    What one ECU can do.

    `known` False means discovery produced nothing usable; in that case
    every capability check passes, which preserves the previous behaviour
    of polling the whole table when an ECU advertises no support bitmask.
    """

    known = True

    def satisfies(self, capability: Capability) -> bool:
        raise NotImplementedError

    def satisfies_all(self, capabilities: Iterable[Capability]) -> bool:
        return all(self.satisfies(c) for c in capabilities)


class AllCapabilities(CapabilitySet):
    """Accepts everything. Used by demo mode and by the mapping CLI."""

    known = False

    def satisfies(self, capability: Capability) -> bool:
        return True

    def __repr__(self) -> str:
        return "AllCapabilities()"


class ResolvedProfile:
    """
    The mapping layer's answer to "what can we read from this car".

    Holds requests in mapping order, the signals they carry, and derived
    channels. Everything downstream - telemetry metadata, the recorder's
    params table, the polling plan - is derived from this object rather
    than from module-level tables.
    """

    def __init__(
        self,
        requests: Sequence[RequestDef],
        derived: Sequence[DerivedDef],
        config: Optional[Dict[str, Any]] = None,
        targets: Optional[Dict[str, int]] = None,
        polling_classes: Sequence[PollingClassDef] = (),
        mappings: Sequence[MappingFile] = (),
    ):
        self.requests: List[RequestDef] = list(requests)
        self.derived: List[DerivedDef] = list(derived)
        self.config: Dict[str, Any] = dict(config or {})
        self.targets: Dict[str, int] = dict(targets or {})
        self.polling_classes: List[PollingClassDef] = list(polling_classes)
        self.mappings: List[MappingFile] = list(mappings)

        self.signals: List[SignalDef] = [s for r in self.requests for s in r.signals]
        self._signal_by_key: Dict[str, SignalDef] = {s.key: s for s in self.signals}
        self._request_by_id: Dict[str, RequestDef] = {r.id: r for r in self.requests}
        self._derived_by_key: Dict[str, DerivedDef] = {d.key: d for d in self.derived}

        #: mapping id -> data version, for stamping every recorded sample
        #: with the exact mapping revision that decoded it.
        self._version_by_mapping: Dict[str, int] = {
            m.id: m.version for m in self.mappings
        }

    # -- lookups ----------------------------------------------------

    def signal(self, key: str) -> Optional[SignalDef]:
        return self._signal_by_key.get(key)

    def derived_signal(self, key: str) -> Optional[DerivedDef]:
        return self._derived_by_key.get(key)

    def request(self, request_id: str) -> Optional[RequestDef]:
        return self._request_by_id.get(request_id)

    def request_for_signal(self, key: str) -> Optional[RequestDef]:
        signal = self._signal_by_key.get(key)

        return None if signal is None else self._request_by_id.get(signal.request_id)

    def has(self, key: str) -> bool:
        return key in self._signal_by_key or key in self._derived_by_key

    # -- data versioning --------------------------------------------

    def channel_version(self, key: str) -> Optional[int]:
        """
        The data version of the mapping file that owns channel `key`.

        A read signal inherits the version of the file its request came
        from; a derived channel the version of the file that defines it.
        Returns None for an unknown channel. This is the value stamped
        onto every recorded sample so a dataset ties back to the exact
        mapping revision that produced it. See docs/DATA_VERSIONING.md.
        """
        signal = self._signal_by_key.get(key)

        if signal is not None:
            request = self._request_by_id.get(signal.request_id)
            mapping_id = request.mapping_id if request is not None else None
        else:
            definition = self._derived_by_key.get(key)
            mapping_id = definition.mapping_id if definition is not None else None

        if mapping_id is None:
            return None

        return self._version_by_mapping.get(mapping_id)

    def mapping_manifest(self) -> List[Dict[str, Any]]:
        """
        Every loaded mapping file with its data version, in load order.

        The authoritative "what decoded this run" record: id, version,
        source path, and whether it is a production mapping. Ordered and
        de-duplicated by id so the result is stable for one profile.
        """
        seen: Dict[str, Dict[str, Any]] = {}

        for m in self.mappings:
            if m.id not in seen:
                seen[m.id] = {
                    "id": m.id,
                    "version": m.version,
                    "source_path": m.source_path,
                    "production": m.production,
                }

        return list(seen.values())

    def mapping_set(self) -> str:
        """
        A compact one-line fingerprint of the loaded mapping set:
        `id@version` for each file, comma-joined and sorted. Stable for a
        given set of files+versions, cheap to store on a session row and
        to compare across runs.
        """
        return ",".join(
            f"{m['id']}@{m['version']}"
            for m in sorted(self.mapping_manifest(), key=lambda r: r["id"])
        )

    # -- ordering ---------------------------------------------------

    def signal_keys(self) -> List[str]:
        """Directly-read signals, in mapping order."""
        return [s.key for s in self.signals]

    def keys(self) -> List[str]:
        """
        Every channel, ordered for display.

        Derived channels marked `position: first` lead, then the read
        signals in mapping order, then the rest of the derived channels.
        """
        first = [d.key for d in self.derived if d.position == "first"]
        last = [d.key for d in self.derived if d.position != "first"]

        return first + self.signal_keys() + last

    # -- metadata ---------------------------------------------------

    def meta_for(self, key: str) -> Optional[Dict[str, Any]]:
        """Display metadata for one channel, in the dashboard's shape."""
        signal = self._signal_by_key.get(key)

        if signal is not None:
            display = signal.display.resolve(self.config)

            return {
                "key": signal.key, "label": signal.label, "unit": signal.unit,
                "digits": display.digits, "lo": display.lo, "hi": display.hi,
            }

        definition = self._derived_by_key.get(key)

        if definition is None:
            return None

        display = definition.display.resolve(self.config)

        return {
            "key": definition.key, "label": definition.label,
            "unit": definition.unit, "digits": display.digits,
            "lo": display.lo, "hi": display.hi,
        }

    def meta(self, keys: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        """Display metadata for every channel, or for a given selection."""
        wanted = list(keys) if keys is not None else self.keys()

        return [m for m in (self.meta_for(k) for k in wanted) if m is not None]

    def param_row(self, key: str) -> Tuple[Optional[int], str, str]:
        """
        (pid, label, unit) for the recorder's `params` table.

        A signal that does not come from OBD has no PID, and the column
        stays NULL - which is exactly what the existing schema allows.
        """
        signal = self._signal_by_key.get(key)

        if signal is not None:
            request = self._request_by_id.get(signal.request_id)
            pid = request.pid if request is not None and request.protocol == "obd" else None

            return pid, signal.label, signal.unit

        definition = self._derived_by_key.get(key)

        if definition is not None:
            return None, definition.label, definition.unit

        return None, key, ""

    # -- protocol views ---------------------------------------------

    def obd_pid_lengths(self) -> Dict[int, int]:
        """
        PID -> data byte count, so a multi-PID reply can be walked.

        This replaces the old hand-maintained PID_LEN table: the lengths
        now come from the same mapping data the decoders use.
        """
        out: Dict[int, int] = {}

        for request in self.requests:
            if request.protocol != "obd" or request.pid is None:
                continue

            if request.response.data_length is not None:
                out[request.pid] = request.response.data_length

        return out

    def obd_pids(self) -> List[int]:
        return sorted({
            r.pid for r in self.requests
            if r.protocol == "obd" and r.pid is not None
        })

    # -- derived ----------------------------------------------------

    def apply_derived(
        self, values: Dict[str, Any], fresh: Dict[str, Any]
    ) -> Dict[str, float]:
        return apply_derived(self.derived, values, fresh, self.config)

    def compute(self, key: str, values: Dict[str, Any]) -> Optional[float]:
        definition = self._derived_by_key.get(key)

        if definition is None:
            return None

        return compute_derived(definition, values, self.config)

    def __repr__(self) -> str:
        return (
            f"ResolvedProfile({len(self.requests)} requests, "
            f"{len(self.signals)} signals, {len(self.derived)} derived)"
        )


class MappingRegistry:
    """Every loaded mapping file, plus resolution against one vehicle."""

    def __init__(self, mappings: Sequence[MappingFile] = ()):
        self.mappings: List[MappingFile] = []

        for mapping in mappings:
            self.add(mapping)

    # -- construction -----------------------------------------------

    def add(self, mapping: MappingFile) -> None:
        """Add a mapping file, rejecting collisions with what is loaded."""
        request_ids = {r.id: m.source_path for m in self.mappings for r in m.requests}
        signal_keys = {
            s.key: m.source_path for m in self.mappings for s in m.signals
        }
        signal_keys.update({
            d.key: m.source_path for m in self.mappings for d in m.derived
        })

        for request in mapping.requests:
            if request.id in request_ids:
                raise DuplicateRequestError(
                    f"request id {request.id!r} is already provided by "
                    f"{request_ids[request.id]}",
                    mapping.source_path,
                )

        for key in [s.key for s in mapping.signals] + [d.key for d in mapping.derived]:
            if key in signal_keys:
                raise DuplicateSignalError(
                    f"signal key {key!r} is already provided by "
                    f"{signal_keys[key]}",
                    mapping.source_path,
                )

        self.mappings.append(mapping)

    @classmethod
    def from_tree(cls, root: str, production_only: bool = True) -> "MappingRegistry":
        from .loader import load_tree

        return cls(load_tree(root, production_only=production_only))

    # -- introspection ----------------------------------------------

    @property
    def requests(self) -> List[RequestDef]:
        return [r for m in self.mappings for r in m.requests]

    @property
    def signals(self) -> List[SignalDef]:
        return [s for m in self.mappings for s in m.signals]

    @property
    def derived(self) -> List[DerivedDef]:
        return [d for m in self.mappings for d in m.derived]

    def mapping(self, mapping_id: str) -> Optional[MappingFile]:
        for mapping in self.mappings:
            if mapping.id == mapping_id:
                return mapping

        return None

    def find_signal(self, key: str) -> Optional[SignalDef]:
        for signal in self.signals:
            if signal.key == key:
                return signal

        return None

    def find_request(self, request_id: str) -> Optional[RequestDef]:
        for request in self.requests:
            if request.id == request_id:
                return request

        return None

    def obd_pids(self) -> List[int]:
        """Every Mode 01 PID any loaded mapping asks for."""
        return sorted({
            r.pid for r in self.requests
            if r.protocol == "obd" and r.pid is not None
        })

    def polling_classes(self) -> List[PollingClassDef]:
        out: List[PollingClassDef] = []

        for mapping in self.mappings:
            out.extend(mapping.polling_classes)

        return out

    # -- resolution -------------------------------------------------

    def resolve(
        self,
        capabilities: Optional[CapabilitySet] = None,
        config: Optional[Dict[str, Any]] = None,
        targets: Optional[Dict[str, int]] = None,
        family: Optional[str] = None,
    ) -> ResolvedProfile:
        """
        Work out what this vehicle can actually provide.

        A mapping file is skipped entirely when its ECU match rules fail.
        Within a surviving file, each request is enabled when its own
        capability requirements hold - which for standard OBD means the
        PID appears in the Mode 01 support bitmask.
        """
        caps = capabilities or AllCapabilities()
        requests: List[RequestDef] = []
        derived: List[DerivedDef] = []
        used: List[MappingFile] = []
        classes: List[PollingClassDef] = []

        for mapping in self.mappings:
            if family is not None and mapping.ecu.family != family:
                continue

            if not caps.satisfies_all(mapping.ecu.match):
                continue

            enabled = [r for r in mapping.requests if caps.satisfies_all(r.requires)]

            if not enabled and not mapping.derived:
                continue

            used.append(mapping)
            requests.extend(enabled)
            classes.extend(mapping.polling_classes)

            available = {s.key for r in enabled for s in r.signals}
            #
            # A derived channel survives only if everything it needs is
            # actually being read. Dropping it is quieter than publishing
            # a channel that can never produce a value.
            #
            candidates = list(mapping.derived)

            for _ in range(len(candidates)):
                progressed = False

                for definition in list(candidates):
                    fallbacks = definition.fallback_map()
                    needed = [
                        name for role, name in definition.inputs
                        if role not in fallbacks
                    ]

                    if not all(name in available for name in needed):
                        continue

                    derived.append(definition)
                    available.add(definition.key)
                    candidates.remove(definition)
                    progressed = True

                if not progressed:
                    break

        return ResolvedProfile(
            requests=requests,
            derived=derived,
            config=config,
            targets=targets,
            polling_classes=classes,
            mappings=used,
        )

    def __repr__(self) -> str:
        return f"MappingRegistry({len(self.mappings)} files)"
