"""Route table and request helpers for the console."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from line_control.judgement.queries import RecordQuery
from line_control.line.supervisor import LineSupervisor
from line_control.registry.confirmations import DEFAULT_WINDOW
from line_control.runtime.errors import ValidationError

Query = Mapping[str, list[str]]
Body = Mapping[str, Any]
Response = tuple[int, Any]
OK = 200
CREATED = 201


def _text(source: Mapping[str, Any], key: str, default: str | None = None) -> str:
    value = source.get(key, default)
    if value is None:
        raise ValidationError(f"{key} is required", field=key)
    if not isinstance(value, str):
        raise ValidationError(f"{key} must be text", field=key)
    return value


def _number(source: Mapping[str, Any], key: str, default: int | None = None) -> int:
    value = source.get(key, default)
    if value is None:
        raise ValidationError(f"{key} is required", field=key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{key} must be a number", field=key)
    return int(value)


def _flag(source: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = source.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _first(query: Query, key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    value = values[0]
    return value if value != "" else None


class Handlers:
    """Every route the console exposes, bound to one supervisor."""

    def __init__(self, supervisor: LineSupervisor) -> None:
        self._supervisor = supervisor

    @property
    def supervisor(self) -> LineSupervisor:
        """Return the supervisor behind these handlers."""
        return self._supervisor

    # ------------------------------------------------------------------ index
    def index(self, query: Query, body: Body) -> Response:
        """Return a directory of the service surface."""
        return OK, {
            "service": "line-control",
            "units": self._supervisor.units(),
            "gates": self._supervisor.gates(),
            "sequences": self._supervisor.sequences(),
            "decisions": self._supervisor.decisions.table_names(),
        }

    def health(self, query: Query, body: Body) -> Response:
        """Return the liveness report."""
        stream = self._supervisor.stream
        return OK, {
            "status": "ok",
            "units": len(self._supervisor.units()),
            "watermark": stream.watermark,
            "pending": stream.pending_count,
            "records": stream.record_count,
            "trail": self._supervisor.audit.total(),
        }

    def meta(self, query: Query, body: Body) -> Response:
        """Return the module inventory of the running service."""
        return OK, {
            "units": self._supervisor.units(),
            "modules": [
                "store",
                "registry",
                "interlock",
                "judgement",
                "seal",
                "feed",
                "bleed",
                "compressor",
                "ignition",
                "lube",
                "exhaust",
                "drive",
                "audit",
            ],
            "gates": self._supervisor.gates(),
            "gateRequirements": {
                gate: self._supervisor.board.gates.requirements(gate)
                for gate in self._supervisor.gates()
            },
            "sequences": self._supervisor.sequences(),
            "decisions": self._supervisor.decisions.table_names(),
            "decisionRules": {
                name: [rule.name for rule in self._supervisor.decisions.table(name).rules()]
                for name in self._supervisor.decisions.table_names()
            },
            "scopes": sorted({spec.scope for spec in self._supervisor.registry.declared()}),
        }

    # ------------------------------------------------------------------ state
    def state(self, query: Query, body: Body) -> Response:
        """Return the overview of every registered unit."""
        unit = _first(query, "unit")
        if unit is not None:
            return OK, self._supervisor.state(unit)
        return OK, self._supervisor.overview()

    def summary(self, query: Query, body: Body) -> Response:
        """Return the one line summary per unit."""
        rows = []
        for unit in self._supervisor.units():
            load = self._supervisor.drive.load(unit)
            rows.append(
                {
                    "unit": unit,
                    "phase": self._supervisor.phase(unit),
                    "load": load,
                    "speed": self._supervisor.drive.speed(unit),
                    "margin": self._supervisor.bleed.guard_margin(unit, load),
                    "surge": self._supervisor.compressor.surge_demand(
                        unit, load, self._supervisor.bleed.position(unit)
                    ),
                    "stage": self._supervisor.compressor.stage(unit),
                    "tripRelay": self._supervisor.exhaust.trip_relay(unit),
                }
            )
        return OK, {"units": rows, "trail": self._supervisor.audit.summary()}

    def snapshot(self, query: Query, body: Body) -> Response:
        """Return the visible key view."""
        view = self._supervisor.stream.visible_view()
        return OK, {"keys": view.keys(), "values": view.as_mapping()}

    def diagnostics(self, query: Query, body: Body) -> Response:
        """Return the diagnostics of one unit."""
        return OK, self._supervisor.diagnostics(_text({"unit": _first(query, "unit")}, "unit"))

    def gates(self, query: Query, body: Body) -> Response:
        """Return the evaluated pre-gates of one unit."""
        unit = _text({"unit": _first(query, "unit")}, "unit")
        return OK, self._supervisor.gate_report(unit)

    def readiness(self, query: Query, body: Body) -> Response:
        """Return the readiness decision of one unit."""
        unit = _text({"unit": _first(query, "unit")}, "unit")
        mode = _first(query, "mode") or "current"
        watermark = _first(query, "watermark")
        if mode == "historical":
            target = None if watermark is None else int(watermark)
            return OK, self._supervisor.readiness_history(unit, target)
        return OK, self._supervisor.readiness(unit, mode=mode).to_dict()

    def load_decision(self, query: Query, body: Body) -> Response:
        """Return the load decision of one unit."""
        unit = _text({"unit": _first(query, "unit")}, "unit")
        mode = _first(query, "mode") or "current"
        return OK, self._supervisor.load_decision(unit, mode=mode).to_dict()

    # ---------------------------------------------------------------- records
    def records(self, query: Query, body: Body) -> Response:
        """Return the visible records matching the query parameters."""
        return OK, self._supervisor.record_query(RecordQuery.from_parameters(query))

    def record_kinds(self, query: Query, body: Body) -> Response:
        """Return the record kinds present in the stream."""
        return OK, self._supervisor.record_kinds()

    def record_counts(self, query: Query, body: Body) -> Response:
        """Return the visible record counts per kind."""
        return OK, self._supervisor.record_counts()

    def record_slice(self, query: Query, body: Body) -> Response:
        """Return the visible records inside a tick interval."""
        start = int(_first(query, "startTick") or 0)
        end = int(_first(query, "endTick") or self._supervisor.clock.current)
        return OK, self._supervisor.record_slice(start, end)

    def record_trace(self, query: Query, body: Body) -> Response:
        """Return the write history of one key."""
        key = _text({"key": _first(query, "key")}, "key")
        return OK, self._supervisor.trace_for(key)

    def record_export(self, query: Query, body: Body) -> Response:
        """Return the committed stream."""
        return OK, self._supervisor.record_export()

    def trail(self, query: Query, body: Body) -> Response:
        """Return the trace records matching the query parameters."""
        recorder = self._supervisor.audit
        unit = _first(query, "unit")
        kind = _first(query, "kind")
        limit = int(_first(query, "limit") or 0)
        if unit is not None and kind is not None:
            return OK, recorder.by_unit_and_kind(unit, kind)
        if unit is not None and limit:
            return OK, recorder.trail(unit, limit=limit)
        if unit is not None:
            return OK, recorder.by_unit(unit)
        if kind is not None:
            return OK, recorder.by_kind(kind)
        return OK, recorder.query()

    def trail_latest(self, query: Query, body: Body) -> Response:
        """Return the newest trace record of one kind."""
        kind = _text({"kind": _first(query, "kind")}, "kind")
        record = self._supervisor.audit.latest(kind)
        if record is None:
            return OK, {"found": False}
        return OK, {"found": True, "record": record}

    def trail_summary(self, query: Query, body: Body) -> Response:
        """Return the compact view of the trail."""
        return OK, self._supervisor.audit.summary()

    def trail_window(self, query: Query, body: Body) -> Response:
        """Return the trace records inside a tick interval."""
        start = int(_first(query, "startTick") or 0)
        end = int(_first(query, "endTick") or self._supervisor.clock.current)
        return OK, self._supervisor.audit.window(start, end)

    # ------------------------------------------------------------ parameters
    def parameters(self, query: Query, body: Body) -> Response:
        """Return the declared parameters, optionally for one scope."""
        scope = _first(query, "scope")
        if scope is None:
            return OK, [spec.to_dict() for spec in self._supervisor.registry.declared()]
        return OK, {
            "scope": scope,
            "generation": self._supervisor.registry.generation(scope),
            "declared": [
                spec.to_dict() for spec in self._supervisor.registry.declared(scope)
            ],
            "values": self._supervisor.registry.values(scope),
        }

    def parameter_set(self, query: Query, body: Body) -> Response:
        """Write one parameter."""
        scope = _text(body, "scope")
        name = _text(body, "name")
        value = body.get("value")
        return OK, self._supervisor.registry.set(scope, name, value).to_dict()

    def parameter_set_many(self, query: Query, body: Body) -> Response:
        """Write several parameters of one scope in a single commit."""
        scope = _text(body, "scope")
        values = body.get("values")
        if not isinstance(values, Mapping):
            raise ValidationError("values must be an object", field="values")
        seq = self._supervisor.registry.set_many(scope, values)
        return OK, {
            "scope": scope,
            "seq": seq,
            "values": self._supervisor.registry.values(scope),
        }

    def parameter_snapshot(self, query: Query, body: Body) -> Response:
        """Capture every value in one scope."""
        return OK, self._supervisor.registry.snapshot(_text(body, "scope")).to_dict()

    def parameter_restore(self, query: Query, body: Body) -> Response:
        """Reapply a snapshot, refusing a stale one."""
        from line_control.registry.parameters import ParameterSnapshot

        scope = _text(body, "scope")
        values = body.get("values")
        if not isinstance(values, Mapping):
            raise ValidationError("values must be an object", field="values")
        snapshot = ParameterSnapshot(
            scope=scope,
            generation=_number(body, "generation"),
            tick=_number(body, "tick", 0),
            values=dict(values),
        )
        return OK, {"seq": self._supervisor.registry.restore(snapshot)}

    def parameter_bump(self, query: Query, body: Body) -> Response:
        """Advance the generation of one scope."""
        return OK, {"generation": self._supervisor.registry.bump(_text(body, "scope"))}

    # --------------------------------------------------------- confirmations
    def confirmations(self, query: Query, body: Body) -> Response:
        """Return the outstanding confirmations."""
        scope = _first(query, "scope")
        board = self._supervisor.confirmations
        return OK, [item.to_dict() for item in board.outstanding(scope)]

    def confirmation_issue(self, query: Query, body: Body) -> Response:
        """Issue a confirmation."""
        return CREATED, self._supervisor.issue_confirmation(
            _text(body, "scope"),
            _text(body, "subject"),
            window=_number(body, "window", DEFAULT_WINDOW),
        )

    def confirmation_consume(self, query: Query, body: Body) -> Response:
        """Redeem a confirmation exactly once."""
        ticket = _text(body, "ticket")
        subject = body.get("subject")
        consumed = self._supervisor.confirmations.consume(
            ticket, None if subject is None else str(subject)
        )
        return OK, consumed.to_dict()

    def confirmation_reissue(self, query: Query, body: Body) -> Response:
        """Replace a confirmation with a fresh ticket."""
        ticket = _text(body, "ticket")
        window = _number(body, "window", DEFAULT_WINDOW)
        return CREATED, self._supervisor.confirmations.reissue(ticket, window=window).to_dict()

    # -------------------------------------------------------------- baselines
    def baselines(self, query: Query, body: Body) -> Response:
        """Return one baseline together with its generation."""
        unit = _text({"unit": _first(query, "unit")}, "unit")
        baseline = self._supervisor.compressor.baseline(unit)
        return OK, {
            "baseline": baseline.to_dict(),
            "currentGeneration": self._supervisor.registry.generation(
                self._supervisor.compressor.scope(unit)
            ),
        }

    # --------------------------------------------------------------- batches
    def batches(self, query: Query, body: Body) -> Response:
        """Return the open batches, optionally for one unit."""
        unit = _first(query, "unit")
        rows = self._supervisor.batches.open_batches(unit)
        return OK, {
            "open": [batch.to_dict() for batch in rows],
            "total": self._supervisor.batches.count(),
        }

    def batch_open(self, query: Query, body: Body) -> Response:
        """Claim a batch identifier."""
        batch = self._supervisor.batches.open(
            _text(body, "batchId"),
            _text(body, "unit"),
            _text(body, "subject", ""),
            generation=_number(body, "generation", 0),
        )
        return CREATED, batch.to_dict()

    def batch_resolve(self, query: Query, body: Body) -> Response:
        """Close a batch with its decision."""
        batch = self._supervisor.batches.resolve(
            _text(body, "batchId"), _text(body, "decision")
        )
        return OK, batch.to_dict()

    def batch_abandon(self, query: Query, body: Body) -> Response:
        """Close a batch without a verdict."""
        note = _text(body, "note", "operator")
        batch = self._supervisor.batches.resolve(
            _text(body, "batchId"), f"abandoned:{note}"
        )
        return OK, batch.to_dict()

    def batch_read(self, query: Query, body: Body) -> Response:
        """Return one batch by identifier."""
        batch_id = _text({"batchId": _first(query, "batchId")}, "batchId")
        return OK, self._supervisor.batches.require(batch_id).to_dict()

    # ------------------------------------------------------------------ units
    def unit_register(self, query: Query, body: Body) -> Response:
        """Register a unit and declare its parameters."""
        return CREATED, self._supervisor.register_unit(_text(body, "unit"))

    def unit_unregister(self, query: Query, body: Body) -> Response:
        """Remove a unit from the overview."""
        return OK, self._supervisor.unregister_unit(_text(body, "unit"))

    def unit_start(self, query: Query, body: Body) -> Response:
        """Run the start sequence."""
        return OK, self._supervisor.start_unit(_text(body, "unit"), _text(body, "ticket"))

    def unit_stop(self, query: Query, body: Body) -> Response:
        """Run the stop sequence."""
        return OK, self._supervisor.stop_unit(_text(body, "unit"))

    def unit_trip(self, query: Query, body: Body) -> Response:
        """Trip a unit."""
        return OK, self._supervisor.trip(_text(body, "unit"), _text(body, "reason"))

    def unit_latch_release(self, query: Query, body: Body) -> Response:
        """Release a named latch on a unit."""
        return OK, self._supervisor.release_latch(
            _text(body, "unit"), _text(body, "name"), _text(body, "note", "")
        )

    # ------------------------------------------------------------------- seal
    def seal_establish(self, query: Query, body: Body) -> Response:
        """Establish the seal at a pressure."""
        unit = _text(body, "unit")
        return OK, self._supervisor.seal.establish(unit, _number(body, "pressure")).to_dict()

    def seal_relieve(self, query: Query, body: Body) -> Response:
        """Relieve the seal."""
        unit = _text(body, "unit")
        return OK, self._supervisor.seal.relieve(unit).to_dict()

    def seal_trim(self, query: Query, body: Body) -> Response:
        """Trim the recorded seal pressure."""
        unit = _text(body, "unit")
        return OK, self._supervisor.seal.trim(unit, _number(body, "pressure")).to_dict()

    # ------------------------------------------------------------------- feed
    def feed_open(self, query: Query, body: Body) -> Response:
        """Open the feed valve through the gated path."""
        return OK, self._supervisor.open_feed(_text(body, "unit"), _text(body, "ticket"))

    def feed_close(self, query: Query, body: Body) -> Response:
        """Close the feed valve."""
        return OK, self._supervisor.feed.close(_text(body, "unit"))

    def feed_position(self, query: Query, body: Body) -> Response:
        """Command a feed valve position."""
        unit = _text(body, "unit")
        return OK, self._supervisor.feed.set_position(unit, _number(body, "position"))

    def feed_setpoint(self, query: Query, body: Body) -> Response:
        """Apply a feed setpoint."""
        unit = _text(body, "unit")
        return OK, self._supervisor.feed.set_setpoint(
            unit, _number(body, "value"), _text(body, "source", "governor")
        ).to_dict()

    def feed_arbitrate(self, query: Query, body: Body) -> Response:
        """Return the arbitrated demand without applying it."""
        return OK, self._supervisor.feed.arbitrate(
            _text(body, "unit"), _number(body, "governor"), _number(body, "protection")
        ).to_dict()

    def feed_limits(self, query: Query, body: Body) -> Response:
        """Move the feed clamps."""
        unit = _text(body, "unit")
        feed = self._supervisor.feed
        lower = body.get("lowLimit")
        upper = body.get("highLimit")
        if lower is not None:
            feed.lower_limit(unit, _number(body, "lowLimit"))
        if upper is not None:
            feed.set_high_limit(unit, _number(body, "highLimit"))
        return OK, feed.status(unit)

    def feed_latch(self, query: Query, body: Body) -> Response:
        """Engage the feed latch."""
        return OK, self._supervisor.feed.latch(
            _text(body, "unit"), _text(body, "reason")
        )

    def feed_unlatch(self, query: Query, body: Body) -> Response:
        """Release the feed latch."""
        return OK, self._supervisor.feed.unlatch(_text(body, "unit"))

    def feed_retry(self, query: Query, body: Body) -> Response:
        """Retry a feed open after a failed attempt."""
        unit = _text(body, "unit")
        ticket = _text(body, "ticket")
        self._supervisor.require_registered(unit)
        attempts = self._supervisor.feed.note_retry(unit)
        result = self._supervisor.feed.retry_open(
            unit,
            ticket,
            self._supervisor.seal.status(unit).ready,
            self._supervisor.ignition.latch_reason(unit),
        )
        result["retry"] = attempts
        return OK, result

    def feed_retry_count(self, query: Query, body: Body) -> Response:
        """Return how many feed retries a unit recorded."""
        unit = _text({"unit": _first(query, "unit")}, "unit")
        return OK, {"unit": unit, "retry": self._supervisor.feed.retry_count(unit)}

    # ------------------------------------------------------------------ bleed
    def bleed_valve(self, query: Query, body: Body) -> Response:
        """Command a bleed valve position."""
        return OK, self._supervisor.bleed.set_valve(
            _text(body, "unit"), _number(body, "position")
        )

    def bleed_open(self, query: Query, body: Body) -> Response:
        """Drive the bleed valve fully open."""
        return OK, self._supervisor.bleed.open(_text(body, "unit"))

    def bleed_close(self, query: Query, body: Body) -> Response:
        """Close the bleed valve once the compressor state is durable."""
        return OK, self._supervisor.bleed.close(_text(body, "unit"))

    def bleed_follow(self, query: Query, body: Body) -> Response:
        """Follow an automatic surge demand."""
        return OK, self._supervisor.bleed.follow_demand(
            _text(body, "unit"), _number(body, "demand")
        )

    def bleed_margin(self, query: Query, body: Body) -> Response:
        """Return the surge margin at a load."""
        unit = _text({"unit": _first(query, "unit")}, "unit")
        load = int(_first(query, "load") or 0)
        return OK, {
            "unit": unit,
            "load": load,
            "margin": self._supervisor.bleed.margin(unit, load),
            "guard": self._supervisor.bleed.guard_margin(unit, load),
        }

    # ------------------------------------------------------------- compressor
    def compressor_persist(self, query: Query, body: Body) -> Response:
        """Persist a compressor state."""
        receipt = self._supervisor.compressor.persist(
            _text(body, "unit"),
            _text(body, "stage"),
            _number(body, "load", 0),
            _number(body, "rotorRpm", 0),
        )
        return OK, receipt.to_dict()

    def compressor_advance(self, query: Query, body: Body) -> Response:
        """Advance the compressor stage."""
        move = self._supervisor.compressor.advance(
            _text(body, "unit"), _text(body, "stage")
        )
        return OK, move.to_dict()

    def compressor_reset(self, query: Query, body: Body) -> Response:
        """Force the compressor back to idle."""
        move = self._supervisor.compressor.reset_stage(_text(body, "unit"))
        return OK, move.to_dict()

    def compressor_recalibrate(self, query: Query, body: Body) -> Response:
        """Recalibrate the efficiency baseline."""
        points = body.get("points")
        if not isinstance(points, Sequence) or isinstance(points, (str, bytes)):
            raise ValidationError("points must be a list", field="points")
        baseline = self._supervisor.compressor.recalibrate(
            _text(body, "unit"), _text(body, "ticket"), points
        )
        return OK, baseline.to_dict()

    def compressor_retire(self, query: Query, body: Body) -> Response:
        """Move the calibration scope on so the baseline becomes stale."""
        unit = _text(body, "unit")
        generation = self._supervisor.compressor.retire_calibration(unit)
        return OK, {"unit": unit, "generation": generation}

    def compressor_maxload(self, query: Query, body: Body) -> Response:
        """Pin the compressor load ceiling."""
        return OK, {
            "unit": _text(body, "unit"),
            "maxLoad": self._supervisor.compressor.set_max_load(
                _text(body, "unit"), _number(body, "load")
            ),
        }

    def compressor_margin(self, query: Query, body: Body) -> Response:
        """Pin the surge margin floor."""
        return OK, {
            "unit": _text(body, "unit"),
            "marginFloor": self._supervisor.compressor.set_margin_floor(
                _text(body, "unit"), float(_number(body, "value"))
            ),
        }

    def compressor_surge(self, query: Query, body: Body) -> Response:
        """Record one surge event."""
        unit = _text(body, "unit")
        return OK, {"unit": unit, "surgeCount": self._supervisor.compressor.record_surge(unit)}

    def compressor_state(self, query: Query, body: Body) -> Response:
        """Return the compressor state of one unit."""
        unit = _text({"unit": _first(query, "unit")}, "unit")
        return OK, self._supervisor.compressor.state(unit)

    # --------------------------------------------------------------- ignition
    def ignition_crank(self, query: Query, body: Body) -> Response:
        """Start cranking."""
        return OK, self._supervisor.ignition.crank(_text(body, "unit"))

    def ignition_spark(self, query: Query, body: Body) -> Response:
        """Prove the igniter."""
        return OK, self._supervisor.ignition.spark_test(_text(body, "unit"))

    def ignition_fire(self, query: Query, body: Body) -> Response:
        """Fire the igniter."""
        return OK, self._supervisor.ignition.fire(_text(body, "unit"))

    def ignition_flame(self, query: Query, body: Body) -> Response:
        """Report whether a flame is visible."""
        return OK, self._supervisor.ignition.report_flame(
            _text(body, "unit"), _flag(body, "detected")
        )

    def ignition_verify(self, query: Query, body: Body) -> Response:
        """Confirm light-off or latch the unit."""
        return OK, self._supervisor.ignition.verify_flame(_text(body, "unit"))

    def ignition_start(self, query: Query, body: Body) -> Response:
        """Run the ignition start chain."""
        return OK, self._supervisor.ignition.start(
            _text(body, "unit"), _text(body, "ticket")
        )

    def ignition_fail(self, query: Query, body: Body) -> Response:
        """Engage the ignition latch."""
        return OK, self._supervisor.ignition.fail_latch(
            _text(body, "unit"), _text(body, "reason")
        )

    def ignition_reset(self, query: Query, body: Body) -> Response:
        """Release the ignition latch."""
        return OK, self._supervisor.ignition.reset_latch(
            _text(body, "unit"), _flag(body, "alarmCleared"), _text(body, "note", "")
        )

    def ignition_window(self, query: Query, body: Body) -> Response:
        """Pin the light-off window."""
        unit = _text(body, "unit")
        return OK, {
            "unit": unit,
            "lightOffWindow": self._supervisor.ignition.set_light_off_window(
                unit, _number(body, "seconds")
            ),
        }

    def ignition_attempts(self, query: Query, body: Body) -> Response:
        """Pin how many spark attempts a start may use."""
        unit = _text(body, "unit")
        return OK, {
            "unit": unit,
            "sparkAttempts": self._supervisor.ignition.set_spark_attempts(
                unit, _number(body, "attempts")
            ),
        }

    # ------------------------------------------------------------------- lube
    def lube_prelube(self, query: Query, body: Body) -> Response:
        """Put the oil system into pre-lube."""
        return OK, self._supervisor.lube.prelube(_text(body, "unit"))

    def lube_establish(self, query: Query, body: Body) -> Response:
        """Establish oil pressure."""
        unit = _text(body, "unit")
        return OK, self._supervisor.lube.establish(unit, _number(body, "pressure"))

    def lube_tank(self, query: Query, body: Body) -> Response:
        """Record a reservoir level."""
        unit = _text(body, "unit")
        return OK, self._supervisor.lube.set_tank_level(unit, _number(body, "level"))

    def lube_oiltemp(self, query: Query, body: Body) -> Response:
        """Record an oil temperature."""
        unit = _text(body, "unit")
        return OK, self._supervisor.lube.set_oil_temp(unit, _number(body, "temperature"))

    def lube_stop(self, query: Query, body: Body) -> Response:
        """Stop the oil supply."""
        return OK, self._supervisor.lube.stop(_text(body, "unit"))

    # ---------------------------------------------------------------- exhaust
    def exhaust_sample(self, query: Query, body: Body) -> Response:
        """Record an exhaust temperature sample."""
        unit = _text(body, "unit")
        return OK, self._supervisor.exhaust.sample(unit, _number(body, "temperature"))

    def exhaust_protect(self, query: Query, body: Body) -> Response:
        """Apply the over-temperature protection demand."""
        unit = _text(body, "unit")
        demand = self._supervisor.exhaust.protect(unit, _number(body, "temperature"))
        return OK, demand.to_dict()

    def exhaust_average(self, query: Query, body: Body) -> Response:
        """Record the averaged exhaust temperature."""
        unit = _text(body, "unit")
        return OK, self._supervisor.exhaust.set_average(unit, _number(body, "temperature"))

    def exhaust_sensors(self, query: Query, body: Body) -> Response:
        """Record how many exhaust sensors are reporting."""
        unit = _text(body, "unit")
        return OK, self._supervisor.exhaust.set_sensor_count(unit, _number(body, "count"))

    def exhaust_limit(self, query: Query, body: Body) -> Response:
        """Pin the over-temperature limit."""
        unit = _text(body, "unit")
        return OK, {
            "unit": unit,
            "limit": self._supervisor.exhaust.set_temp_limit(unit, _number(body, "limit")),
        }

    def exhaust_reset(self, query: Query, body: Body) -> Response:
        """Clear the exhaust alarm after a cool reading."""
        unit = _text(body, "unit")
        return OK, self._supervisor.exhaust.reset(unit, _number(body, "observed"))

    def exhaust_reload(self, query: Query, body: Body) -> Response:
        """Release the feed latch when the alarm is clear."""
        return OK, self._supervisor.exhaust.reload(_text(body, "unit"))

    def exhaust_verdict(self, query: Query, body: Body) -> Response:
        """Return the windowed exhaust verdict."""
        unit = _text({"unit": _first(query, "unit")}, "unit")
        return OK, self._supervisor.exhaust.verdict(unit).to_dict()

    def exhaust_reset_channel(self, query: Query, body: Body) -> Response:
        """Drop the samples of one exhaust channel."""
        unit = _text(body, "unit")
        return OK, {"unit": unit, "seq": self._supervisor.exhaust.channel(unit).reset(unit)}

    # ------------------------------------------------------------------ drive
    def drive_speed(self, query: Query, body: Body) -> Response:
        """Record a rotor speed."""
        unit = _text(body, "unit")
        return OK, self._supervisor.drive.set_speed(unit, _number(body, "rotorRpm"))

    def drive_load(self, query: Query, body: Body) -> Response:
        """Record a load."""
        unit = _text(body, "unit")
        return OK, self._supervisor.drive.set_load(unit, _number(body, "load"))

    def drive_coast(self, query: Query, body: Body) -> Response:
        """Start or end a coast down."""
        unit = _text(body, "unit")
        if _flag(body, "end"):
            return OK, self._supervisor.drive.end_coast(unit)
        return OK, self._supervisor.drive.coast_down(unit)

    def drive_sync(self, query: Query, body: Body) -> Response:
        """Close the breaker to the grid."""
        return OK, self._supervisor.drive.sync_grid(_text(body, "unit"))

    def drive_trip(self, query: Query, body: Body) -> Response:
        """Open the breaker."""
        return OK, self._supervisor.drive.trip_breaker(_text(body, "unit"))

    def drive_grid(self, query: Query, body: Body) -> Response:
        """Record a grid label."""
        unit = _text(body, "unit")
        return OK, self._supervisor.drive.set_grid_label(unit, _text(body, "label"))

    def drive_voltage(self, query: Query, body: Body) -> Response:
        """Record a grid voltage."""
        unit = _text(body, "unit")
        return OK, self._supervisor.drive.set_grid_voltage(unit, _number(body, "volts"))

    def drive_limits(self, query: Query, body: Body) -> Response:
        """Move the drive limits."""
        unit = _text(body, "unit")
        drive = self._supervisor.drive
        if body.get("syncSpeed") is not None:
            drive.set_sync_speed(unit, _number(body, "syncSpeed"))
        if body.get("maxLoad") is not None:
            drive.set_max_load(unit, _number(body, "maxLoad"))
        return OK, drive.status(unit)

    # ------------------------------------------------------------------ trail
    def trail_record(self, query: Query, body: Body) -> Response:
        """Append one operator note to the trail."""
        return CREATED, self._supervisor.audit.record(
            _text(body, "kind", "note"), _text(body, "unit", ""), _text(body, "detail", "")
        )

    def trail_describe(self, query: Query, body: Body) -> Response:
        """Render one trail record as a line of text."""
        index = _number(body, "index", 0)
        records = self._supervisor.audit.query()
        if not records:
            raise ValidationError("the trail is empty", field="index")
        chosen = records[index]
        return OK, {"line": self._supervisor.audit.describe(chosen), "record": chosen}
