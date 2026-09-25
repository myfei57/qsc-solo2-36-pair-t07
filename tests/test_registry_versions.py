"""Contract (b): generations, confirmations and baselines that go stale."""

from __future__ import annotations

import pytest

from line_control.registry.baselines import CurvePoint
from line_control.runtime.errors import (
    DuplicateRecordError,
    ExpiredCredentialError,
    StaleCredentialError,
    UnknownReferenceError,
    ValidationError,
)

from tests.support import feed_ticket, recalibrate_ticket


def test_parameter_write_is_stamped_with_the_scope_generation(unit, line) -> None:
    written = line.registry.set(f"feed:{unit}", "setpoint", 60)

    assert written.generation == 0
    assert line.registry.value(f"feed:{unit}", "setpoint") == 60


def test_generation_bump_marks_a_previous_snapshot_stale(unit, line) -> None:
    scope = f"feed:{unit}"
    snapshot = line.registry.snapshot(scope)
    line.registry.bump(scope)

    assert snapshot.is_stale(line.registry.generation(scope))
    with pytest.raises(StaleCredentialError):
        line.registry.assert_fresh(snapshot)


def test_restoring_a_stale_snapshot_is_refused(unit, line) -> None:
    scope = f"feed:{unit}"
    line.registry.set(scope, "setpoint", 40)
    snapshot = line.registry.snapshot(scope)
    line.registry.bump(scope)

    with pytest.raises(StaleCredentialError):
        line.registry.restore(snapshot)


def test_restoring_a_fresh_snapshot_is_accepted(unit, line) -> None:
    scope = f"feed:{unit}"
    line.registry.set(scope, "setpoint", 40)
    snapshot = line.registry.snapshot(scope)
    line.registry.set(scope, "setpoint", 10)

    line.registry.restore(snapshot)

    assert line.registry.value(scope, "setpoint") == 40


def test_parameter_outside_its_declared_bounds_is_refused(unit, line) -> None:
    with pytest.raises(ValidationError) as refusal:
        line.registry.set(f"feed:{unit}", "setpoint", 900)

    assert refusal.value.context["high"] == 100


def test_undeclared_parameter_is_refused_as_unknown(line) -> None:
    with pytest.raises(UnknownReferenceError):
        line.registry.set("feed:ghost", "setpoint", 10)


def test_confirmation_past_its_window_is_refused_as_expired(line) -> None:
    line.register_unit("u1")
    ticket = feed_ticket(line, window=2)
    for index in range(5):
        line.audit.record("note", "u1", f"keep the clock moving {index}")

    with pytest.raises(ExpiredCredentialError):
        line.confirmations.assert_usable(ticket)


def test_confirmation_cannot_be_redeemed_twice(line) -> None:
    line.register_unit("u1")
    ticket = feed_ticket(line)
    line.confirmations.consume(ticket)

    with pytest.raises(DuplicateRecordError):
        line.confirmations.consume(ticket)


def test_confirmation_is_refused_after_the_scope_generation_moves(unit, line) -> None:
    ticket = feed_ticket(line)
    line.registry.bump(f"feed_gate:{unit}")

    with pytest.raises(StaleCredentialError):
        line.confirmations.assert_usable(ticket)


def test_confirmation_issued_for_another_subject_is_refused(line) -> None:
    line.register_unit("u1")
    ticket = line.confirmations.issue("feed_gate:u1", "feed.close").ticket

    with pytest.raises(UnknownReferenceError):
        line.confirmations.assert_usable(ticket, subject="feed.open")


def test_unknown_confirmation_ticket_is_refused(line) -> None:
    with pytest.raises(UnknownReferenceError):
        line.confirmations.assert_usable("cfm-999999")


def test_reissued_confirmation_replaces_the_old_ticket(line) -> None:
    line.register_unit("u1")
    first = feed_ticket(line)
    second = line.confirmations.reissue(first).ticket

    assert second != first
    assert line.confirmations.assert_usable(second).subject == "feed.open"
    assert [item.ticket for item in line.confirmations.outstanding("feed_gate:u1")] == [second]


def test_baseline_published_before_a_generation_bump_is_refused_as_stale(unit, line) -> None:
    assert line.compressor.baseline(unit).generation == 0
    line.compressor.retire_calibration(unit)

    with pytest.raises(StaleCredentialError):
        line.compressor.baseline(unit)


def test_recalibration_republishes_a_fresh_baseline(unit, line) -> None:
    ticket = recalibrate_ticket(line)
    baseline = line.compressor.recalibrate(
        unit, ticket, [CurvePoint(0, 0.60), CurvePoint(100, 0.90)]
    )

    assert baseline.generation == 1
    assert line.compressor.baseline(unit).value_at(100) == pytest.approx(0.90)


def test_recalibration_without_a_confirmation_is_refused(unit, line) -> None:
    with pytest.raises(UnknownReferenceError):
        line.compressor.recalibrate(
            unit, "cfm-000404", [CurvePoint(0, 0.5), CurvePoint(100, 0.9)]
        )


def test_single_point_baseline_is_refused(unit, line) -> None:
    with pytest.raises(ValidationError):
        line.baselines.publish("efficiency", line.compressor.scope(unit), [CurvePoint(0, 0.5)])


def test_baseline_generation_history_records_each_publication(unit, line) -> None:
    line.compressor.retire_calibration(unit)
    line.compressor.recalibrate(
        unit,
        recalibrate_ticket(line),
        [CurvePoint(0, 0.60), CurvePoint(100, 0.88)],
    )

    assert line.baselines.generations(line.compressor.scope(unit)) == [0, 2]
