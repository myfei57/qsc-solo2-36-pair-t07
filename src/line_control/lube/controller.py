"""Lubrication supply.

The oil has to be up before cranking and may only be stopped once the rotor
has slowed, so the module keeps both the pressure it established and the
speed it insists on before letting go.
"""

from __future__ import annotations

from typing import Any, Callable

from line_control.registry.parameters import Bounds, ParameterRegistry, ParameterSpec
from line_control.runtime.errors import (
    LimitViolationError,
    OrderingError,
    UnknownReferenceError,
)
from line_control.runtime.keys import scope_key
from line_control.store.stream import RecordStream

MIN_PRESSURE = 30
STOP_SPEED = 300


class LubeController:
    """Oil pressure, tank level and the shutdown interlock."""

    def __init__(
        self,
        stream: RecordStream,
        registry: ParameterRegistry,
        speed_probe: Callable[[str], int],
        slow_probe: Callable[[str, int], bool],
    ) -> None:
        self._stream = stream
        self._registry = registry
        self._speed_probe = speed_probe
        self._slow_probe = slow_probe

    def scope(self, unit: str) -> str:
        """Return the parameter scope this module uses for a unit."""
        return scope_key("lube", unit)

    def declare_unit(self, unit: str) -> None:
        """Declare the tunable limits of one unit."""
        scope = self.scope(unit)
        self._registry.declare(
            ParameterSpec(scope, "min_pressure", "int", MIN_PRESSURE, Bounds(5, 80), "bar")
        )
        self._registry.declare(
            ParameterSpec(scope, "stop_speed", "int", STOP_SPEED, Bounds(0, 1200), "rpm")
        )
        self._registry.declare(
            ParameterSpec(scope, "tank_min", "int", 20, Bounds(0, 100), "percent")
        )

    def _limit(self, unit: str, name: str, fallback: Any) -> Any:
        try:
            return self._registry.value(self.scope(unit), name)
        except UnknownReferenceError:
            return fallback

    # ------------------------------------------------------------ read paths
    def _state_key(self, unit: str) -> str:
        return scope_key("lube", unit, "state")

    def minimum(self, unit: str) -> int:
        """Return the minimum acceptable oil pressure."""
        return int(self._limit(unit, "min_pressure", MIN_PRESSURE))

    def stop_speed(self, unit: str) -> int:
        """Return the rotor speed below which the oil may stop."""
        return int(self._limit(unit, "stop_speed", STOP_SPEED))

    def state(self, unit: str) -> str:
        """Return the oil system state label."""
        record = self._stream.visible_view().current(self._state_key(unit))
        if record is None:
            return "idle"
        return str(record.payload.get("state", "idle"))

    def established(self, unit: str) -> bool:
        """Report whether oil pressure is established."""
        return self.state(unit) == "established"

    def stopped(self, unit: str) -> bool:
        """Report whether the oil supply has been stopped."""
        return self.state(unit) == "stopped"

    def pressure(self, unit: str) -> int:
        """Return the last recorded oil pressure."""
        record = self._stream.visible_view().current(scope_key("lube", unit, "pressure"))
        if record is None:
            return 0
        return int(record.payload.get("value", 0))

    def tank_level(self, unit: str) -> int:
        """Return the reservoir level."""
        record = self._stream.visible_view().current(scope_key("lube", unit, "tank"))
        if record is None:
            return 0
        return int(record.payload.get("value", 0))

    def tank_ok(self, unit: str) -> bool:
        """Report whether the reservoir is above its minimum."""
        return self.tank_level(unit) >= int(self._limit(unit, "tank_min", 20))

    def oil_temp(self, unit: str) -> int:
        """Return the recorded oil temperature."""
        record = self._stream.visible_view().current(scope_key("lube", unit, "oilTemp"))
        if record is None:
            return 0
        return int(record.payload.get("value", 0))

    def speed(self, unit: str) -> int:
        """Return the rotor speed the drive module reports."""
        return int(self._speed_probe(unit))

    def status(self, unit: str) -> dict[str, Any]:
        """Return a snapshot of the oil module for one unit."""
        return {
            "unit": unit,
            "state": self.state(unit),
            "established": self.established(unit),
            "stopped": self.stopped(unit),
            "pressure": self.pressure(unit),
            "minimum": self.minimum(unit),
            "tank": self.tank_level(unit),
            "tankOk": self.tank_ok(unit),
            "oilTemp": self.oil_temp(unit),
            "stopSpeed": self.stop_speed(unit),
            "speed": self.speed(unit),
        }

    # ----------------------------------------------------------- write paths
    def prelube(self, unit: str) -> dict[str, Any]:
        """Put the oil system into its pre-lube state."""
        self._write_state(unit, "prelube", "lube.prelube")
        return self.status(unit)

    def establish(self, unit: str, pressure: int) -> dict[str, Any]:
        """Establish oil pressure, refusing one below the minimum."""
        minimum = self.minimum(unit)
        if int(pressure) < minimum:
            raise LimitViolationError(
                f"oil pressure {pressure} is below the minimum {minimum} for unit {unit}",
                unit=unit,
                value=int(pressure),
                low=minimum,
                high=10_000,
            )
        first = self._stream.append(
            "lube.establish",
            self._state_key(unit),
            {"unit": unit, "state": "established"},
        )
        second = self._stream.append(
            "lube.establish",
            scope_key("lube", unit, "pressure"),
            {"unit": unit, "value": int(pressure)},
        )
        self._stream.commit_upto(max(first.seq, second.seq))
        return self.status(unit)

    def set_tank_level(self, unit: str, level: int) -> dict[str, Any]:
        """Record a reservoir level, refusing one outside 0..100."""
        if not 0 <= int(level) <= 100:
            raise LimitViolationError(
                f"reservoir level {level} is outside 0..100",
                unit=unit,
                value=int(level),
                low=0,
                high=100,
            )
        record = self._stream.append(
            "lube.tank",
            scope_key("lube", unit, "tank"),
            {"unit": unit, "value": int(level)},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def set_oil_temp(self, unit: str, temperature: int) -> dict[str, Any]:
        """Record an oil temperature."""
        record = self._stream.append(
            "lube.oilTemp",
            scope_key("lube", unit, "oilTemp"),
            {"unit": unit, "value": int(temperature)},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def stop(self, unit: str) -> dict[str, Any]:
        """Stop the oil supply once the rotor has slowed enough."""
        speed = self.speed(unit)
        threshold = self.stop_speed(unit)
        if not self._slow_probe(unit, threshold):
            raise OrderingError(
                f"oil stop refused: rotor is at {speed} rpm, above {threshold}",
                unit=unit,
                speed=speed,
                threshold=threshold,
            )
        self._write_state(unit, "stopped", "lube.stop")
        return self.status(unit)

    def _write_state(self, unit: str, state: str, kind: str) -> None:
        record = self._stream.append(
            kind,
            self._state_key(unit),
            {"unit": unit, "state": state},
        )
        self._stream.commit_upto(record.seq)
