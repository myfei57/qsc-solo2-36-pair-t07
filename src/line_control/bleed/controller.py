"""Bleed valve control.

Closing the bleed valve is only allowed once the compressor state is durable
in the record stream, which is what keeps a crash from leaving the machine
with a closed bleed valve and no record of why.
"""

from __future__ import annotations

from typing import Any, Callable

from line_control.registry.parameters import Bounds, ParameterRegistry, ParameterSpec
from line_control.runtime.errors import (
    LimitViolationError,
    NotDurableError,
    UnknownReferenceError,
)
from line_control.runtime.keys import scope_key
from line_control.store.stream import RecordStream

TRAVEL_LOW = 0
TRAVEL_HIGH = 100


class BleedController:
    """Bleed valve position and the surge margin around it."""

    def __init__(
        self,
        stream: RecordStream,
        registry: ParameterRegistry,
        durable_probe: Callable[[str], bool],
        margin_probe: Callable[[str, int], float],
    ) -> None:
        self._stream = stream
        self._registry = registry
        self._durable_probe = durable_probe
        self._margin_probe = margin_probe

    def scope(self, unit: str) -> str:
        """Return the parameter scope this module uses for a unit."""
        return scope_key("bleed", unit)

    def declare_unit(self, unit: str) -> None:
        """Declare the tunable limits of one unit."""
        self._registry.declare(
            ParameterSpec(
                self.scope(unit),
                "position",
                "int",
                0,
                Bounds(TRAVEL_LOW, TRAVEL_HIGH),
                "percent",
            )
        )

    # ------------------------------------------------------------ read paths
    def _valve_key(self, unit: str) -> str:
        return scope_key("bleed", unit, "valve")

    def position(self, unit: str) -> int:
        """Return the commanded bleed position."""
        try:
            return int(self._registry.value(self.scope(unit), "position"))
        except UnknownReferenceError:
            return 0

    def valve_state(self, unit: str) -> str:
        """Return the valve state label."""
        record = self._stream.visible_view().current(self._valve_key(unit))
        if record is None:
            return "unknown"
        return str(record.payload.get("state", "unknown"))

    def open_position(self, unit: str) -> int:
        """Return the effective opening, full open when the valve is open."""
        if self.valve_state(unit) == "open":
            return TRAVEL_HIGH
        return self.position(unit)

    def durable(self, unit: str) -> bool:
        """Report whether the compressor state is durable for this unit."""
        return self._durable_probe(unit)

    def margin(self, unit: str, load: int) -> float:
        """Return the raw surge margin at a load."""
        return self._margin_probe(unit, int(load))

    def guard_margin(self, unit: str, load: int) -> float:
        """Return the surge margin, floored at zero."""
        return max(0.0, self.margin(unit, load))

    def status(self, unit: str) -> dict[str, Any]:
        """Return a snapshot of the bleed module for one unit."""
        return {
            "unit": unit,
            "valve": self.valve_state(unit),
            "position": self.position(unit),
            "openPosition": self.open_position(unit),
            "durable": self.durable(unit),
        }

    # ----------------------------------------------------------- write paths
    def adjust(self, unit: str, source: str, position: int) -> dict[str, Any]:
        """Move the valve and record which channel asked for it."""
        if not TRAVEL_LOW <= int(position) <= TRAVEL_HIGH:
            raise LimitViolationError(
                f"bleed position {position} is outside {TRAVEL_LOW}..{TRAVEL_HIGH}",
                unit=unit,
                value=int(position),
                low=TRAVEL_LOW,
                high=TRAVEL_HIGH,
            )
        self._registry.set(self.scope(unit), "position", int(position))
        record = self._stream.append(
            "bleed.adjust",
            scope_key("bleed", unit, "command"),
            {"unit": unit, "source": source, "value": int(position)},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def set_valve(self, unit: str, position: int) -> dict[str, Any]:
        """Move the valve on an operator request."""
        return self.adjust(unit, "operator", position)

    def open(self, unit: str) -> dict[str, Any]:
        """Drive the bleed valve fully open."""
        self._registry.set(self.scope(unit), "position", TRAVEL_HIGH)
        record = self._stream.append(
            "bleed.open",
            self._valve_key(unit),
            {"unit": unit, "state": "open", "position": TRAVEL_HIGH},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def close(self, unit: str) -> dict[str, Any]:
        """Close the bleed valve once the compressor state is durable."""
        if not self.durable(unit):
            raise NotDurableError(
                f"bleed close refused: compressor state is not durable for unit {unit}",
                unit=unit,
                prerequisite="compressor.durable",
            )
        self._registry.set(self.scope(unit), "position", TRAVEL_LOW)
        record = self._stream.append(
            "bleed.close",
            self._valve_key(unit),
            {"unit": unit, "state": "closed", "position": TRAVEL_LOW},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def follow_demand(self, unit: str, demand: int) -> dict[str, Any]:
        """Follow an automatic surge demand, never closing below it."""
        opening = max(int(demand), self.position(unit))
        return self.adjust(unit, "surge", min(opening, TRAVEL_HIGH))
