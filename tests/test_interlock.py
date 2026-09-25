"""Contract (c): ordered stages, latches and pre-gates."""

from __future__ import annotations

import pytest

from line_control.runtime.clock import LogicalClock
from line_control.runtime.errors import (
    GateBlockedError,
    LatchEngagedError,
    NotDurableError,
    OrderingError,
    UnknownReferenceError,
)
from line_control.store.stream import RecordStream

from tests.support import feed_ticket, prime_for_feed


def sequence(tmp_path):
    """Return a three stage sequence and the clock behind it."""
    clock = LogicalClock()
    stream = RecordStream.open(tmp_path / "seq", clock, durable=False)
    from line_control.interlock.stages import StageSequence

    return StageSequence("walk", ("idle", "warm", "hot"), stream, clock)


def test_stage_skip_is_refused_as_out_of_order(tmp_path) -> None:
    stages = sequence(tmp_path)

    with pytest.raises(OrderingError) as refusal:
        stages.advance("u1", "hot")

    assert refusal.value.context["expected"] == "warm"
    assert stages.current("u1") == "idle"


def test_stage_step_back_is_refused_without_a_forced_reset(tmp_path) -> None:
    stages = sequence(tmp_path)
    stages.advance("u1", "warm")
    stages.advance("u1", "hot")

    with pytest.raises(OrderingError):
        stages.advance("u1", "warm")


def test_resettable_stage_may_be_entered_from_anywhere(tmp_path) -> None:
    clock = LogicalClock()
    stream = RecordStream.open(tmp_path / "seq", clock, durable=False)
    from line_control.interlock.stages import StageSequence

    stages = StageSequence("walk", ("idle", "warm", "hot", "coast"), stream, clock, resettable=("coast",))
    stages.advance("u1", "warm")
    stages.advance("u1", "hot")
    stages.advance("u1", "coast")

    assert stages.current("u1") == "coast"


def test_unknown_stage_name_is_refused(tmp_path) -> None:
    stages = sequence(tmp_path)

    with pytest.raises(UnknownReferenceError):
        stages.advance("u1", "molten")


def test_stage_history_records_each_move(tmp_path) -> None:
    stages = sequence(tmp_path)
    stages.advance("u1", "warm")
    stages.advance("u1", "hot")

    history = stages.history("u1")

    assert [move.current for move in history] == ["warm", "hot"]
    assert history[0].previous == "idle"


def test_require_reached_refuses_an_earlier_stage(tmp_path) -> None:
    stages = sequence(tmp_path)
    stages.advance("u1", "warm")

    with pytest.raises(OrderingError):
        stages.require_reached("u1", "hot")
    stages.require_reached("u1", "warm")


def test_latch_stays_engaged_until_the_cause_is_gone(line, unit) -> None:
    line.ignition.fail_latch(unit, "light-off failed")

    with pytest.raises(LatchEngagedError):
        line.board.latches.clear(unit, "ignition", released=False)
    assert line.ignition.latched(unit)

    line.board.latches.clear(unit, "ignition", released=True, note="operator reset")
    assert not line.ignition.latched(unit)


def test_engaged_latches_are_listed_for_a_unit(line, unit) -> None:
    line.ignition.fail_latch(unit, "light-off failed")
    line.feed.latch(unit, "trip")

    assert line.board.latches.engaged(unit) == ["feed", "ignition"]


def test_gate_reports_every_unsatisfied_requirement(line, unit) -> None:
    verdict = line.board.gates.evaluate("feed_open", unit)

    assert verdict.open is False
    assert set(verdict.blocked_by) == {"seal_established", "compressor_durable"}


def test_feed_gate_is_blocked_before_the_seal_is_established(line, unit) -> None:
    line.compressor.advance(unit, "start")
    line.compressor.persist(unit, "start", 0, 3000)

    verdict = line.board.gates.evaluate("feed_open", unit)

    assert verdict.blocked_by == ("seal_established",)


def test_feed_gate_is_blocked_when_the_compressor_state_is_not_durable(line, unit) -> None:
    line.seal.establish(unit, 30)

    verdict = line.board.gates.evaluate("feed_open", unit)

    assert verdict.blocked_by == ("compressor_durable",)


def test_feed_gate_opens_once_seal_and_durable_state_agree(line, unit) -> None:
    prime_for_feed(line, unit)

    assert line.board.gates.require("feed_open", unit).open is True


def test_feed_open_is_refused_when_the_gate_is_closed(line, unit) -> None:
    ticket = feed_ticket(line, unit)

    with pytest.raises(GateBlockedError) as refusal:
        line.open_feed(unit, ticket)

    assert "compressor_durable" in refusal.value.context["blocked_by"]


def test_bleed_close_is_refused_before_the_compressor_state_is_durable(line, unit) -> None:
    with pytest.raises(NotDurableError):
        line.bleed.close(unit)

    line.compressor.advance(unit, "start")
    line.compressor.persist(unit, "start", 0, 3000)
    line.bleed.close(unit)

    assert line.bleed.valve_state(unit) == "closed"


def test_undeclared_gate_is_refused_as_unknown(line, unit) -> None:
    with pytest.raises(UnknownReferenceError):
        line.board.require_gate("feed_nowhere", unit)


def test_declared_gates_and_sequences_are_listed(line, unit) -> None:
    assert line.gates() == ["feed_open", "start_ready"]
    assert line.sequences() == ["compressor", "ignition"]
