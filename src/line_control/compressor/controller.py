"""Compressor state, durability and calibration.

Persisting the compressor state is what makes the bleed valve allowed to
close and what opens the feed gate, so the persist path writes its records and
the commit that covers them in one step and reports the resulting watermark.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from line_control.compressor.curve import (
    default_points,
    normalise_points,
    surge_line,
)
from line_control.interlock.board import InterlockBoard
from line_control.interlock.stages import StageMove
from line_control.judgement.queries import RecordQuery
from line_control.registry.baselines import Baseline, CurvePoint
from line_control.registry.confirmations import ConfirmationBoard
from line_control.registry.parameters import Bounds, ParameterRegistry, ParameterSpec
from line_control.runtime.clock import LogicalClock
from line_control.runtime.errors import (
    LimitViolationError,
    UnknownReferenceError,
)
from line_control.runtime.keys import scope_key
from line_control.store.records import Record
from line_control.store.stream import RecordStream

STAGES = ("idle", "start", "ramp", "full", "coast")
RESETTABLE = ("coast",)
SEQUENCE = "compressor"
BASELINE_NAME = "efficiency"
RECALIBRATE_SUBJECT = "compressor.recalibrate"
DEFAULT_MAX_LOAD = 100
DEFAULT_MARGIN_FLOOR = 0.05
# Bleed demand law of the surge protection: the demand sits at the base
# opening while the margin holds at the floor, opens further as the margin
# falls through the floor and backs off once the margin clears the floor by
# a full proportional band.
SURGE_BASE_OPENING = 60
SURGE_PROPORTIONAL_BAND = 0.25


@dataclass(frozen=True)
class PersistReceipt:
    """Proof that a compressor state reached the record stream."""

    unit: str
    stage: str
    load: int
    rotor_rpm: int
    watermark: int
    tick: int

    def to_dict(self) -> dict[str, Any]:
        """Render the receipt for the wire."""
        return {
            "unit": self.unit,
            "stage": self.stage,
            "load": self.load,
            "rotorRpm": self.rotor_rpm,
            "watermark": self.watermark,
            "tick": self.tick,
            "durable": True,
        }


class CompressorController:
    """Walks the compressor through its stages and keeps the baseline book."""

    def __init__(
        self,
        stream: RecordStream,
        clock: LogicalClock,
        registry: ParameterRegistry,
        board: InterlockBoard,
        confirmations: ConfirmationBoard,
        baselines,
    ) -> None:
        self._stream = stream
        self._clock = clock
        self._registry = registry
        self._board = board
        self._confirmations = confirmations
        self._baselines = baselines

    def scope(self, unit: str) -> str:
        """Return the parameter scope this module uses for a unit."""
        return scope_key("compressor", unit)

    def declare_unit(self, unit: str) -> None:
        """Declare the tunable limits and the stage sequence of one unit."""
        scope = self.scope(unit)
        self._registry.declare(
            ParameterSpec(scope, "max_load", "int", DEFAULT_MAX_LOAD, Bounds(0, 100), "percent")
        )
        self._registry.declare(
            ParameterSpec(
                scope, "margin_floor", "float", DEFAULT_MARGIN_FLOOR, Bounds(0.0, 0.5), "ratio"
            )
        )
        self._board.sequence(SEQUENCE, STAGES, resettable=RESETTABLE)

    # ------------------------------------------------------------ read paths
    def sequence(self):
        """Return the compressor stage sequence."""
        return self._board.stages(SEQUENCE)

    def _limit(self, unit: str, name: str, fallback: Any) -> Any:
        try:
            return self._registry.value(self.scope(unit), name)
        except UnknownReferenceError:
            return fallback

    def stage(self, unit: str) -> str:
        """Return the current compressor stage."""
        return self.sequence().current(unit)

    def stage_history(self, unit: str) -> list[StageMove]:
        """Return every recorded stage move of a unit."""
        return self.sequence().history(unit)

    def _durable_record(self, unit: str) -> Record | None:
        """Return the committed durable marker of a unit, if one exists."""
        return self._stream.visible_view().current(scope_key("compressor", unit, "durable"))

    def durable(self, unit: str) -> bool:
        """Report whether a committed compressor state exists for a unit."""
        return self._durable_record(unit) is not None

    def durable_mark(self, unit: str) -> int:
        """Return the watermark the durable marker points at."""
        record = self._durable_record(unit)
        if record is None:
            return 0
        return int(record.payload.get("mark", 0))

    def persisted_state(self, unit: str) -> dict[str, Any]:
        """Return the state that was written by the last persist call."""
        query = RecordQuery(unit=unit, kind="compressor.persist")
        records = query.apply(self._stream.visible())
        if not records:
            return {}
        return dict(records[-1].payload)

    def max_load(self, unit: str) -> int:
        """Return the load ceiling of a unit."""
        return int(self._limit(unit, "max_load", DEFAULT_MAX_LOAD))

    def margin_floor(self, unit: str) -> float:
        """Return the margin below which the bleed valve is driven open."""
        return float(self._limit(unit, "margin_floor", DEFAULT_MARGIN_FLOOR))

    # ------------------------------------------------------------- baselines
    def publish_default_baseline(self, unit: str) -> Baseline:
        """Publish the shipped efficiency curve for a unit."""
        return self._baselines.publish(BASELINE_NAME, self.scope(unit), default_points())

    def baseline(self, unit: str) -> Baseline:
        """Return the fresh baseline of a unit, refusing a stale one."""
        return self._baselines.require(self.scope(unit), BASELINE_NAME)

    def baseline_points(self, unit: str) -> list[CurvePoint]:
        """Return the points of the fresh baseline of a unit."""
        return list(self.baseline(unit).points)

    def baseline_generation(self, unit: str) -> int:
        """Return the generation the current baseline was published under."""
        return self.baseline(unit).generation

    def recalibrate(self, unit: str, ticket: str, raw_points: Iterable[Any]) -> Baseline:
        """Replace the baseline, stamping it with a new generation."""
        points: Sequence[CurvePoint] = normalise_points(raw_points)
        self._confirmations.consume(ticket, subject=RECALIBRATE_SUBJECT)
        self._registry.bump(self.scope(unit))
        return self._baselines.publish(BASELINE_NAME, self.scope(unit), points)

    def retire_calibration(self, unit: str) -> int:
        """Move the scope on so the published baseline becomes stale."""
        return self._registry.bump(self.scope(unit))

    def margin(self, unit: str, load: int) -> float:
        """Return the surge margin at a load using the fresh baseline."""
        return self.baseline(unit).value_at(int(load)) - surge_line(int(load))

    # ------------------------------------------------------------- surge math
    def surge_target(self, unit: str, load: int) -> int:
        """Return the minimum bleed opening the surge margin calls for."""
        deficit = self.margin_floor(unit) - self.margin(unit, load)
        target = SURGE_BASE_OPENING + 100.0 * deficit / SURGE_PROPORTIONAL_BAND
        return int(min(100, max(0, round(target))))

    def surge_demand(self, unit: str, load: int, current: int) -> int:
        """Return the bleed demand, never asking for less than the current opening."""
        target = self.surge_target(unit, int(load))
        return max(target, int(current))

    def record_surge(self, unit: str) -> int:
        """Count one surge event against a unit."""
        count = self.surge_count(unit) + 1
        record = self._stream.append(
            "compressor.surge",
            scope_key("compressor", unit, "surge"),
            {"unit": unit, "count": count},
        )
        self._stream.commit_upto(record.seq)
        return count

    def surge_count(self, unit: str) -> int:
        """Return how many surge events a unit has recorded."""
        query = RecordQuery(unit=unit, kind="compressor.surge")
        return len(query.apply(self._stream.visible()))

    # ----------------------------------------------------------- write paths
    def persist(self, unit: str, stage: str, load: int, rotor_rpm: int) -> PersistReceipt:
        """Write a compressor state and commit it in one step."""
        self.sequence().index(stage)
        if not 0 <= int(load) <= 100:
            raise LimitViolationError(
                f"compressor load {load} is outside 0..100",
                unit=unit,
                value=int(load),
                low=0,
                high=100,
            )
        if int(rotor_rpm) < 0:
            raise LimitViolationError(
                "rotor speed cannot be negative",
                unit=unit,
                value=int(rotor_rpm),
                low=0,
                high=20000,
            )
        first = self._stream.append(
            "compressor.persist",
            scope_key("compressor", unit, "state"),
            {
                "unit": unit,
                "stage": stage,
                "load": int(load),
                "rotorRpm": int(rotor_rpm),
            },
        )
        watermark = self._stream.commit_upto(first.seq)
        marker = self._stream.append(
            "compressor.durable",
            scope_key("compressor", unit, "durable"),
            {"unit": unit, "mark": watermark, "stage": stage},
        )
        self._stream.commit_upto(marker.seq)
        return PersistReceipt(
            unit=unit,
            stage=stage,
            load=int(load),
            rotor_rpm=int(rotor_rpm),
            watermark=watermark,
            tick=marker.tick,
        )

    def advance(self, unit: str, stage: str) -> StageMove:
        """Step the compressor to the next stage in order."""
        return self.sequence().advance(unit, stage)

    def reset_stage(self, unit: str) -> StageMove:
        """Force the compressor back to idle."""
        return self.sequence().reset(unit)

    def set_max_load(self, unit: str, load: int) -> int:
        """Pin the load ceiling, refusing one outside the travel range."""
        if not 0 <= int(load) <= 100:
            raise LimitViolationError(
                f"compressor load ceiling {load} is outside 0..100",
                unit=unit,
                value=int(load),
                low=0,
                high=100,
            )
        self._registry.set(self.scope(unit), "max_load", int(load))
        return int(load)

    def set_margin_floor(self, unit: str, value: float) -> float:
        """Pin the surge margin floor."""
        self._registry.set(self.scope(unit), "margin_floor", float(value))
        return float(value)

    # --------------------------------------------------------------- summary
    def ramp_target(self, unit: str) -> int:
        """Return the load the compressor is currently aiming at."""
        persisted = self.persisted_state(unit)
        if not persisted:
            return 0
        return int(persisted.get("load", 0))

    def state(self, unit: str) -> dict[str, Any]:
        """Return a snapshot of the compressor module for one unit."""
        persisted = self.persisted_state(unit)
        return {
            "unit": unit,
            "stage": self.stage(unit),
            "durable": self.durable(unit),
            "mark": self.durable_mark(unit),
            "maxLoad": self.max_load(unit),
            "rampTarget": self.ramp_target(unit),
            "surgeCount": self.surge_count(unit),
            "persisted": persisted,
        }
