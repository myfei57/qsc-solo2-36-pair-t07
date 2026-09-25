"""One facade over sequences, latches and gates."""

from __future__ import annotations

from typing import Sequence

from line_control.interlock.gates import GateBoard, GateRequirement, GateVerdict
from line_control.interlock.latches import LatchBoard, LatchState
from line_control.interlock.stages import StageSequence
from line_control.runtime.clock import LogicalClock
from line_control.runtime.errors import UnknownReferenceError
from line_control.store.stream import RecordStream


class InterlockBoard:
    """The interlock surface every domain module talks to."""

    def __init__(self, stream: RecordStream, clock: LogicalClock) -> None:
        self._stream = stream
        self._clock = clock
        self._sequences: dict[str, StageSequence] = {}
        self._latches = LatchBoard(stream, clock)
        self._gates = GateBoard()

    @property
    def latches(self) -> LatchBoard:
        """Return the latch board."""
        return self._latches

    @property
    def gates(self) -> GateBoard:
        """Return the gate board."""
        return self._gates

    def sequence(
        self,
        name: str,
        stages: Sequence[str] | None = None,
        resettable: Sequence[str] = (),
    ) -> StageSequence:
        """Declare or return a named stage sequence."""
        existing = self._sequences.get(name)
        if existing is not None:
            return existing
        if stages is None:
            raise UnknownReferenceError(
                f"sequence {name} is not declared", sequence=name
            )
        created = StageSequence(
            name,
            stages,
            self._stream,
            self._clock,
            resettable=resettable,
        )
        self._sequences[name] = created
        return created

    def stages(self, name: str) -> StageSequence:
        """Return a declared sequence."""
        return self.sequence(name)

    def sequence_names(self) -> list[str]:
        """Return every declared sequence name."""
        return sorted(self._sequences)

    def define_gate(self, gate: str, requirements: Sequence[GateRequirement]) -> None:
        """Declare a pre-gate."""
        self._gates.define(gate, requirements)

    def require_gate(self, gate: str, unit: str) -> GateVerdict:
        """Refuse the step when a pre-gate is closed."""
        return self._gates.require(gate, unit)

    def gate_report(self, unit: str) -> list[GateVerdict]:
        """Evaluate every gate for one unit."""
        return self._gates.report(unit)

    def set_latch(self, unit: str, name: str, reason: str) -> LatchState:
        """Engage a latch."""
        return self._latches.set(unit, name, reason)

    def clear_latch(self, unit: str, name: str, released: bool, note: str = "") -> LatchState:
        """Release a latch once its cause is gone."""
        return self._latches.clear(unit, name, released, note=note)

    def require_latch_clear(self, unit: str, name: str) -> None:
        """Refuse when a latch is still engaged."""
        self._latches.require_clear(unit, name)
