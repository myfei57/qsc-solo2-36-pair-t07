"""One process wide view of the line.

The supervisor owns the shared record stream and the four cross module
contracts, wires the domain modules together, and exposes the start and stop
sequences as single calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from line_control.audit.recorder import Recorder
from line_control.bleed.controller import BleedController
from line_control.compressor.controller import CompressorController
from line_control.drive.controller import DriveController
from line_control.exhaust.controller import CombustionController
from line_control.feed.controller import FeedController
from line_control.ignition.controller import IgnitionController
from line_control.interlock.board import InterlockBoard
from line_control.interlock.gates import GateRequirement
from line_control.judgement.batches import BatchRegistry
from line_control.judgement.decisions import (
    CURRENT,
    Decision,
    DecisionRule,
    DecisionService,
    DecisionTable,
)
from line_control.judgement.queries import RecordQuery
from line_control.lube.controller import LubeController
from line_control.registry.baselines import BaselineBook
from line_control.registry.confirmations import ConfirmationBoard
from line_control.registry.confirmations import DEFAULT_WINDOW
from line_control.registry.generations import GenerationLedger
from line_control.registry.parameters import ParameterRegistry
from line_control.runtime.clock import LogicalClock
from line_control.runtime.errors import UnknownReferenceError, ValidationError
from line_control.runtime.keys import scope_key
from line_control.runtime.tickets import TicketGenerator
from line_control.seal.controller import SealController
from line_control.store.meta import MetaStore
from line_control.store.stream import RecordStream

READINESS_TABLE = "start_readiness"
LOAD_TABLE = "load_shed"
REGISTRATION_KIND = "unit.register"


class LineSupervisor:
    """Owns the stream, the contracts and every domain module."""

    def __init__(self, data_dir: Path | str, durable: bool = True) -> None:
        self._dir = Path(data_dir)
        self._durable = durable
        self._clock = LogicalClock()
        self._stream = RecordStream.open(self._dir, self._clock, durable=durable)
        self._meta = MetaStore(self._dir / "meta.json", durable=durable)
        self._recover_clock()

        self._ledger = GenerationLedger(self._meta)
        self._registry = ParameterRegistry(self._stream, self._clock, self._ledger)
        self._board = InterlockBoard(self._stream, self._clock)
        self._tickets = TicketGenerator(self._meta, "cfm")
        self._confirmations = ConfirmationBoard(
            self._stream, self._clock, self._ledger, self._tickets
        )
        self._baselines = BaselineBook(self._stream, self._clock, self._ledger)

        self._seal = SealController(self._stream, self._registry)
        self._drive = DriveController(self._stream, self._registry)
        self._compressor = CompressorController(
            self._stream,
            self._clock,
            self._registry,
            self._board,
            self._confirmations,
            self._baselines,
        )
        self._lube = LubeController(
            self._stream, self._registry, self._drive.speed, self._drive.slow_enough
        )
        self._bleed = BleedController(
            self._stream,
            self._registry,
            durable_probe=self._compressor.durable,
            margin_probe=self._compressor.margin,
        )
        self._feed = FeedController(
            self._stream, self._registry, self._board, self._confirmations
        )
        self._ignition = IgnitionController(
            self._stream,
            self._clock,
            self._registry,
            self._board,
            self._confirmations,
            lube_ready=self._lube.established,
            seal_ready=lambda unit: self._seal.status(unit).ready,
            feed_open=self._feed.open,
            feed_valve_open=self._feed.valve_open,
        )
        self._exhaust = CombustionController(
            self._stream, self._clock, self._registry, self._board, self._feed
        )
        self._audit = Recorder(self._stream, self._clock)
        self._batches = BatchRegistry(self._stream, self._clock)
        self._decisions = DecisionService(self._stream, self._clock)

        self._recover_generations()
        self._declare_gates()
        self._declare_tables()
        self._redeclare_units()

    # -------------------------------------------------------------- accessors
    @property
    def stream(self) -> RecordStream:
        """Return the shared record stream."""
        return self._stream

    @property
    def registry(self) -> ParameterRegistry:
        """Return the parameter registry."""
        return self._registry

    @property
    def board(self) -> InterlockBoard:
        """Return the interlock board."""
        return self._board

    @property
    def confirmations(self) -> ConfirmationBoard:
        """Return the confirmation board."""
        return self._confirmations

    @property
    def baselines(self) -> BaselineBook:
        """Return the baseline book."""
        return self._baselines

    @property
    def seal(self) -> SealController:
        """Return the seal module."""
        return self._seal

    @property
    def feed(self) -> FeedController:
        """Return the feed module."""
        return self._feed

    @property
    def bleed(self) -> BleedController:
        """Return the bleed module."""
        return self._bleed

    @property
    def compressor(self) -> CompressorController:
        """Return the compressor module."""
        return self._compressor

    @property
    def ignition(self) -> IgnitionController:
        """Return the ignition module."""
        return self._ignition

    @property
    def lube(self) -> LubeController:
        """Return the oil module."""
        return self._lube

    @property
    def exhaust(self) -> CombustionController:
        """Return the exhaust module."""
        return self._exhaust

    @property
    def drive(self) -> DriveController:
        """Return the drive module."""
        return self._drive

    @property
    def audit(self) -> Recorder:
        """Return the trace recorder."""
        return self._audit

    @property
    def batches(self) -> BatchRegistry:
        """Return the batch registry."""
        return self._batches

    @property
    def decisions(self) -> DecisionService:
        """Return the decision service."""
        return self._decisions

    @property
    def clock(self) -> LogicalClock:
        """Return the logical clock."""
        return self._clock

    @property
    def data_dir(self) -> Path:
        """Return the directory the stream is stored in."""
        return self._dir

    # -------------------------------------------------------------- recovery
    def _recover_clock(self) -> None:
        highest = 0
        for record in self._stream.visible():
            highest = max(highest, record.tick)
        self._clock.advance_to(highest)

    def _recover_generations(self) -> None:
        for record in self._stream.visible("param.epoch"):
            scope = str(record.payload.get("scope", ""))
            if scope:
                self._ledger.restore(scope, int(record.payload.get("generation", 0)))

    def _declare_unit_modules(self, unit: str) -> None:
        """Declare every parameter and sequence a unit needs."""
        self._seal.declare_unit(unit)
        self._lube.declare_unit(unit)
        self._drive.declare_unit(unit)
        self._compressor.declare_unit(unit)
        self._bleed.declare_unit(unit)
        self._feed.declare_unit(unit)
        self._ignition.declare_unit(unit)
        self._exhaust.declare_unit(unit)

    def _redeclare_units(self) -> None:
        """Rebuild the in-memory declarations for every recovered unit."""
        for unit in self.units():
            self._declare_unit_modules(unit)

    # ----------------------------------------------------------------- wiring
    def _declare_gates(self) -> None:
        self._board.define_gate(
            "feed_open",
            [
                GateRequirement("seal_established", self._seal.established),
                GateRequirement("compressor_durable", self._compressor.durable),
                GateRequirement("feed_not_latched", lambda unit: not self._feed.latched(unit)),
                GateRequirement(
                    "ignition_not_latched", lambda unit: not self._ignition.latched(unit)
                ),
            ],
        )
        self._board.define_gate(
            "start_ready",
            [
                GateRequirement("no_exhaust_alarm", lambda unit: not self._exhaust.alarm(unit)),
                GateRequirement("feed_not_latched", lambda unit: not self._feed.latched(unit)),
                GateRequirement(
                    "ignition_not_latched", lambda unit: not self._ignition.latched(unit)
                ),
            ],
        )

    def _declare_tables(self) -> None:
        table = DecisionTable(READINESS_TABLE, fallback="ready")
        table.add(
            DecisionRule(
                "ignition_latched",
                "blocked",
                lambda context: context.flag(
                    context.key("latch", "ignition", context.unit), "engaged"
                ),
                priority=50,
            )
        )
        table.add(
            DecisionRule(
                "feed_latched",
                "blocked",
                lambda context: context.flag(
                    context.key("latch", "feed", context.unit), "engaged"
                ),
                priority=40,
            )
        )
        table.add(
            DecisionRule(
                "seal_missing",
                "blocked",
                lambda context: context.text(
                    context.key("seal", context.unit, "state"), "state"
                )
                != "established",
                priority=30,
            )
        )
        table.add(
            DecisionRule(
                "not_durable",
                "blocked",
                lambda context: not context.present(
                    context.key("compressor", context.unit, "durable")
                ),
                priority=20,
            )
        )
        table.add(
            DecisionRule(
                "oil_missing",
                "blocked",
                lambda context: context.text(
                    context.key("lube", context.unit, "state"), "state"
                )
                != "established",
                priority=10,
            )
        )
        self._decisions.register(table)

        load_table = DecisionTable(LOAD_TABLE, fallback="hold")
        load_table.add(
            DecisionRule(
                "over_load",
                "shed",
                lambda context: context.number(
                    context.key("drive", context.unit, "load"), "value"
                )
                > context.number(
                    context.key(
                        "param", context.key("compressor", context.unit), "max_load"
                    ),
                    "value",
                    100.0,
                ),
                priority=30,
            )
        )
        load_table.add(
            DecisionRule(
                "surge_risk",
                "open_bleed",
                lambda context: context.number(
                    context.key("drive", context.unit, "load"), "value"
                )
                > 0
                and context.number(
                    context.key(
                        "param", context.key("bleed", context.unit), "position"
                    ),
                    "value",
                    100.0,
                )
                <= 0.0,
                priority=20,
            )
        )
        load_table.add(
            DecisionRule(
                "unloaded",
                "idle",
                lambda context: context.number(
                    context.key("drive", context.unit, "load"), "value"
                )
                <= 0.0,
                priority=10,
            )
        )
        self._decisions.register(load_table)

    @staticmethod
    def readiness_keys(unit: str) -> list[str]:
        """Return the stream keys the readiness table reads."""
        return [
            scope_key("latch", "ignition", unit),
            scope_key("latch", "feed", unit),
            scope_key("seal", unit, "state"),
            scope_key("compressor", unit, "durable"),
            scope_key("lube", unit, "state"),
        ]

    @staticmethod
    def load_keys(unit: str) -> list[str]:
        """Return the stream keys the load table reads."""
        return [
            scope_key("drive", unit, "load"),
            scope_key("param", scope_key("compressor", unit), "max_load"),
            scope_key("param", scope_key("bleed", unit), "position"),
        ]

    def gates(self) -> list[str]:
        """Return every declared pre-gate."""
        return self._board.gates.names()

    def sequences(self) -> list[str]:
        """Return every declared stage sequence."""
        return self._board.sequence_names()

    # ------------------------------------------------------------ unit set-up
    def units(self) -> list[str]:
        """Return the registered units, sorted."""
        found: list[str] = []
        for key in self._stream.visible_view().keys():
            parts = key.split(":")
            if len(parts) == 3 and parts[0] == "unit" and parts[2] == "registered":
                found.append(parts[1])
        return sorted(found)

    def is_registered(self, unit: str) -> bool:
        """Report whether a unit has been registered."""
        return bool(self._stream.visible_view().current(scope_key("unit", unit, "registered")))

    def require_registered(self, unit: str) -> None:
        """Refuse an operation on a unit that was never registered."""
        if not self.is_registered(unit):
            raise UnknownReferenceError(
                f"unit {unit} is not registered", unit=unit, kind="unit"
            )

    def register_unit(self, unit: str) -> dict[str, Any]:
        """Declare every parameter a unit needs and publish its baseline."""
        if not unit:
            raise ValidationError("a unit name is required")
        if self.is_registered(unit):
            return self.state(unit)
        self._declare_unit_modules(unit)
        self._compressor.publish_default_baseline(unit)
        first = self._stream.append(
            REGISTRATION_KIND,
            scope_key("unit", unit, "registered"),
            {"unit": unit, "state": "registered"},
        )
        second = self._stream.append(
            "unit.meta",
            scope_key("unit", unit, "label"),
            {"unit": unit, "value": unit},
        )
        self._stream.commit_upto(max(first.seq, second.seq))
        self._audit.record("unit", unit, "unit registered")
        return self.state(unit)

    def unregister_unit(self, unit: str) -> dict[str, Any]:
        """Tombstone a registration so the unit stops appearing in overviews."""
        self.require_registered(unit)
        record = self._stream.tombstone(
            scope_key("unit", unit, "registered"), kind="unit.unregister"
        )
        self._stream.commit_upto(record.seq)
        self._audit.record("unit", unit, "unit unregistered")
        return {"unit": unit, "registered": False, "seq": record.seq}

    # ------------------------------------------------------------- sequences
    def start_unit(self, unit: str, ticket: str) -> dict[str, Any]:
        """Run the full start sequence for one unit."""
        self.require_registered(unit)
        self._board.require_gate("start_ready", unit)
        self._lube.prelube(unit)
        self._lube.establish(unit, 40)
        self._lube.set_tank_level(unit, 80)
        self._seal.establish(unit, 30)
        self._compressor.advance(unit, "start")
        self._compressor.persist(unit, "start", 0, 3000)
        self._ignition.start(unit, ticket)
        self._ignition.report_flame(unit, True)
        self._ignition.verify_flame(unit)
        self._compressor.advance(unit, "ramp")
        self._compressor.persist(unit, "ramp", 20, 3000)
        self._drive.set_speed(unit, 3000)
        self._drive.sync_grid(unit)
        self._drive.set_load(unit, 40)
        self._audit.record("start", unit, "start sequence completed")
        return self.state(unit)

    def stop_unit(self, unit: str) -> dict[str, Any]:
        """Run the full stop sequence for one unit."""
        self.require_registered(unit)
        self._drive.set_load(unit, 0)
        self._feed.close(unit)
        self._drive.trip_breaker(unit)
        self._drive.coast_down(unit)
        self._drive.set_speed(unit, 200)
        self._compressor.advance(unit, "coast")
        self._compressor.persist(unit, "coast", 0, 200)
        self._lube.stop(unit)
        self._seal.relieve(unit)
        self._drive.set_speed(unit, 0)
        self._drive.end_coast(unit)
        self._ignition.sequence().reset(unit)
        self._audit.record("stop", unit, "stop sequence completed")
        return self.state(unit)

    def trip(self, unit: str, reason: str) -> dict[str, Any]:
        """Trip a running unit, latching ignition and opening the bleed."""
        self.require_registered(unit)
        self._bleed.open(unit)
        self._drive.trip_breaker(unit)
        self._drive.coast_down(unit)
        self._feed.latch(unit, reason)
        self._ignition.fail_latch(unit, reason)
        self._audit.record("trip", unit, reason)
        return self.state(unit)

    # --------------------------------------------------------------- queries
    def state(self, unit: str) -> dict[str, Any]:
        """Return the full state of one unit."""
        return {
            "unit": unit,
            "registered": self.is_registered(unit),
            "phase": self.phase(unit),
            "seal": self._seal.status(unit).to_dict(),
            "feed": self._feed.status(unit),
            "bleed": self._bleed.status(unit),
            "compressor": self._compressor.state(unit),
            "ignition": self._ignition.status(unit),
            "lube": self._lube.status(unit),
            "exhaust": self._exhaust.status(unit),
            "drive": self._drive.status(unit),
        }

    def phase(self, unit: str) -> str:
        """Return the coarse operating phase of a unit."""
        if self._drive.coasting(unit):
            return "coasting"
        if self._feed.valve_open(unit) and self._ignition.cranking(unit):
            return "starting"
        if self._feed.valve_open(unit):
            return "running"
        return "idle"

    def overview(self) -> list[dict[str, Any]]:
        """Return the state of every registered unit."""
        return [self.state(unit) for unit in self.units()]

    def diagnostics(self, unit: str) -> dict[str, Any]:
        """Return the deeper view used by the diagnostics endpoint."""
        self.require_registered(unit)
        baseline_points = [
            point.to_dict() for point in self._compressor.baseline_points(unit)
        ]
        return {
            "unit": unit,
            "phase": self.phase(unit),
            "stage": self._compressor.stage(unit),
            "watermark": self._stream.watermark,
            "pending": self._stream.pending_count,
            "pendingRecords": [record.to_dict() for record in self._stream.pending()],
            "records": self._stream.record_count,
            "chainLength": self._stream.verify_chain(),
            "clock": self._clock.snapshot(),
            "meta": self._meta.snapshot(),
            "scopes": self._ledger.scopes(),
            "exhaustChannels": self._exhaust.channel_names(),
            "declared": [spec.to_dict() for spec in self._registry.declared()],
            "baselineGeneration": self._compressor.baseline_generation(unit),
            "baselineHistory": self._baselines.generations(self._compressor.scope(unit)),
            "baseline": baseline_points,
            "gates": [verdict.to_dict() for verdict in self._board.gate_report(unit)],
            "gateRequirements": {
                gate: self._board.gates.requirements(gate) for gate in self.gates()
            },
            "latches": self._board.latches.engaged(unit),
            "decisions": self._decisions.table_names(),
            "tickets": {
                "issued": self._tickets.issued(),
                "counter": self._tickets.counter_key,
                "prefix": self._tickets.prefix,
            },
            "trace": self._audit.trail(unit, limit=10),
            "stageHistory": [move.to_dict() for move in self._compressor.stage_history(unit)],
            "replay": [
                record.to_dict()
                for record in self._stream.replay(max(0, self._stream.watermark - 5))
            ],
        }

    def gate_report(self, unit: str) -> list[dict[str, Any]]:
        """Return the evaluated pre-gates of one unit."""
        return [verdict.to_dict() for verdict in self._board.gate_report(unit)]

    def readiness(self, unit: str, mode: str = CURRENT) -> Decision:
        """Return the readiness decision for one unit in either mode."""
        context = self._decisions.context(unit, mode=mode, keys=self.readiness_keys(unit))
        return self._decisions.table(READINESS_TABLE).decide(context)

    def load_decision(self, unit: str, mode: str = CURRENT) -> Decision:
        """Return the load decision for one unit in either mode."""
        context = self._decisions.context(unit, mode=mode, keys=self.load_keys(unit))
        return self._decisions.table(LOAD_TABLE).decide(context)

    def readiness_history(self, unit: str, watermark: int | None = None) -> dict[str, Any]:
        """Return the readiness decision now and at an earlier watermark."""
        target = self._stream.watermark if watermark is None else int(watermark)
        now, then = self._decisions.compare(
            READINESS_TABLE, unit, target, keys=self.readiness_keys(unit)
        )
        return {"now": now.to_dict(), "then": then.to_dict(), "watermark": target}

    def record_query(self, query: RecordQuery) -> list[dict[str, Any]]:
        """Return the visible records matching a filter."""
        return [record.to_dict() for record in query.apply(self._stream.visible())]

    def record_kinds(self) -> list[str]:
        """Return the kinds present in the visible stream."""
        return self._stream.kinds()

    def record_slice(self, start_tick: int, end_tick: int) -> list[dict[str, Any]]:
        """Return the visible records inside a tick interval."""
        return [record.to_dict() for record in self._stream.slice(start_tick, end_tick)]

    def record_counts(self) -> dict[str, int]:
        """Return the visible record counts per kind."""
        return self._stream.group_by_kind()

    def trace_for(self, key: str) -> list[dict[str, Any]]:
        """Return the committed write history of one key."""
        return [record.to_dict() for record in self._stream.trace(key)]

    def record_export(self) -> list[dict[str, Any]]:
        """Return the committed stream as plain dictionaries."""
        return self._stream.export()

    def issue_confirmation(
        self, scope: str, subject: str, window: int = DEFAULT_WINDOW
    ) -> dict[str, Any]:
        """Issue an operator confirmation for one scope."""
        return self._confirmations.issue(scope, subject, window=window).to_dict()

    def open_feed(self, unit: str, ticket: str) -> dict[str, Any]:
        """Open the feed valve through the gated path."""
        self.require_registered(unit)
        return self._feed.open(unit, ticket)

    def release_latch(self, unit: str, name: str, note: str = "") -> dict[str, Any]:
        """Release a latch once its cause is gone."""
        self.require_registered(unit)
        state = self._board.clear_latch(unit, name, True, note=note or f"{name} released")
        return state.to_dict()
