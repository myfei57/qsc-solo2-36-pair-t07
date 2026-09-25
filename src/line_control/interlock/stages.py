"""Multi stage ordering for one unit.

A sequence refuses two things: a step that skips over an intermediate stage,
and a step back to an earlier stage.  Stages listed as *resettable* may be
entered from anywhere, which is how a coast down or a trip is modelled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from line_control.runtime.clock import LogicalClock
from line_control.runtime.errors import (
    OrderingError,
    UnknownReferenceError,
    ValidationError,
)
from line_control.runtime.keys import scope_key
from line_control.store.stream import RecordStream


@dataclass(frozen=True)
class StageMove:
    """One recorded stage transition."""

    unit: str
    sequence: str
    previous: str
    current: str
    tick: int
    forced: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Render the move for the wire."""
        return {
            "unit": self.unit,
            "sequence": self.sequence,
            "previous": self.previous,
            "current": self.current,
            "tick": self.tick,
            "forced": self.forced,
        }


class StageSequence:
    """An ordered list of stages that a unit walks through."""

    def __init__(
        self,
        name: str,
        stages: Sequence[str],
        stream: RecordStream,
        clock: LogicalClock,
        resettable: Sequence[str] = (),
    ) -> None:
        if not name:
            raise ValidationError("sequence name is required")
        if len(stages) < 2:
            raise ValidationError("a sequence needs at least two stages", sequence=name)
        if len(set(stages)) != len(stages):
            raise ValidationError("stage names must be unique", sequence=name)
        self._name = name
        self._stages = tuple(stages)
        self._resettable = frozenset(resettable)
        unknown = self._resettable.difference(self._stages)
        if unknown:
            raise ValidationError(
                "resettable stages must exist in the sequence",
                sequence=name,
                unknown=sorted(unknown),
            )
        self._stream = stream
        self._clock = clock

    @property
    def name(self) -> str:
        return self._name

    @property
    def stages(self) -> tuple[str, ...]:
        return self._stages

    @property
    def first(self) -> str:
        return self._stages[0]

    def key(self, unit: str) -> str:
        """Return the stream key holding the current stage of a unit."""
        return scope_key("stage", self._name, unit)

    def index(self, stage: str) -> int:
        """Return the position of a stage, refusing an unknown name."""
        try:
            return self._stages.index(stage)
        except ValueError as missing:
            raise UnknownReferenceError(
                f"{stage} is not a stage of sequence {self._name}",
                sequence=self._name,
                stage=stage,
            ) from missing

    # ------------------------------------------------------------ read paths
    def current(self, unit: str) -> str:
        """Return the stage a unit currently sits in."""
        record = self._stream.visible_view().current(self.key(unit))
        if record is None:
            return self.first
        return str(record.payload.get("stage", self.first))

    def entered_tick(self, unit: str) -> int:
        """Return the tick at which the unit entered its current stage."""
        record = self._stream.visible_view().current(self.key(unit))
        if record is None:
            return 0
        return record.tick

    def position(self, unit: str) -> int:
        """Return the ordinal of the current stage."""
        return self.index(self.current(unit))

    def history(self, unit: str) -> list[StageMove]:
        """Return every recorded move of a unit, oldest first."""
        out: list[StageMove] = []
        for record in self._stream.visible("stage.move"):
            if record.key != self.key(unit):
                continue
            payload = record.payload
            out.append(
                StageMove(
                    unit=unit,
                    sequence=self._name,
                    previous=str(payload.get("previous", "")),
                    current=str(payload.get("stage", payload.get("current", ""))),
                    tick=record.tick,
                    forced=bool(payload.get("forced", False)),
                )
            )
        return out

    # ----------------------------------------------------------- write paths
    def advance(self, unit: str, stage: str, forced: bool = False) -> StageMove:
        """Move a unit to ``stage`` when the ordering allows it."""
        if not unit:
            raise ValidationError("unit is required")
        target = self.index(stage)
        previous = self.current(unit)
        origin = self.index(previous)
        if previous == stage:
            return StageMove(unit, self._name, previous, stage, self.entered_tick(unit))
        if not forced and stage not in self._resettable:
            if target < origin:
                raise OrderingError(
                    f"sequence {self._name} cannot move back from {previous} to {stage}",
                    sequence=self._name,
                    unit=unit,
                    previous=previous,
                    requested=stage,
                )
            if target > origin + 1:
                raise OrderingError(
                    f"sequence {self._name} cannot skip from {previous} to {stage}",
                    sequence=self._name,
                    unit=unit,
                    previous=previous,
                    requested=stage,
                    expected=self._stages[origin + 1],
                )
        record = self._stream.append(
            "stage.move",
            self.key(unit),
            {"unit": unit, "previous": previous, "stage": stage, "forced": forced},
        )
        self._stream.commit_upto(record.seq)
        return StageMove(
            unit=unit,
            sequence=self._name,
            previous=previous,
            current=stage,
            tick=record.tick,
            forced=forced,
        )

    def reset(self, unit: str) -> StageMove:
        """Force a unit back to the first stage."""
        return self.advance(unit, self.first, forced=True)

    def require(self, unit: str, stage: str) -> None:
        """Refuse unless the unit already sits in ``stage``."""
        current = self.current(unit)
        if current != stage:
            raise OrderingError(
                f"sequence {self._name} expected {stage} but the unit is in {current}",
                sequence=self._name,
                unit=unit,
                expected=stage,
                found=current,
            )

    def require_reached(self, unit: str, stage: str) -> None:
        """Refuse unless the unit has reached at least ``stage``."""
        if self.position(unit) < self.index(stage):
            raise OrderingError(
                f"sequence {self._name} has not reached {stage}",
                sequence=self._name,
                unit=unit,
                required=stage,
                found=self.current(unit),
            )
