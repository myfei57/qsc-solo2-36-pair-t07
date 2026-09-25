"""Seal gas supply for one unit.

The seal has to be established before the feed valve may move, so the whole
module exists to answer one question with evidence: is the seal up, and at
what pressure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from line_control.registry.parameters import Bounds, ParameterRegistry, ParameterSpec
from line_control.runtime.errors import LimitViolationError, UnknownReferenceError
from line_control.runtime.keys import scope_key
from line_control.store.stream import RecordStream

MIN_PRESSURE = 20


@dataclass(frozen=True)
class SealStatus:
    """The seal condition of one unit."""

    unit: str
    established: bool
    pressure: int
    minimum: int
    margin: int

    @property
    def ready(self) -> bool:
        """Report whether the seal is up and above its minimum pressure."""
        return self.established and self.pressure >= self.minimum

    def to_dict(self) -> dict[str, Any]:
        """Render the status for the wire."""
        return {
            "unit": self.unit,
            "established": self.established,
            "pressure": self.pressure,
            "minimum": self.minimum,
            "margin": self.margin,
            "ready": self.ready,
        }


class SealController:
    """Tracks seal pressure and the established flag."""

    def __init__(
        self,
        stream: RecordStream,
        registry: ParameterRegistry,
    ) -> None:
        self._stream = stream
        self._registry = registry

    def scope(self, unit: str) -> str:
        """Return the parameter scope this module uses for a unit."""
        return scope_key("seal", unit)

    def declare_unit(self, unit: str) -> None:
        """Declare the tunable limits of one unit."""
        self._registry.declare(
            ParameterSpec(
                scope=self.scope(unit),
                name="min_pressure",
                kind="int",
                default=MIN_PRESSURE,
                bounds=Bounds(5, 80),
                unit="bar",
            )
        )

    # ------------------------------------------------------------ read paths
    def _state_key(self, unit: str) -> str:
        return scope_key("seal", unit, "state")

    def _pressure_key(self, unit: str) -> str:
        return scope_key("seal", unit, "pressure")

    def minimum(self, unit: str) -> int:
        """Return the minimum acceptable seal pressure."""
        try:
            return int(self._registry.value(self.scope(unit), "min_pressure"))
        except UnknownReferenceError:
            return MIN_PRESSURE

    def established(self, unit: str) -> bool:
        """Report whether the seal is established."""
        record = self._stream.visible_view().current(self._state_key(unit))
        return bool(record and record.payload.get("state") == "established")

    def pressure(self, unit: str) -> int:
        """Return the last recorded seal pressure."""
        record = self._stream.visible_view().current(self._pressure_key(unit))
        if record is None:
            return 0
        return int(record.payload.get("value", 0))

    def margin(self, unit: str) -> int:
        """Return how far the seal pressure sits above its minimum."""
        return max(0, self.pressure(unit) - self.minimum(unit))

    def status(self, unit: str) -> SealStatus:
        """Return the full seal condition of a unit."""
        return SealStatus(
            unit=unit,
            established=self.established(unit),
            pressure=self.pressure(unit),
            minimum=self.minimum(unit),
            margin=self.margin(unit),
        )

    # ----------------------------------------------------------- write paths
    def establish(self, unit: str, pressure: int) -> SealStatus:
        """Establish the seal, refusing a pressure below the minimum."""
        minimum = self.minimum(unit)
        if int(pressure) < minimum:
            raise LimitViolationError(
                f"seal pressure {pressure} is below the minimum {minimum} for unit {unit}",
                unit=unit,
                value=int(pressure),
                low=minimum,
                high=10_000,
            )
        self._write(unit, "established", int(pressure), kind="seal.establish")
        return self.status(unit)

    def relieve(self, unit: str) -> SealStatus:
        """Drop the seal and record the pressure as zero."""
        self._write(unit, "relieved", 0, kind="seal.relieve")
        return self.status(unit)

    def trim(self, unit: str, pressure: int) -> SealStatus:
        """Adjust the recorded pressure without changing the established flag."""
        if int(pressure) < 0:
            raise LimitViolationError(
                "seal pressure cannot be negative",
                unit=unit,
                value=int(pressure),
                low=0,
                high=10_000,
            )
        record = self._stream.append(
            "seal.trim",
            self._pressure_key(unit),
            {"unit": unit, "value": int(pressure)},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def _write(self, unit: str, state: str, pressure: int, kind: str) -> None:
        self._stream.append(
            kind,
            self._state_key(unit),
            {"unit": unit, "state": state},
        )
        second = self._stream.append(
            kind,
            self._pressure_key(unit),
            {"unit": unit, "value": pressure},
        )
        self._stream.commit_upto(second.seq)
