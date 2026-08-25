"""
Derived signals.

Channels like turbo boost and fuel litres are arithmetic over other
signals. They are described by a closed set of named operations rather
than an expression language: mappings stay data, and there is no eval
anywhere in this file or the format it reads.
"""

from typing import Any, Dict, Optional, Sequence

from .errors import InvalidFieldError
from .model import DerivedDef

__all__ = ["apply_derived", "compute_derived", "derived_ready"]


def _resolve_inputs(
    definition: DerivedDef,
    values: Dict[str, Any],
) -> Optional[Dict[str, float]]:
    """Bind each role to a number, or None if a required input is missing."""
    fallback = definition.fallback_map()
    bound: Dict[str, float] = {}

    for role, source in definition.inputs:
        if source in values and isinstance(values[source], (int, float)):
            bound[role] = float(values[source])
        elif role in fallback:
            bound[role] = float(fallback[role])
        else:
            return None

    return bound


def _scale_for(definition: DerivedDef, config: Dict[str, Any]) -> float:
    if definition.scale_config is None:
        return definition.scale

    if definition.scale_config not in config:
        return definition.scale

    return float(config[definition.scale_config])


def compute_derived(
    definition: DerivedDef,
    values: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """
    Evaluate one derived signal, or None when its inputs are not available.

    Operation semantics - the order of multiply and divide is part of the
    contract, because it decides the last float digit:

        linear          value * scale / divide + add
        subtract_scale  (value - reference) * scale / divide + add
        divide_scale    (value / divide) * scale + add
        sum             sum(all inputs) * scale / divide + add
        product         value * reference * scale / divide + add
        ratio           (value / reference) * scale + add
    """
    config = config or {}
    bound = _resolve_inputs(definition, values)

    if bound is None:
        return None

    scale = _scale_for(definition, config)
    divide = definition.divide
    add = definition.add
    operation = definition.operation

    try:
        if operation == "linear":
            result = bound["value"]

            if definition.pre_add:
                result = result + definition.pre_add

            result = result * scale / divide + add

        elif operation == "subtract_scale":
            result = bound["value"] - bound["reference"]

            if scale != 1.0:
                result = result * scale

            if divide != 1.0:
                result = result / divide

            result = result + add

        elif operation == "divide_scale":
            result = bound["value"]

            if definition.pre_add:
                result = result + definition.pre_add

            if divide != 1.0:
                result = result / divide

            if scale != 1.0:
                result = result * scale

            result = result + add

        elif operation == "sum":
            result = sum(bound[role] for role, _ in definition.inputs)
            result = result * scale / divide + add

        elif operation == "product":
            result = bound["value"] * bound["reference"] * scale / divide + add

        elif operation == "ratio":
            if bound["reference"] == 0:
                return None

            result = (bound["value"] / bound["reference"]) * scale + add

        else:
            raise InvalidFieldError(
                f"derived signal {definition.key!r} uses unknown "
                f"operation {operation!r}"
            )
    except KeyError as exc:
        raise InvalidFieldError(
            f"derived signal {definition.key!r} operation {operation!r} "
            f"needs input role {exc.args[0]!r}"
        )
    except ZeroDivisionError:
        return None

    if definition.round is not None:
        result = round(result, definition.round)

    return float(result)


def derived_ready(definition: DerivedDef, fresh: Dict[str, Any]) -> bool:
    """
    True when this cycle brought a new reading for the trigger inputs.

    Matching the old poll loop: boost is recomputed when a new manifold
    pressure arrives, not when a new barometric pressure does.
    """
    if not definition.trigger:
        return True

    return all(key in fresh for key in definition.trigger)


def apply_derived(
    definitions: Sequence[DerivedDef],
    values: Dict[str, Any],
    fresh: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, float]:
    """
    Evaluate every derived signal whose trigger fired this cycle.

    `values` is the carried-forward view (a derived signal may reference a
    channel that was last read several cycles ago); `fresh` is what came in
    this cycle. Returns only the newly computed values.
    """
    out: Dict[str, float] = {}

    for definition in definitions:
        if not derived_ready(definition, fresh):
            continue

        result = compute_derived(definition, values, config)

        if result is None:
            continue

        out[definition.key] = result

    return out
