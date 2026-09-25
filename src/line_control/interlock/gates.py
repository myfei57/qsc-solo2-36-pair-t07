"""Pre-gates: named preconditions that must hold before a step runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from line_control.runtime.errors import (
    GateBlockedError,
    UnknownReferenceError,
    ValidationError,
)


@dataclass(frozen=True)
class GateRequirement:
    """One named precondition of a gate."""

    name: str
    probe: Callable[[str], bool]
    detail: str = ""

    def satisfied(self, unit: str) -> bool:
        """Evaluate the precondition for a unit."""
        return bool(self.probe(unit))


@dataclass(frozen=True)
class GateVerdict:
    """The outcome of evaluating a gate."""

    gate: str
    unit: str
    open: bool
    blocked_by: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Render the verdict for the wire."""
        return {
            "gate": self.gate,
            "unit": self.unit,
            "open": self.open,
            "blockedBy": list(self.blocked_by),
        }


@dataclass
class _Gate:
    name: str
    requirements: list[GateRequirement] = field(default_factory=list)


class GateBoard:
    """A registry of named gates built from probe callables."""

    def __init__(self) -> None:
        self._gates: dict[str, _Gate] = {}

    def define(self, gate: str, requirements: Sequence[GateRequirement]) -> None:
        """Declare a gate and the preconditions it checks."""
        if not gate:
            raise ValidationError("gate name is required")
        if not requirements:
            raise ValidationError("a gate needs at least one requirement", gate=gate)
        self._gates[gate] = _Gate(name=gate, requirements=list(requirements))

    def names(self) -> list[str]:
        """Return every declared gate name."""
        return sorted(self._gates)

    def requirements(self, gate: str) -> list[str]:
        """Return the requirement names of a gate."""
        return [requirement.name for requirement in self._lookup(gate).requirements]

    def evaluate(self, gate: str, unit: str) -> GateVerdict:
        """Evaluate a gate without raising."""
        declared = self._lookup(gate)
        blocked = tuple(
            requirement.name
            for requirement in declared.requirements
            if not requirement.satisfied(unit)
        )
        return GateVerdict(gate=gate, unit=unit, open=not blocked, blocked_by=blocked)

    def require(self, gate: str, unit: str) -> GateVerdict:
        """Evaluate a gate and refuse the step when it is closed."""
        verdict = self.evaluate(gate, unit)
        if not verdict.open:
            raise GateBlockedError(
                f"gate {gate} is closed for unit {unit}",
                gate=gate,
                unit=unit,
                blocked_by=list(verdict.blocked_by),
            )
        return verdict

    def report(self, unit: str) -> list[GateVerdict]:
        """Evaluate every declared gate for one unit."""
        return [self.evaluate(gate, unit) for gate in self.names()]

    def _lookup(self, gate: str) -> _Gate:
        try:
            return self._gates[gate]
        except KeyError as missing:
            raise UnknownReferenceError(f"gate {gate} is not declared", gate=gate) from missing
