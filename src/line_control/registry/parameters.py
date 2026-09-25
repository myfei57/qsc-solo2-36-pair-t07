"""Generational parameter registry.

Parameters live in the append only stream, so a value only becomes visible
once its write is committed.  Every change is stamped with the generation of
its scope, and a snapshot taken at generation *n* is refused once the scope
moves past *n*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from line_control.registry.generations import GenerationLedger
from line_control.runtime.clock import LogicalClock
from line_control.runtime.errors import (
    StaleCredentialError,
    UnknownReferenceError,
    ValidationError,
)
from line_control.runtime.keys import scope_key
from line_control.store.stream import RecordStream


@dataclass(frozen=True)
class Bounds:
    """Inclusive operating range of a parameter."""

    low: float
    high: float

    def check(self, value: float, name: str) -> None:
        """Refuse a value outside the range."""
        if value < self.low or value > self.high:
            raise ValidationError(
                f"parameter {name} is outside its operating range",
                name=name,
                value=value,
                low=self.low,
                high=self.high,
            )

    def to_dict(self) -> dict[str, float]:
        """Render the range for the wire."""
        return {"low": self.low, "high": self.high}


@dataclass(frozen=True)
class ParameterSpec:
    """Declared shape of one parameter."""

    scope: str
    name: str
    kind: str = "int"
    default: Any = 0
    bounds: Bounds | None = None
    unit: str = ""

    def key(self) -> str:
        """Return the stream key this parameter is stored under."""
        return scope_key("param", self.scope, self.name)

    def to_dict(self) -> dict[str, Any]:
        """Render the declaration for the wire."""
        payload: dict[str, Any] = {
            "scope": self.scope,
            "name": self.name,
            "kind": self.kind,
            "default": self.default,
            "unit": self.unit,
        }
        if self.bounds is not None:
            payload["bounds"] = self.bounds.to_dict()
        return payload


@dataclass(frozen=True)
class Parameter:
    """One effective parameter value together with its provenance."""

    scope: str
    name: str
    value: Any
    generation: int
    tick: int

    def to_dict(self) -> dict[str, Any]:
        """Render the value for the wire."""
        return {
            "scope": self.scope,
            "name": self.name,
            "value": self.value,
            "generation": self.generation,
            "tick": self.tick,
        }


@dataclass(frozen=True)
class ParameterSnapshot:
    """A detached copy of every parameter in one scope."""

    scope: str
    generation: int
    tick: int
    values: Mapping[str, Any] = field(default_factory=dict)

    def is_stale(self, current_generation: int) -> bool:
        """Report whether the scope has moved past this snapshot."""
        return self.generation != current_generation

    def to_dict(self) -> dict[str, Any]:
        """Render the snapshot for the wire."""
        return {
            "scope": self.scope,
            "generation": self.generation,
            "tick": self.tick,
            "values": dict(self.values),
        }


class ParameterRegistry:
    """Holds declared parameter shapes and their effective values."""

    def __init__(
        self,
        stream: RecordStream,
        clock: LogicalClock,
        ledger: GenerationLedger,
    ) -> None:
        self._stream = stream
        self._clock = clock
        self._ledger = ledger
        self._specs: dict[tuple[str, str], ParameterSpec] = {}

    # ----------------------------------------------------------- declaration
    def declare(self, spec: ParameterSpec) -> ParameterSpec:
        """Register the declared shape of a parameter."""
        if not spec.scope or not spec.name:
            raise ValidationError("parameter scope and name are required")
        if spec.kind not in {"int", "float", "text", "bool"}:
            raise ValidationError("unsupported parameter kind", kind=spec.kind)
        self._specs[(spec.scope, spec.name)] = spec
        return spec

    def declared(self, scope: str | None = None) -> list[ParameterSpec]:
        """Return declared shapes, optionally narrowed to one scope."""
        specs = [
            spec
            for (spec_scope, _), spec in self._specs.items()
            if scope is None or spec_scope == scope
        ]
        return sorted(specs, key=lambda spec: (spec.scope, spec.name))

    def spec(self, scope: str, name: str) -> ParameterSpec:
        """Return the declaration of one parameter."""
        try:
            return self._specs[(scope, name)]
        except KeyError as missing:
            raise UnknownReferenceError(
                f"parameter {name} is not declared in scope {scope}",
                scope=scope,
                name=name,
            ) from missing

    # ------------------------------------------------------------ read paths
    def get(self, scope: str, name: str) -> Parameter:
        """Return the effective value of a parameter."""
        spec = self.spec(scope, name)
        record = self._stream.visible_view().current(spec.key())
        if record is None:
            return Parameter(scope, name, spec.default, 0, 0)
        return Parameter(
            scope=scope,
            name=name,
            value=record.payload.get("value", spec.default),
            generation=record.generation,
            tick=record.tick,
        )

    def value(self, scope: str, name: str) -> Any:
        """Return just the effective value of a parameter."""
        return self.get(scope, name).value

    def values(self, scope: str) -> dict[str, Any]:
        """Return every effective value in one scope."""
        return {
            spec.name: self.get(spec.scope, spec.name).value
            for spec in self.declared(scope)
        }

    def generation(self, scope: str) -> int:
        """Return the generation the scope currently carries."""
        return self._ledger.current(scope)

    # ----------------------------------------------------------- write paths
    def set(self, scope: str, name: str, value: Any) -> Parameter:
        """Store a new value and make it visible in one commit."""
        spec = self.spec(scope, name)
        coerced = self._coerce(spec, value)
        generation = self._ledger.current(scope)
        record = self._stream.append(
            "param.set",
            spec.key(),
            {"scope": scope, "name": name, "value": coerced, "kind": spec.kind},
            generation=generation,
        )
        self._stream.commit_upto(record.seq)
        return Parameter(scope, name, coerced, generation, record.tick)

    def set_many(self, scope: str, values: Mapping[str, Any]) -> int:
        """Store several values in one commit."""
        generation = self._ledger.current(scope)
        last = 0
        for name, raw in sorted(values.items()):
            spec = self.spec(scope, name)
            coerced = self._coerce(spec, raw)
            record = self._stream.append(
                "param.set",
                spec.key(),
                {"scope": scope, "name": name, "value": coerced, "kind": spec.kind},
                generation=generation,
            )
            last = record.seq
        if last:
            self._stream.commit_upto(last)
        return last

    def bump(self, scope: str) -> int:
        """Advance the scope generation, invalidating older artefacts."""
        generation = self._ledger.bump(scope)
        record = self._stream.append(
            "param.epoch",
            scope_key("epoch", scope),
            {"scope": scope, "generation": generation},
            generation=generation,
        )
        self._stream.commit_upto(record.seq)
        return generation

    # -------------------------------------------------------------- snapshots
    def snapshot(self, scope: str) -> ParameterSnapshot:
        """Capture every effective value in one scope."""
        return ParameterSnapshot(
            scope=scope,
            generation=self._ledger.current(scope),
            tick=self._clock.current,
            values=self.values(scope),
        )

    def restore(self, snapshot: ParameterSnapshot) -> int:
        """Reapply a snapshot, refusing one the scope has moved past."""
        self.assert_fresh(snapshot)
        last = 0
        for name, raw in sorted(snapshot.values.items()):
            spec = self._specs.get((snapshot.scope, name))
            if spec is None:
                continue
            coerced = self._coerce(spec, raw)
            record = self._stream.append(
                "param.restore",
                spec.key(),
                {"scope": snapshot.scope, "name": name, "value": coerced},
                generation=snapshot.generation,
            )
            last = record.seq
        if last:
            self._stream.commit_upto(last)
        return last

    def assert_fresh(self, snapshot: ParameterSnapshot) -> None:
        """Refuse a snapshot that belongs to an older generation."""
        current = self._ledger.current(snapshot.scope)
        if snapshot.is_stale(current):
            raise StaleCredentialError(
                f"snapshot for scope {snapshot.scope} is stale",
                scope=snapshot.scope,
                snapshot_generation=snapshot.generation,
                current_generation=current,
            )

    # ------------------------------------------------------------ validation
    def _coerce(self, spec: ParameterSpec, value: Any) -> Any:
        if spec.kind == "int":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(
                    f"parameter {spec.name} expects a whole number",
                    name=spec.name,
                    value=value,
                )
            coerced: Any = int(value)
        elif spec.kind == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(
                    f"parameter {spec.name} expects a number",
                    name=spec.name,
                    value=value,
                )
            coerced = float(value)
        elif spec.kind == "bool":
            if not isinstance(value, bool):
                raise ValidationError(
                    f"parameter {spec.name} expects a boolean",
                    name=spec.name,
                    value=value,
                )
            coerced = bool(value)
        else:
            if not isinstance(value, str):
                raise ValidationError(
                    f"parameter {spec.name} expects text",
                    name=spec.name,
                    value=value,
                )
            coerced = value
        if spec.bounds is not None:
            spec.bounds.check(float(coerced), spec.name)
        return coerced
