"""Start, stop and interlock behaviour across the domain modules."""

from __future__ import annotations

import pytest

from line_control.runtime.errors import (
    GateBlockedError,
    LatchEngagedError,
    LightOffFailedError,
    LimitViolationError,
    OrderingError,
    UnknownReferenceError,
)

from tests.support import feed_ticket, prime_for_feed


def test_start_sequence_brings_a_unit_to_running(line, unit) -> None:
    state = line.start_unit(unit, feed_ticket(line, unit))

    assert state["phase"] == "running"
    assert state["feed"]["valve"] == "open"
    assert state["compressor"]["stage"] == "ramp"
    assert state["compressor"]["durable"] is True
    assert state["ignition"]["stage"] == "run"
    assert state["ignition"]["flame"] is True
    assert state["drive"]["speed"] == 3000
    assert state["drive"]["breaker"] == "connected"


def test_stop_sequence_returns_a_unit_to_idle(line, unit) -> None:
    line.start_unit(unit, feed_ticket(line, unit))

    state = line.stop_unit(unit)

    assert state["phase"] == "idle"
    assert state["feed"]["valve"] == "closed"
    assert state["lube"]["stopped"] is True
    assert state["seal"]["established"] is False
    assert state["drive"]["breaker"] == "open"
    assert state["drive"]["speed"] == 0
    assert state["drive"]["coasting"] is False
    assert state["compressor"]["stage"] == "coast"


def test_start_is_refused_while_the_exhaust_alarm_is_latched(line, unit) -> None:
    line.exhaust.sample(unit, 700)

    with pytest.raises(GateBlockedError) as refusal:
        line.start_unit(unit, feed_ticket(line, unit))

    assert "no_exhaust_alarm" in refusal.value.context["blocked_by"]


def test_crank_is_refused_before_oil_pressure_is_established(line, unit) -> None:
    with pytest.raises(OrderingError) as refusal:
        line.ignition.crank(unit)

    assert refusal.value.context["missing"] == "lube_established"


def test_ignition_start_is_refused_while_the_latch_is_engaged(line, unit) -> None:
    line.ignition.fail_latch(unit, "light-off failed")

    with pytest.raises(LatchEngagedError):
        line.ignition.start(unit, feed_ticket(line, unit))


def test_light_off_failure_latches_the_unit_and_raises(line, unit) -> None:
    prime_for_feed(line, unit)
    line.ignition.start(unit, feed_ticket(line, unit))
    line.ignition.report_flame(unit, False)

    with pytest.raises(LightOffFailedError):
        line.ignition.verify_flame(unit)

    assert line.ignition.latched(unit) is True
    assert line.ignition.latch_reason(unit) == "light-off failed"


def test_flame_verification_advances_the_sequence_to_run(line, unit) -> None:
    prime_for_feed(line, unit)
    line.ignition.start(unit, feed_ticket(line, unit))
    line.ignition.report_flame(unit, True)
    line.ignition.verify_flame(unit)

    assert line.ignition.stage(unit) == "run"
    assert line.ignition.cranking(unit) is False


def test_spark_test_is_refused_before_cranking(line, unit) -> None:
    with pytest.raises(OrderingError):
        line.ignition.spark_test(unit)


def test_oil_stop_is_refused_while_the_rotor_is_still_fast(line, unit) -> None:
    line.drive.set_speed(unit, 3000)

    with pytest.raises(OrderingError) as refusal:
        line.lube.stop(unit)

    assert refusal.value.context["threshold"] == 300


def test_oil_stop_is_accepted_once_the_rotor_has_slowed(line, unit) -> None:
    line.drive.set_speed(unit, 200)

    line.lube.stop(unit)

    assert line.lube.stopped(unit) is True


def test_grid_sync_is_refused_below_the_synchronising_window(line, unit) -> None:
    line.drive.set_speed(unit, 1200)

    with pytest.raises(OrderingError) as refusal:
        line.drive.sync_grid(unit)

    assert refusal.value.context["window"] == 2950


def test_feed_retry_needs_an_established_seal(line, unit) -> None:
    with pytest.raises(GateBlockedError):
        line.feed.retry_open(unit, feed_ticket(line, unit), seal_ok=False, ignition_latch="")


def test_feed_retry_clears_the_latch_and_counts_the_attempt(line, unit) -> None:
    prime_for_feed(line, unit)
    line.feed.latch(unit, "manual trip")

    result = line.feed.retry_open(
        unit, feed_ticket(line, unit), seal_ok=True, ignition_latch=""
    )

    assert result["valve"] == "open"
    assert result["retry"] == 1
    assert line.feed.latched(unit) is False


def test_trip_opens_the_bleed_and_latches_both_channels(line, unit) -> None:
    line.start_unit(unit, feed_ticket(line, unit))

    state = line.trip(unit, "overspeed")

    assert state["bleed"]["valve"] == "open"
    assert state["feed"]["latched"] is True
    assert state["ignition"]["latched"] is True
    assert state["drive"]["coasting"] is True


def test_over_temperature_sample_latches_and_drops_the_feed_limit(line, unit) -> None:
    line.exhaust.sample(unit, 700)

    assert line.exhaust.alarm(unit) is True
    assert line.exhaust.alarm_reason(unit) == "exhaust over-temperature"
    assert line.feed.low_limit(unit) == 0
    assert line.exhaust.trip_relay(unit) is True


def test_over_temperature_protection_pulls_the_feed_setpoint_down(line, unit) -> None:
    demand = line.exhaust.protect(unit, 700)

    assert demand.source == "protection"
    assert demand.value == 30
    assert line.feed.setpoint(unit) == 30


