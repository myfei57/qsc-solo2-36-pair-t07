"""Ignition sequence and its failure latch.

The start sequence is a walk through the ignition stage chain: crank, prove
the spark, open the feed, fire, prove the flame.  A stage that is skipped is
refused, and a start that ends without a flame engages a latch that has to be
cleared deliberately before the next attempt.
"""

from __future__ import annotations

from typing import Any, Callable

from line_control.interlock.board import InterlockBoard
from line_control.interlock.stages import StageMove
from line_control.registry.confirmations import ConfirmationBoard
from line_control.registry.parameters import Bounds, ParameterRegistry, ParameterSpec
from line_control.runtime.clock import LogicalClock
from line_control.runtime.errors import (
    LightOffFailedError,
    OrderingError,
    UnknownReferenceError,
)
from line_control.runtime.keys import scope_key
from line_control.store.stream import RecordStream

STAGES = ("idle", "crank", "spark", "fire", "flame", "run")
RESETTABLE = ("idle",)
SEQUENCE = "ignition"
LATCH = "ignition"
LIGHT_OFF_WINDOW = 60


class IgnitionController:
    """Drives the start chain and owns the ignition failure latch."""

    def __init__(
        self,
        stream: RecordStream,
        clock: LogicalClock,
        registry: ParameterRegistry,
        board: InterlockBoard,
        confirmations: ConfirmationBoard,
        lube_ready: Callable[[str], bool],
        seal_ready: Callable[[str], bool],
        feed_open: Callable[[str, str], dict[str, Any]],
        feed_valve_open: Callable[[str], bool],
    ) -> None:
        self._stream = stream
        self._clock = clock
        self._registry = registry
        self._board = board
        self._confirmations = confirmations
        self._lube_ready = lube_ready
        self._seal_ready = seal_ready
        self._feed_open = feed_open
        self._feed_valve_open = feed_valve_open

    def scope(self, unit: str) -> str:
        """Return the parameter scope this module uses for a unit."""
        return scope_key("ignition", unit)

    def declare_unit(self, unit: str) -> None:
        """Declare the tunable limits and the stage sequence of one unit."""
        scope = self.scope(unit)
        self._registry.declare(
            ParameterSpec(
                scope, "light_off_window", "int", LIGHT_OFF_WINDOW, Bounds(5, 600), "tick"
            )
        )
        self._registry.declare(
            ParameterSpec(scope, "spark_attempts", "int", 3, Bounds(1, 10), "count")
        )
        self._board.sequence(SEQUENCE, STAGES, resettable=RESETTABLE)

    def _limit(self, unit: str, name: str, fallback: Any) -> Any:
        try:
            return self._registry.value(self.scope(unit), name)
        except UnknownReferenceError:
            return fallback

    # ------------------------------------------------------------ read paths
    def sequence(self):
        """Return the ignition stage sequence."""
        return self._board.stages(SEQUENCE)

    def stage(self, unit: str) -> str:
        """Return the current ignition stage."""
        return self.sequence().current(unit)

    def history(self, unit: str) -> list[StageMove]:
        """Return every recorded move of the ignition chain."""
        return self.sequence().history(unit)

    def cranking(self, unit: str) -> bool:
        """Report whether the unit is cranking."""
        record = self._stream.visible_view().current(scope_key("ignition", unit, "crank"))
        return bool(record and record.payload.get("value"))

    def spark(self, unit: str) -> str:
        """Return the spark proof label."""
        record = self._stream.visible_view().current(scope_key("ignition", unit, "spark"))
        if record is None:
            return "unknown"
        return str(record.payload.get("state", "unknown"))

    def flame(self, unit: str) -> bool:
        """Report whether a flame is currently detected."""
        record = self._stream.visible_view().current(scope_key("ignition", unit, "flame"))
        return bool(record and record.payload.get("value"))

    def latched(self, unit: str) -> bool:
        """Report whether the ignition latch is engaged."""
        return self._board.latches.is_set(unit, LATCH)

    def latch_reason(self, unit: str) -> str:
        """Return why the ignition latch was set."""
        return self._board.latches.reason(unit, LATCH)

    def light_off_window(self, unit: str) -> int:
        """Return the light-off window of a unit."""
        return int(self._limit(unit, "light_off_window", LIGHT_OFF_WINDOW))

    def spark_attempts(self, unit: str) -> int:
        """Return how many spark attempts the unit allows."""
        return int(self._limit(unit, "spark_attempts", 3))

    def status(self, unit: str) -> dict[str, Any]:
        """Return a snapshot of the ignition module for one unit."""
        return {
            "unit": unit,
            "stage": self.stage(unit),
            "cranking": self.cranking(unit),
            "spark": self.spark(unit),
            "flame": self.flame(unit),
            "latched": self.latched(unit),
            "latchReason": self.latch_reason(unit),
            "lightOffWindow": self.light_off_window(unit),
            "sparkAttempts": self.spark_attempts(unit),
        }

    # ----------------------------------------------------------- write paths
    def crank(self, unit: str) -> dict[str, Any]:
        """Start cranking, refusing while oil pressure is not established."""
        if not self._lube_ready(unit):
            raise OrderingError(
                f"crank refused: oil pressure is not established for unit {unit}",
                unit=unit,
                missing="lube_established",
            )
        self.sequence().advance(unit, "crank")
        record = self._stream.append(
            "ignition.crank",
            scope_key("ignition", unit, "crank"),
            {"unit": unit, "value": True},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def spark_test(self, unit: str) -> dict[str, Any]:
        """Prove the igniter, refusing unless the unit is already cranking."""
        self.sequence().require(unit, "crank")
        self.sequence().advance(unit, "spark")
        record = self._stream.append(
            "ignition.spark",
            scope_key("ignition", unit, "spark"),
            {"unit": unit, "state": "ok"},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def fire(self, unit: str) -> dict[str, Any]:
        """Fire the igniter, refusing while the feed valve is closed."""
        self.sequence().advance(unit, "fire")
        if not self._feed_valve_open(unit):
            raise OrderingError(
                f"fire refused: the feed valve is closed for unit {unit}",
                unit=unit,
                missing="feed_open",
            )
        record = self._stream.append(
            "ignition.fire",
            scope_key("ignition", unit, "fired"),
            {"unit": unit, "value": True},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def report_flame(self, unit: str, detected: bool) -> dict[str, Any]:
        """Record whether a flame is visible."""
        record = self._stream.append(
            "ignition.flame",
            scope_key("ignition", unit, "flame"),
            {"unit": unit, "value": bool(detected), "tick": self._clock.current},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def verify_flame(self, unit: str) -> dict[str, Any]:
        """Confirm light-off, or latch the unit and refuse the start."""
        self.sequence().require_reached(unit, "fire")
        if not self.flame(unit):
            self._board.set_latch(unit, LATCH, "light-off failed")
            raise LightOffFailedError(
                f"light-off failed: no flame detected for unit {unit}",
                unit=unit,
                window=self.light_off_window(unit),
            )
        self.sequence().advance(unit, "flame")
        self.sequence().advance(unit, "run")
        record = self._stream.append(
            "ignition.crank",
            scope_key("ignition", unit, "crank"),
            {"unit": unit, "value": False},
        )
        self._stream.commit_upto(record.seq)
        return self.status(unit)

    def start(self, unit: str, ticket: str) -> dict[str, Any]:
        """Run the whole start chain and report where it ended."""
        self._board.require_latch_clear(unit, LATCH)
        if not self._seal_ready(unit):
            raise OrderingError(
                f"start refused: the seal is not established for unit {unit}",
                unit=unit,
                missing="seal_established",
            )
        self.sequence().require(unit, "idle")
        self.crank(unit)
        self.spark_test(unit)
        self._feed_open(unit, ticket)
        self.fire(unit)
        return self.status(unit)

    def fail_latch(self, unit: str, reason: str) -> dict[str, Any]:
        """Engage the ignition latch with a cause."""
        self._board.set_latch(unit, LATCH, reason)
        return self.status(unit)

    def reset_latch(self, unit: str, alarm_cleared: bool, note: str = "") -> dict[str, Any]:
        """Release the ignition latch once the cause is gone."""
        self._board.clear_latch(unit, LATCH, alarm_cleared, note=note or "ignition latch reset")
        return self.status(unit)

    def set_light_off_window(self, unit: str, window: int) -> int:
        """Pin the light-off window."""
        self._registry.set(self.scope(unit), "light_off_window", int(window))
        return int(window)

    def set_spark_attempts(self, unit: str, attempts: int) -> int:
        """Pin how many spark attempts a start may use."""
        self._registry.set(self.scope(unit), "spark_attempts", int(attempts))
        return int(attempts)
