"""Contract (d): windows, batch uniqueness, filters and decision modes."""

from __future__ import annotations

import pytest

from line_control.judgement.queries import RecordQuery, record_unit
from line_control.judgement.thresholds import Band, ThresholdWindow
from line_control.runtime.clock import LogicalClock
from line_control.runtime.errors import (
    DuplicateRecordError,
    UnknownReferenceError,
    ValidationError,
)
from line_control.store.stream import RecordStream

from tests.support import feed_ticket


def channel(tmp_path, window: int = 3):
    """Return an exhaust style channel plus its clock."""
    clock = LogicalClock()
    stream = RecordStream.open(tmp_path / "chan", clock, durable=False)
    return ThresholdWindow("exhaust", Band(0.0, 10.0), window, stream, clock), clock


def test_band_with_inverted_edges_is_refused() -> None:
    with pytest.raises(ValidationError):
        Band(low=10.0, high=1.0)


def test_window_verdict_flags_a_sample_above_the_band(tmp_path) -> None:
    window, _ = channel(tmp_path)
    window.observe("u1", 4.0)
    window.observe("u1", 12.0)

    verdict = window.evaluate("u1")

    assert verdict.within is False
    assert verdict.sample_count == 2
    assert verdict.overshoot == pytest.approx(2.0)


def test_window_verdict_reports_the_largest_overshoot(tmp_path) -> None:
    window, _ = channel(tmp_path)
    window.observe("u1", 11.0)
    window.observe("u1", 15.0)

    assert window.evaluate("u1").overshoot == pytest.approx(5.0)


def test_window_verdict_ignores_samples_that_left_the_window(tmp_path) -> None:
    window, clock = channel(tmp_path, window=2)
    window.observe("u1", 3.0)
    for _ in range(6):
        clock.tick()
    window.observe("u1", 5.0)

    verdict = window.evaluate("u1")

    assert verdict.sample_count == 1
    assert verdict.latest == pytest.approx(5.0)


def test_window_without_samples_reports_in_band(tmp_path) -> None:
    window, _ = channel(tmp_path)

    verdict = window.evaluate("u1")

    assert verdict.within is True
    assert verdict.sample_count == 0


def test_channel_reset_tombstones_the_samples(tmp_path) -> None:
    window, _ = channel(tmp_path)
    window.observe("u1", 4.0)
    window.reset("u1")

    assert window.evaluate("u1").sample_count == 0


def test_second_batch_open_with_the_same_identifier_is_refused_as_duplicate(line) -> None:
    line.batches.open("b-1", "u1")

    with pytest.raises(DuplicateRecordError):
        line.batches.open("b-1", "u2")


def test_batch_decision_cannot_be_written_twice(line) -> None:
    line.batches.open("b-1", "u1")
    line.batches.resolve("b-1", "accepted")

    with pytest.raises(DuplicateRecordError):
        line.batches.resolve("b-1", "rejected")


def test_unknown_batch_is_refused(line) -> None:
    with pytest.raises(UnknownReferenceError):
        line.batches.require("b-missing")


def test_open_batches_are_listed_per_unit(line) -> None:
    line.batches.open("b-1", "u1")
    line.batches.open("b-2", "u2")
    line.batches.resolve("b-2", "accepted")

    assert [batch.batch_id for batch in line.batches.open_batches("u1")] == ["b-1"]
    assert line.batches.count() == 2


def test_query_filters_by_unit_and_kind(line) -> None:
    line.register_unit("u1")
    line.register_unit("u2")

    selected = RecordQuery(kind="unit.register", unit="u1").apply(line.stream.visible())

    assert [record_unit(record) for record in selected] == ["u1"]


def test_query_filters_by_tick_window(line) -> None:
    line.register_unit("u1")
    mark = line.clock.current
    line.register_unit("u2")

    early = RecordQuery(kind="unit.register", end_tick=mark).apply(line.stream.visible())
    late = RecordQuery(kind="unit.register", start_tick=mark + 1).apply(line.stream.visible())

    assert [record_unit(record) for record in early] == ["u1"]
    assert [record_unit(record) for record in late] == ["u2"]


def test_query_hides_tombstones_unless_asked(line, unit) -> None:
    line.unregister_unit(unit)

    hidden = RecordQuery(kind="unit.unregister").apply(line.stream.visible())
    shown = RecordQuery(kind="unit.unregister", include_tombstones=True).apply(
        line.stream.visible()
    )

    assert hidden == []
    assert len(shown) == 1


def test_query_limit_returns_the_newest_records(line) -> None:
    for index in range(4):
        line.audit.record("note", "u1", f"entry {index}")

    selected = RecordQuery(kind="trace", limit=2).apply(line.stream.visible())

    assert [record.payload["detail"] for record in selected] == ["entry 2", "entry 3"]


def test_query_describes_its_active_conditions(line) -> None:
    query = RecordQuery(unit="u1", kind="trace", limit=3, include_tombstones=True)

    described = query.describe()

    assert described["unit"] == "u1"
    assert described["include_tombstones"] is True
    assert "generation" not in described


def test_current_and_historical_decisions_can_differ(line) -> None:
    line.register_unit("u1")
    mark = line.stream.watermark
    assert line.readiness("u1").outcome == "blocked"

    line.start_unit("u1", feed_ticket(line))
    history = line.readiness_history("u1", mark)

    assert line.readiness("u1").outcome == "ready"
    assert history["now"]["outcome"] == "ready"
    assert history["then"]["outcome"] == "blocked"
    assert history["then"]["mode"] == "historical"


def test_decision_rule_order_follows_priority(line) -> None:
    table = line.decisions.table("start_readiness")

    assert [rule.name for rule in table.rules()] == [
        "ignition_latched",
        "feed_latched",
        "seal_missing",
        "not_durable",
        "oil_missing",
    ]
    assert table.name == "start_readiness"


def test_load_decision_changes_as_the_valve_closes(line, unit) -> None:
    line.start_unit(unit, feed_ticket(line, unit))
    line.drive.set_load(unit, 40)
    line.bleed.set_valve(unit, 0)

    assert line.load_decision(unit).outcome == "open_bleed"

    line.bleed.set_valve(unit, 60)
    assert line.load_decision(unit).outcome == "hold"

    line.drive.set_load(unit, 0)
    assert line.load_decision(unit).outcome == "idle"


def test_load_decision_sheds_when_the_ceiling_is_exceeded(line, unit) -> None:
    line.start_unit(unit, feed_ticket(line, unit))
    line.drive.set_load(unit, 40)
    line.compressor.set_max_load(unit, 20)

    assert line.load_decision(unit).outcome == "shed"
