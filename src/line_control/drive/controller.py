"""Rotor speed, load and the grid breaker.

Synchronising to the grid is the one step here with a hard precondition: the
rotor has to be inside the synchronising window, otherwise the breaker is
refused rather than closed blind.
"""

from __future__ import annotations

from typing import Any

from line_control.registry.parameters import Bounds, ParameterRegistry, ParameterSpec
from line_control.runtime.errors import (
    LimitViolationError,
    OrderingError,
    UnknownReferenceError,
)
from line_control.runtime.keys import scope_key
from line_control.store.stream import RecordStream

SYNC_SPEED = 2950
MAX_LOAD = 100


class DriveController:
    """Rotor speed, load and the breaker to the grid."""

    def __init__(
        self,
        stream: RecordStream,
        registry: ParameterRegistry,
    ) -> None:
        self._stream = stream
        self._registry = registry

    def scope(self, unit: str) -> str:
        """Return the parameter scope this module uses for a unit."""
        return scope_key("drive", unit)

    def declare_unit(self, unit: str) -> None:
        """Declare the tunable limits of one unit."""
        scope = self.scope(unit)
        self._registry.declare(
            ParameterSpec(scope, "sync_speed", "int", SYNC_SPEED, Bounds(2000, 3400), "rpm")
        )
        self._registry.declare(
            ParameterSpec(scope, "max_load", "int", MAX_LOAD, Bounds(0, 120), "percent")
        )
        self._registry.declare(
            ParameterSpec(
                scope, "nominal_voltage", "int", 10_500, Bounds(0, 40_000), "volt"
            )
        )

    def _limit(self, unit: str, name: str, fallback: Any) -> Any:
        try:
            return self._registry.value(self.scope(unit), name)
        except UnknownReferenceError:
            return fallback

    # ------------------------------------------------------------ read paths
    def speed(self, unit: str) -> int:
        """Return the current rotor speed."""
        record = self._stream.visible_view().current(scope_key("drive", unit, "speed"))
        if record is None:
            return 0
        return int(record.payload.get("value", 0))

    def frequency(self, unit: str) -> float:
        """Return the electrical frequency implied by the rotor speed."""
        return self.speed(unit) / 60.0

    def load(self, unit: str) -> int:
        """Return the current load."""
        record = self._stream.visible_view().current(scope_key("drive", unit, "load"))
        if record is None:
            return 0
        return int(record.payload.get("value", 0))

    def coasting(self, unit: str) -> bool:
        """Report whether the rotor is coasting down."""
        record = self._stream.visible_view().current(scope_key("drive", unit, "coasting"))
        return bool(record and record.payload.get("value"))

    def slow_enough(self, unit: str, threshold: int) -> bool:
        """Report whether the rotor is at or below a threshold."""
        return self.speed(unit) <= int(threshold)

    def sync_speed(self, unit: str) -> int:
        """Return the rotor speed required before synchronising."""
        return int(self._limit(unit, "sync_speed", SYNC_SPEED))

    def max_load(self, unit: str) -> int:
        """Return the load ceiling of the unit."""
        return int(self._limit(unit, "max_load", MAX_LOAD))

    def grid_connected(self, unit: str) -> bool:
        """Report whether the breaker is closed to the grid."""
        record = self._stream.visible_view().current(scope_key("drive", unit, "breaker"))
        return bool(record and record.payload.get("state") == "connected")

    def grid_label(self, unit: str) -> str:
        """Return the operator supplied grid label."""
        record = self._stream.visible_view().current(scope_key("drive", unit, "gridLabel"))
        if record is None:
            return ""
        return str(record.payload.get("value", ""))

    def grid_voltage(self, unit: str) -> int:
        """Return the recorded grid voltage."""
        record = self._stream.visible_view().current(scope_key("drive", unit, "voltage"))
        if record is None:
            return 0
        return int(record.payload.get("value", 0))

    def status(self, unit: str) -> dict[str, Any]:
        """Return a snapshot of the drive module for one unit."""
        return {
            "unit": unit,
            "speed": self.speed(unit),
            "frequency": self.frequency(unit),
            "load": self.load(unit),
            "coasting": self.coasting(unit),
            "syncSpeed": self.sync_speed(unit),
            "breaker": "connected" if self.grid_connected(unit) else "open",
            "gridLabel": self.grid_label(unit),
            "gridVoltage": self.grid_voltage(unit),
        }

    # ----------------------------------------------------------- write paths
    def set_speed(self, unit: str, rotor_rpm: int) -> dict[str, Any]:
        """Record a rotor speed, refusing a negative value."""
        if int(rotor_rpm) < 0:
            raise LimitViolationError(
                "rotor speed cannot be negative",
                unit=unit,
                value=int(rotor_rpm),
                low=0,
                high=20_000,
            )
        record = self._stream.append(
            "drive.speed",
            scope_key("drive", unit, "speed"),
            {"unit": unit, "value": int(rotor_rpm)},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def set_load(self, unit: str, load: int) -> dict[str, Any]:
        """Record a load, refusing one above the unit ceiling."""
        ceiling = self.max_load(unit)
        if not 0 <= int(load) <= ceiling:
            raise LimitViolationError(
                f"load {load} is outside 0..{ceiling}",
                unit=unit,
                value=int(load),
                low=0,
                high=ceiling,
            )
        record = self._stream.append(
            "drive.load",
            scope_key("drive", unit, "load"),
            {"unit": unit, "value": int(load)},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def coast_down(self, unit: str) -> dict[str, Any]:
        """Mark the rotor as coasting."""
        record = self._stream.append(
            "drive.coast",
            scope_key("drive", unit, "coasting"),
            {"unit": unit, "value": True},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def end_coast(self, unit: str) -> dict[str, Any]:
        """Clear the coasting flag once the rotor has stopped."""
        record = self._stream.append(
            "drive.coast",
            scope_key("drive", unit, "coasting"),
            {"unit": unit, "value": False},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def sync_grid(self, unit: str) -> dict[str, Any]:
        """Close the breaker once the rotor is inside the synchronising window."""
        speed = self.speed(unit)
        window = self.sync_speed(unit)
        if speed < window:
            raise OrderingError(
                f"breaker refused: rotor at {speed} rpm is below the window of {window}",
                unit=unit,
                speed=speed,
                window=window,
                stage="sync",
            )
        record = self._stream.append(
            "drive.sync",
            scope_key("drive", unit, "breaker"),
            {"unit": unit, "state": "connected", "speed": speed},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def trip_breaker(self, unit: str) -> dict[str, Any]:
        """Open the breaker."""
        record = self._stream.append(
            "drive.trip",
            scope_key("drive", unit, "breaker"),
            {"unit": unit, "state": "tripped"},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def set_grid_label(self, unit: str, label: str) -> dict[str, Any]:
        """Record an operator supplied grid label."""
        record = self._stream.append(
            "drive.gridLabel",
            scope_key("drive", unit, "gridLabel"),
            {"unit": unit, "value": str(label)},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def set_grid_voltage(self, unit: str, volts: int) -> dict[str, Any]:
        """Record a grid voltage reading."""
        record = self._stream.append(
            "drive.voltage",
            scope_key("drive", unit, "voltage"),
            {"unit": unit, "value": int(volts)},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def set_sync_speed(self, unit: str, rotor_rpm: int) -> int:
        """Pin the synchronising window."""
        self._registry.set(self.scope(unit), "sync_speed", int(rotor_rpm))
        return int(rotor_rpm)

    def set_max_load(self, unit: str, load: int) -> int:
        """Pin the load ceiling."""
        self._registry.set(self.scope(unit), "max_load", int(load))
        return int(load)
