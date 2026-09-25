"""Exhaust temperature sampling and protection.

An over-temperature sample does three things at once: it is recorded against
the window, it engages the protection latch, and it pulls the feed lower limit
to zero.  Clearing the alarm has to observe a temperature back inside the band
first, otherwise the latch stays engaged.
"""

from __future__ import annotations

from typing import Any

from line_control.feed.arbiter import PROTECTION, Demand
from line_control.feed.controller import FeedController
from line_control.interlock.board import InterlockBoard
from line_control.judgement.thresholds import Band, ThresholdVerdict, ThresholdWindow
from line_control.registry.parameters import Bounds, ParameterRegistry, ParameterSpec
from line_control.runtime.clock import LogicalClock
from line_control.runtime.errors import LatchEngagedError, UnknownReferenceError
from line_control.runtime.keys import scope_key
from line_control.store.stream import RecordStream

TEMP_LIMIT = 600
CHANNEL = "exhaust"
LATCH = "exhaust_overtemp"
WINDOW = 10


class CombustionController:
    """Exhaust temperature, its alarm latch and the protection demand."""

    def __init__(
        self,
        stream: RecordStream,
        clock: LogicalClock,
        registry: ParameterRegistry,
        board: InterlockBoard,
        feed: FeedController,
    ) -> None:
        self._stream = stream
        self._clock = clock
        self._registry = registry
        self._board = board
        self._feed = feed
        self._channels: dict[str, ThresholdWindow] = {}

    def scope(self, unit: str) -> str:
        """Return the parameter scope this module uses for a unit."""
        return scope_key("exhaust", unit)

    def declare_unit(self, unit: str, limit: int = TEMP_LIMIT) -> None:
        """Declare the tunable limits of one unit."""
        scope = self.scope(unit)
        self._registry.declare(
            ParameterSpec(scope, "temp_limit", "int", limit, Bounds(100, 900), "degree")
        )
        self._registry.declare(
            ParameterSpec(scope, "sensor_count", "int", 1, Bounds(1, 32), "count")
        )

    # ------------------------------------------------------------ read paths
    def _limit(self, unit: str, name: str, fallback: Any) -> Any:
        try:
            return self._registry.value(self.scope(unit), name)
        except UnknownReferenceError:
            return fallback

    def temp_limit(self, unit: str) -> int:
        """Return the over-temperature limit of a unit."""
        return int(self._limit(unit, "temp_limit", TEMP_LIMIT))

    def band(self, unit: str) -> Band:
        """Return the acceptable exhaust band of a unit."""
        return Band(low=0.0, high=float(self.temp_limit(unit)))

    def channel(self, unit: str) -> ThresholdWindow:
        """Return the sampling channel of a unit, creating it on first use."""
        existing = self._channels.get(unit)
        if existing is None:
            existing = ThresholdWindow(CHANNEL, self.band(unit), WINDOW, self._stream, self._clock)
            self._channels[unit] = existing
        return existing

    def channel_names(self) -> list[str]:
        """Return the units that already have a sampling channel."""
        return sorted(self._channels)

    def temperature(self, unit: str) -> int:
        """Return the most recent exhaust temperature."""
        samples = self.channel(unit).samples(unit, include_stale=True)
        if not samples:
            return 0
        return int(samples[-1].value)

    def average(self, unit: str) -> int:
        """Return the recorded average temperature, falling back to the latest."""
        record = self._stream.visible_view().current(scope_key("exhaust", unit, "average"))
        if record is None:
            return self.temperature(unit)
        return int(record.payload.get("value", 0))

    def sensor_count(self, unit: str) -> int:
        """Return how many exhaust sensors a unit reports."""
        return int(self._limit(unit, "sensor_count", 1))

    def alarm(self, unit: str) -> bool:
        """Report whether the over-temperature latch is engaged."""
        return self._board.latches.is_set(unit, LATCH)

    def alarm_reason(self, unit: str) -> str:
        """Return why the over-temperature latch was set."""
        return self._board.latches.reason(unit, LATCH)

    def trip_relay(self, unit: str) -> bool:
        """Report whether either protection relay is asserted."""
        return self.alarm(unit) or self._feed.latched(unit)

    def verdict(self, unit: str) -> ThresholdVerdict:
        """Return the windowed comparison of the exhaust channel."""
        return self.channel(unit).evaluate(unit)

    def reloadable(self, unit: str) -> bool:
        """Report whether the unit could be reloaded right now."""
        return not self.alarm(unit) and not self._feed.latched(unit)

    def status(self, unit: str) -> dict[str, Any]:
        """Return a snapshot of the exhaust module for one unit."""
        return {
            "unit": unit,
            "temperature": self.temperature(unit),
            "average": self.average(unit),
            "limit": self.temp_limit(unit),
            "sensors": self.sensor_count(unit),
            "alarm": self.alarm(unit),
            "alarmReason": self.alarm_reason(unit),
            "reloadable": self.reloadable(unit),
            "tripRelay": self.trip_relay(unit),
            "within": self.verdict(unit).within,
        }

    # ----------------------------------------------------------- write paths
    def sample(self, unit: str, temperature: int) -> dict[str, Any]:
        """Record one exhaust sample and apply the latch when it is over limit."""
        self.channel(unit).observe(unit, float(temperature))
        if int(temperature) > self.temp_limit(unit):
            self.alarm_latch(unit, "exhaust over-temperature")
        else:
            self.clear_alarm(unit)
        return self.status(unit)

    def protect(self, unit: str, temperature: int) -> Demand:
        """Fold an over-temperature excursion into the feed protection demand."""
        demand = self.protection_demand(unit, temperature)
        self.channel(unit).observe(unit, float(temperature))
        applied = self._feed.set_setpoint(unit, demand, source=PROTECTION)
        record = self._stream.append(
            "exhaust.protect",
            scope_key("exhaust", unit, "temperature"),
            {"unit": unit, "value": int(temperature), "demand": demand},
        )
        self._stream.commit_upto(record.seq)
        return applied

    def protection_demand(self, unit: str, temperature: int) -> int:
        """Return the feed demand an over-temperature reading calls for."""
        ceiling = self.temp_limit(unit)
        if int(temperature) <= ceiling:
            return 100
        excess = int(temperature) - ceiling
        return max(30, 100 - excess * 2)

    def set_average(self, unit: str, temperature: int) -> dict[str, Any]:
        """Record the averaged exhaust temperature."""
        record = self._stream.append(
            "exhaust.average",
            scope_key("exhaust", unit, "average"),
            {"unit": unit, "value": int(temperature)},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def set_sensor_count(self, unit: str, count: int) -> dict[str, Any]:
        """Record how many exhaust sensors are reporting."""
        self._registry.set(self.scope(unit), "sensor_count", int(count))
        return self.status(unit)

    def set_temp_limit(self, unit: str, limit: int) -> int:
        """Pin the over-temperature limit and retune the sampling band."""
        pinned = int(limit)
        self._registry.set(self.scope(unit), "temp_limit", pinned)
        self.channel(unit).set_band(self.band(unit))
        return pinned

    def alarm_latch(self, unit: str, reason: str) -> dict[str, Any]:
        """Engage the over-temperature latch and pull the feed limit to zero."""
        self._board.set_latch(unit, LATCH, reason)
        self._feed.lower_limit(unit, 0)
        return self.status(unit)

    def clear_alarm(self, unit: str) -> dict[str, Any]:
        """Release the over-temperature latch and restore the feed limit."""
        if self.alarm(unit):
            self._board.clear_latch(unit, LATCH, True, note="temperature back inside band")
        self._feed.raise_limit(unit, 100)
        return self.status(unit)

    def reset(self, unit: str, observed: int) -> dict[str, Any]:
        """Clear the alarm, refusing while the observed temperature is still hot."""
        if int(observed) > self.temp_limit(unit):
            raise LatchEngagedError(
                f"alarm reset refused: {observed} is still above the limit",
                unit=unit,
                name=LATCH,
                reason=self.alarm_reason(unit),
                observed=int(observed),
            )
        self.channel(unit).observe(unit, float(observed))
        self._board.clear_latch(
            unit, LATCH, True, note="operator reset with a cool temperature"
        )
        self._feed.raise_limit(unit, 100)
        self._feed.unlatch(unit)
        return self.status(unit)

    def reload(self, unit: str) -> dict[str, Any]:
        """Release the feed latch when the alarm is clear."""
        if self.alarm(unit):
            raise LatchEngagedError(
                f"reload refused: the exhaust alarm is active for unit {unit}",
                unit=unit,
                name=LATCH,
                reason=self.alarm_reason(unit),
            )
        self._feed.unlatch(unit)
        return self.status(unit)
