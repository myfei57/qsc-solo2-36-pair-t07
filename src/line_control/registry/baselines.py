"""Generation stamped baselines and the curve maths that reads them.

A baseline is only usable while the scope it belongs to still carries the
generation it was published under.  Publishing a new baseline for the scope,
or bumping the scope, makes the previous one stale, and stale baselines are
refused rather than silently reused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

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
class CurvePoint:
    """One breakpoint of a piecewise linear curve."""

    load: int
    value: float

    def to_dict(self) -> dict[str, float]:
        """Render the point for the wire."""
        return {"load": self.load, "value": self.value}


def interpolate(points: Sequence[CurvePoint], load: int) -> float:
    """Return the curve value at ``load``, holding both ends flat."""
    if not points:
        raise ValidationError("a curve needs at least one point")
    ordered = sorted(points, key=lambda point: point.load)
    if load <= ordered[0].load:
        return ordered[0].value
    for index in range(1, len(ordered)):
        previous = ordered[index - 1]
        following = ordered[index]
        if load <= following.load:
            span = following.load - previous.load
            if span <= 0:
                return following.value
            ratio = (load - previous.load) / span
            return previous.value + ratio * (following.value - previous.value)
    return ordered[-1].value


@dataclass(frozen=True)
class Baseline:
    """A named curve pinned to one generation of one scope."""

    name: str
    scope: str
    generation: int
    tick: int
    points: tuple[CurvePoint, ...]

    def value_at(self, load: int) -> float:
        """Evaluate the baseline at a load."""
        return interpolate(self.points, load)

    def is_stale(self, current_generation: int) -> bool:
        """Report whether the scope has moved past this baseline."""
        return self.generation != current_generation

    def to_dict(self) -> dict[str, Any]:
        """Render the baseline for the wire."""
        return {
            "name": self.name,
            "scope": self.scope,
            "generation": self.generation,
            "tick": self.tick,
            "points": [point.to_dict() for point in self.points],
        }


class BaselineBook:
    """Publishes baselines and refuses to serve stale ones."""

    def __init__(
        self,
        stream: RecordStream,
        clock: LogicalClock,
        ledger: GenerationLedger,
    ) -> None:
        self._stream = stream
        self._clock = clock
        self._ledger = ledger

    def publish(
        self,
        name: str,
        scope: str,
        points: Iterable[CurvePoint | tuple[int, float]],
    ) -> Baseline:
        """Store a baseline stamped with the current generation of ``scope``."""
        if not name or not scope:
            raise ValidationError("baseline name and scope are required")
        normalised: list[CurvePoint] = []
        for point in points:
            if isinstance(point, CurvePoint):
                normalised.append(point)
            else:
                load, value = point
                normalised.append(CurvePoint(load=int(load), value=float(value)))
        if len(normalised) < 2:
            raise ValidationError(
                "a baseline needs at least two points", name=name, scope=scope
            )
        normalised.sort(key=lambda point: point.load)
        generation = self._ledger.current(scope)
        record = self._stream.append(
            "baseline.publish",
            scope_key("baseline", scope, name),
            {
                "name": name,
                "scope": scope,
                "points": [point.to_dict() for point in normalised],
            },
            generation=generation,
        )
        self._stream.commit_upto(record.seq)
        return Baseline(
            name=name,
            scope=scope,
            generation=generation,
            tick=record.tick,
            points=tuple(normalised),
        )

    def read(self, scope: str, name: str) -> Baseline | None:
        """Return the stored baseline, or ``None`` when none was published."""
        record = self._stream.visible_view().current(scope_key("baseline", scope, name))
        if record is None:
            return None
        points = tuple(
            CurvePoint(load=int(point["load"]), value=float(point["value"]))
            for point in record.payload.get("points", [])
        )
        return Baseline(
            name=name,
            scope=scope,
            generation=record.generation,
            tick=record.tick,
            points=points,
        )

    def require(self, scope: str, name: str) -> Baseline:
        """Return a baseline, refusing one the scope has moved past."""
        baseline = self.read(scope, name)
        if baseline is None:
            raise UnknownReferenceError(
                f"baseline {name} was never published for scope {scope}",
                name=name,
                scope=scope,
            )
        current = self._ledger.current(scope)
        if baseline.is_stale(current):
            raise StaleCredentialError(
                f"baseline {name} is stale for scope {scope}",
                name=name,
                scope=scope,
                baseline_generation=baseline.generation,
                current_generation=current,
            )
        return baseline

    def generations(self, scope: str) -> list[int]:
        """Return the generation of every baseline ever published for a scope."""
        prefix = scope_key("baseline", scope, "")
        out = [
            record.generation
            for record in self._stream.visible("baseline.publish")
            if record.key.startswith(prefix)
        ]
        return sorted(out)