def test_over_temperature_reset_is_refused_while_the_reading_is_still_hot(line, unit) -> None:
    line.exhaust.sample(unit, 700)

    with pytest.raises(LatchEngagedError):
        line.exhaust.reset(unit, 700)

    assert line.exhaust.alarm(unit) is True


def test_over_temperature_reset_clears_the_latch_and_restores_the_limit(line, unit) -> None:
    line.exhaust.sample(unit, 700)

    line.exhaust.reset(unit, 500)

    assert line.exhaust.alarm(unit) is False
    assert line.feed.low_limit(unit) == 100
    assert line.exhaust.reloadable(unit) is True


def test_reload_is_refused_while_the_alarm_is_active(line, unit) -> None:
    line.exhaust.sample(unit, 700)

    with pytest.raises(LatchEngagedError):
        line.exhaust.reload(unit)


def test_seal_below_the_minimum_pressure_is_refused(line, unit) -> None:
    with pytest.raises(LimitViolationError) as refusal:
        line.seal.establish(unit, 5)

    assert refusal.value.context["low"] == 20


def test_oil_below_the_minimum_pressure_is_refused(line, unit) -> None:
    with pytest.raises(LimitViolationError) as refusal:
        line.lube.establish(unit, 10)

    assert refusal.value.context["low"] == 30


def test_feed_position_outside_the_travel_range_is_refused(line, unit) -> None:
    with pytest.raises(LimitViolationError):
        line.feed.set_position(unit, 140)

    line.feed.set_position(unit, 40)
    assert line.feed.position(unit) == 40


def test_feed_setpoint_cannot_be_negative(line, unit) -> None:
    with pytest.raises(LimitViolationError):
        line.feed.set_setpoint(unit, -5)


def test_feed_clamps_cannot_cross_each_other(line, unit) -> None:
    line.feed.lower_limit(unit, 70)
    pinned = line.feed.set_high_limit(unit, 20)

    assert pinned == 70
    assert line.feed.high_limit(unit) == 70


def test_arbitration_lets_protection_pull_the_demand_down(line, unit) -> None:
    limited = line.feed.arbitrate(unit, 80, 30)
    unopposed = line.feed.arbitrate(unit, 40, 90)

    assert limited.value == 30
    assert limited.source == "protection"
    assert unopposed.value == 40
    assert unopposed.source == "governor"


def test_protection_setpoint_only_lowers_the_applied_value(line, unit) -> None:
    line.feed.set_setpoint(unit, 60)
    raised = line.feed.set_setpoint(unit, 95, source="protection")
    lowered = line.feed.set_setpoint(unit, 20, source="protection")

    assert raised.value == 60
    assert lowered.value == 20
    assert lowered.source == "protection"


def test_feed_flow_uses_the_setpoint_and_the_valve_position(line, unit) -> None:
    prime_for_feed(line, unit)
    line.feed.open(unit, feed_ticket(line, unit))
    line.feed.set_setpoint(unit, 60)
    line.feed.set_position(unit, 50)

    assert line.feed.flow(unit) == 30

    line.feed.close(unit)
    assert line.feed.flow(unit) == 0


def test_compressor_load_ceiling_outside_the_range_is_refused(line, unit) -> None:
    with pytest.raises(LimitViolationError):
        line.compressor.set_max_load(unit, 300)


def test_drive_load_above_the_ceiling_is_refused(line, unit) -> None:
    line.drive.set_max_load(unit, 50)

    with pytest.raises(LimitViolationError):
        line.drive.set_load(unit, 80)


def test_surge_demand_never_asks_for_less_than_the_current_opening(unit, line) -> None:
    assert line.compressor.surge_demand(unit, 0, 80) == 80
    assert line.compressor.surge_demand(unit, 100, 0) == 60

    line.compressor.set_margin_floor(unit, 0.3)
    assert line.compressor.surge_demand(unit, 0, 0) == 100


def test_surge_events_are_counted(line, unit) -> None:
    line.compressor.record_surge(unit)
    line.compressor.record_surge(unit)

    assert line.compressor.surge_count(unit) == 2


def test_unit_state_reports_every_module(line, unit) -> None:
    state = line.state(unit)

    assert set(state) == {
        "unit",
        "registered",
        "phase",
        "seal",
        "feed",
        "bleed",
        "compressor",
        "ignition",
        "lube",
        "exhaust",
        "drive",
    }


def test_unregistered_unit_is_refused(line) -> None:
    with pytest.raises(UnknownReferenceError):
        line.start_unit("u9", "cfm-000001")

    assert line.is_registered("u9") is False
    assert line.units() == []


def test_unregister_removes_the_unit_from_the_overview(line, unit) -> None:
    assert line.units() == [unit]

    line.unregister_unit(unit)

    assert line.units() == []
    assert line.overview() == []


def test_registering_the_same_unit_twice_keeps_one_registration(line, unit) -> None:
    line.register_unit(unit)

    assert line.units() == [unit]


def test_diagnostics_reports_gates_stage_history_and_replay(line, unit) -> None:
    line.start_unit(unit, feed_ticket(line, unit))

    report = line.diagnostics(unit)

    assert report["stage"] == "ramp"
    assert report["chainLength"] == report["records"]
    assert report["latches"] == []
    assert {entry["gate"] for entry in report["gates"]} == {"feed_open", "start_ready"}
    assert [move["current"] for move in report["stageHistory"]][-1] == "ramp"
    assert report["replay"]
