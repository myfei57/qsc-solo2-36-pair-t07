"""Latches that must be deliberately cleared.

A latch records why it was set.  Clearing it is a decision, not a side effect:
the caller has to assert that the underlying condition is gone, otherwise the
clear is refused and the latch stays engaged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from line_control.runtime.clock import LogicalClock
from line_control.runtime.errors import LatchEngagedError, ValidationError
from line_control.runtime.keys import scope_key
from line_control.store.stream import RecordStream


@dataclass(frozen=True)
class LatchState:
    """The current condition of one latch."""

    unit: str
    name: str
    engaged: bool
    reason: str
    set_tick: int
    clear_tick: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Render the latch for the wire."""
        return {
            "unit": self.unit,
            "name": self.name,
            "engaged": self.engaged,
            "reason": self.reason,
            "set_tick": self.set_tick,
            "clear_tick": self.clear_tick,
        }


class LatchBoard:
    """Every latch in the service, stored in the record stream."""

    def __init__(
        self, stream: RecordStream, clock: LogicalClock, prefix: str = "latch"
    ) -> None:
        self._stream = stream
        self._clock = clock
        self._prefix = prefix

    def key(self, unit: str, name: str) -> str:
        """Return the stream key of one latch."""
        return scope_key(self._prefix, name, unit)

    # ------------------------------------------------------------ read paths
    def read(self, unit: str, name: str) -> LatchState | None:
        """Return the latch state, or ``None`` when it was never touched."""
        record = self._stream.visible_view().current(self.key(unit, name))
        if record is None:
            return None
        payload = record.payload
        return LatchState(
            unit=unit,
            name=name,
            engaged=bool(payload.get("engaged", False)),
            reason=str(payload.get("reason", "")),
            set_tick=int(payload.get("set_tick", record.tick)),
            clear_tick=payload.get("clear_tick"),
        )

    def is_set(self, unit: str, name: str) -> bool:
        """Report whether the latch is currently engaged."""
        state = self.read(unit, name)
        return bool(state and state.engaged)

    def reason(self, unit: str, name: str) -> str:
        """Return why the latch was last set, or an empty string."""
        state = self.read(unit, name)
        if state is None or not state.engaged:
            return ""
        return state.reason

    def engaged(self, unit: str) -> list[str]:
        """Return every engaged latch of a unit, sorted by name."""
        prefix = scope_key(self._prefix) + ":"
        names: set[str] = set()
        for record in self._stream.visible():
            if not record.key.startswith(prefix):
                continue
            parts = record.key.split(":")
            if len(parts) == 3 and parts[2] == unit and bool(record.payload.get("engaged")):
                names.add(parts[1])
        return sorted(names)

    # ----------------------------------------------------------- write paths
    def set(self, unit: str, name: str, reason: str) -> LatchState:
        """Engage a latch and remember why."""
        if not unit or not name:
            raise ValidationError("latch unit and name are required")
        tick = self._clock.tick()
        record = self._stream.append(
            "latch.set",
            self.key(unit, name),
            {
                "unit": unit,
                "name": name,
                "engaged": True,
                "reason": reason,
                "set_tick": tick,
            },
        )
        self._stream.commit_upto(record.seq)
        return LatchState(unit, name, True, reason, tick)

    def clear(self, unit: str, name: str, released: bool, note: str = "") -> LatchState:
        """Release a latch only once the caller confirms the cause is gone."""
        state = self.read(unit, name)
        if state is None or not state.engaged:
            return LatchState(unit, name, False, "", 0)
        if not released:
            raise LatchEngagedError(
                f"latch {name} on unit {unit} cannot be cleared yet",
                unit=unit,
                name=name,
                reason=state.reason,
            )
        tick = self._clock.tick()
        record = self._stream.append(
            "latch.clear",
            self.key(unit, name),
            {
                "unit": unit,
                "name": name,
                "engaged": False,
                "reason": state.reason,
                "set_tick": state.set_tick,
                "clear_tick": tick,
                "note": note,
            },
        )
        self._stream.commit_upto(record.seq)
        return LatchState(unit, name, False, state.reason, state.set_tick, tick)

    def require_clear(self, unit: str, name: str) -> None:
        """Refuse when a latch is still engaged."""
        state = self.read(unit, name)
        if state is not None and state.engaged:
            raise LatchEngagedError(
                f"latch {name} is engaged on unit {unit}",
                unit=unit,
                name=name,
                reason=state.reason,
            )
