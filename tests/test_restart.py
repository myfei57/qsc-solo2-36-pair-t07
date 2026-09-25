"""Restart recovery: replay by watermark, generations and the clock."""

from __future__ import annotations

import json

import pytest

from line_control.runtime.errors import (
    ExpiredCredentialError,
    StaleCredentialError,
)

from tests.support import feed_ticket, open_line, prime_for_feed


def test_restart_keeps_the_compressor_stage_and_durability(tmp_path) -> None:
    first = open_line(tmp_path)
    first.register_unit("u1")
    first.compressor.advance("u1", "start")
    first.compressor.persist("u1", "start", 0, 3000)

    reopened = open_line(tmp_path)

    assert reopened.compressor.stage("u1") == "start"
    assert reopened.compressor.durable("u1") is True
    assert reopened.compressor.durable_mark("u1") > 0
    assert reopened.units() == ["u1"]


def test_restart_drops_an_uncommitted_write(tmp_path) -> None:
    first = open_line(tmp_path)
    first.register_unit("u1")
    first.stream.append("scratch.note", "scratch:u1", {"unit": "u1", "value": 1})

    reopened = open_line(tmp_path)

    assert reopened.stream.pending_count == 0
    assert reopened.stream.visible_view().current("scratch:u1") is None


def test_restart_keeps_the_generation_so_a_stale_baseline_stays_stale(tmp_path) -> None:
    first = open_line(tmp_path)
    first.register_unit("u1")
    first.compressor.retire_calibration("u1")

    reopened = open_line(tmp_path)
    scope = reopened.compressor.scope("u1")

    assert reopened.registry.generation(scope) == 1
    with pytest.raises(StaleCredentialError):
        reopened.compressor.baseline("u1")


def test_restart_keeps_an_engagement_of_the_ignition_latch(tmp_path) -> None:
    first = open_line(tmp_path)
    first.register_unit("u1")
    first.ignition.fail_latch("u1", "light-off failed")

    reopened = open_line(tmp_path)

    assert reopened.ignition.latched("u1") is True
    assert reopened.ignition.latch_reason("u1") == "light-off failed"
    assert reopened.board.latches.engaged("u1") == ["ignition"]


def test_restart_keeps_the_clock_so_an_old_confirmation_stays_expired(tmp_path) -> None:
    first = open_line(tmp_path)
    first.register_unit("u1")
    ticket = feed_ticket(first, window=2)
    for index in range(5):
        first.audit.record("note", "u1", f"keep the clock moving {index}")
    assert first.confirmations.read(ticket).is_expired(first.clock.current)

    reopened = open_line(tmp_path)

    with pytest.raises(ExpiredCredentialError):
        reopened.confirmations.assert_usable(ticket)


def test_restart_replays_the_trace_for_each_unit(tmp_path) -> None:
    first = open_line(tmp_path)
    first.register_unit("u1")
    first.start_unit("u1", feed_ticket(first))
    expected = [record["payload"]["detail"] for record in first.audit.by_unit("u1")]

    reopened = open_line(tmp_path)

    assert [record["payload"]["detail"] for record in reopened.audit.by_unit("u1")] == expected
    assert reopened.audit.counts() == {"start": 1, "unit": 1}


def test_restart_preserves_the_committed_record_count(tmp_path) -> None:
    first = open_line(tmp_path)
    first.register_unit("u1")
    first.start_unit("u1", feed_ticket(first))
    committed = first.stream.record_count

    reopened = open_line(tmp_path)

    assert reopened.stream.record_count == committed
    assert reopened.stream.watermark == committed
    assert reopened.stream.verify_chain() == committed


def test_restart_after_a_stop_reports_an_idle_unit(tmp_path) -> None:
    first = open_line(tmp_path)
    first.register_unit("u1")
    first.start_unit("u1", feed_ticket(first))
    first.stop_unit("u1")

    reopened = open_line(tmp_path)

    assert reopened.phase("u1") == "idle"
    assert reopened.lube.stopped("u1") is True
    assert reopened.seal.established("u1") is False


def test_restart_keeps_the_feed_limit_lifted_after_a_reset(tmp_path) -> None:
    first = open_line(tmp_path)
    first.register_unit("u1")
    first.exhaust.sample("u1", 700)
    first.exhaust.reset("u1", 500)

    reopened = open_line(tmp_path)

    assert reopened.exhaust.alarm("u1") is False
    assert reopened.feed.low_limit("u1") == 100


def test_restart_after_priming_keeps_the_feed_gate_open(tmp_path) -> None:
    first = open_line(tmp_path)
    first.register_unit("u1")
    prime_for_feed(first, "u1")

    reopened = open_line(tmp_path)

    assert reopened.board.gates.require("feed_open", "u1").open is True


def test_restart_keeps_the_confirmation_counter_moving_forward(tmp_path) -> None:
    first = open_line(tmp_path)
    first.register_unit("u1")
    issued = feed_ticket(first)

    reopened = open_line(tmp_path)
    second = feed_ticket(reopened)

    assert second != issued
    assert reopened.confirmations.read(issued) is not None


def test_corrupt_log_line_blocks_the_restart(tmp_path) -> None:
    from line_control.runtime.errors import StreamIntegrityError

    first = open_line(tmp_path)
    first.register_unit("u1")
    path = first.data_dir / "records.jsonl"
    lines = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    lines[-1]["payload"]["value"] = "tampered"
    path.write_text(
        "\n".join(json.dumps(line, sort_keys=True) for line in lines) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(StreamIntegrityError):
        open_line(tmp_path)
