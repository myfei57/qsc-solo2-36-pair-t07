"""Contract (a): append only records behind a commit watermark."""

from __future__ import annotations

import json

import pytest

from line_control.runtime.clock import LogicalClock
from line_control.runtime.errors import StreamIntegrityError, ValidationError
from line_control.store.stream import RecordStream


def open_stream(tmp_path, name: str = "stream", durable: bool = False) -> RecordStream:
    """Open a bare record stream on a scratch directory."""
    return RecordStream.open(tmp_path / name, LogicalClock(), durable=durable)


def test_appended_record_is_invisible_until_the_watermark_passes(tmp_path) -> None:
    stream = open_stream(tmp_path)
    stream.append("note", "k1", {"value": 1})

    assert stream.visible() == []
    assert len(stream.pending()) == 1
    assert stream.pending_count == 1

    stream.commit_upto(1)

    assert [record.payload["value"] for record in stream.visible()] == [1]
    assert stream.pending_count == 0


def test_commit_advances_the_watermark_only_forward(tmp_path) -> None:
    stream = open_stream(tmp_path)
    stream.append("note", "k1", {"value": 1})
    stream.append("note", "k2", {"value": 2})
    stream.commit_upto(2)

    assert stream.watermark == 2
    assert stream.commit_upto(1) == 2
    assert stream.commit_upto(99) == 2


def test_replay_returns_only_committed_records_after_the_marker(tmp_path) -> None:
    stream = open_stream(tmp_path)
    for index in range(4):
        stream.append("note", f"k{index}", {"value": index})
    stream.commit_upto(3)
    stream.append("note", "k9", {"value": 9})

    replayed = stream.replay(1)

    assert [record.seq for record in replayed] == [2, 3]


def test_restart_replays_up_to_the_watermark_and_drops_uncommitted_tail(tmp_path) -> None:
    first = open_stream(tmp_path)
    first.append("note", "k1", {"value": 1})
    first.commit_upto(1)
    first.append("note", "k2", {"value": 2})

    reopened = open_stream(tmp_path)

    assert [record.payload["value"] for record in reopened.visible()] == [1]
    assert reopened.pending_count == 0
    assert reopened.record_count == 1


def test_discard_uncommitted_rolls_the_stream_back_to_the_watermark(tmp_path) -> None:
    stream = open_stream(tmp_path)
    stream.append("note", "k1", {"value": 1})
    stream.commit_upto(1)
    stream.append("note", "k2", {"value": 2})
    stream.append("note", "k3", {"value": 3})

    dropped = stream.discard_uncommitted()

    assert dropped == 2
    assert [record.key for record in stream.all_records()] == ["k1"]
    assert stream.record_count == 1


def test_tombstone_removes_a_key_from_the_visible_view(tmp_path) -> None:
    stream = open_stream(tmp_path)
    stream.append("note", "k1", {"value": 1})
    stream.tombstone("k1")
    stream.commit_upto(stream.record_count)

    assert stream.visible_view().current("k1") is None
    assert "k1" not in stream.visible_view().keys()


def test_key_reappears_after_a_write_follows_its_tombstone(tmp_path) -> None:
    stream = open_stream(tmp_path)
    stream.append("note", "k1", {"value": 1})
    stream.tombstone("k1")
    stream.append("note", "k1", {"value": 7})
    stream.commit_upto(stream.record_count)

    record = stream.visible_view().current("k1")

    assert record is not None
    assert record.payload["value"] == 7


def test_materialised_view_keeps_the_newest_record_per_key(tmp_path) -> None:
    stream = open_stream(tmp_path)
    stream.append("note", "k1", {"value": 1})
    stream.append("note", "k1", {"value": 2})
    stream.append("note", "k2", {"value": 3})
    stream.commit_upto(stream.record_count)

    view = stream.visible_view()

    assert view.keys() == ["k1", "k2"]
    assert view.current("k1").payload["value"] == 2
    assert view.as_mapping()["k2"] == {"value": 3}


def test_view_at_an_earlier_watermark_shows_the_historical_value(tmp_path) -> None:
    stream = open_stream(tmp_path)
    stream.append("note", "k1", {"value": 1})
    stream.commit_upto(1)
    mark = stream.watermark
    stream.append("note", "k1", {"value": 2})
    stream.commit_upto(2)

    assert stream.visible_view().current("k1").payload["value"] == 2
    assert stream.view_at(mark).current("k1").payload["value"] == 1


def test_digest_chain_breaks_when_a_record_body_is_altered(tmp_path) -> None:
    stream = open_stream(tmp_path)
    stream.append("note", "k1", {"value": 1})
    stream.append("note", "k2", {"value": 2})
    stream.commit_upto(2)
    path = tmp_path / "stream" / "records.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    lines[0]["payload"]["value"] = 99
    path.write_text(
        "\n".join(json.dumps(line, sort_keys=True) for line in lines) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(StreamIntegrityError):
        open_stream(tmp_path)


def test_record_sequence_numbers_are_contiguous(tmp_path) -> None:
    stream = open_stream(tmp_path)
    for index in range(5):
        stream.append("note", f"k{index}", {"value": index})
    stream.commit_upto(stream.record_count)

    assert stream.verify_chain() == 5
    assert [record.seq for record in stream.visible()] == [1, 2, 3, 4, 5]


def test_record_without_a_kind_is_refused(tmp_path) -> None:
    stream = open_stream(tmp_path)

    with pytest.raises(ValidationError):
        stream.append("", "k1", {"value": 1})


def test_durable_stream_survives_a_process_restart(tmp_path) -> None:
    first = open_stream(tmp_path, name="durable", durable=True)
    first.append("note", "k1", {"value": 5})
    first.commit_upto(1)

    reopened = open_stream(tmp_path, name="durable", durable=True)

    assert reopened.visible_view().current("k1").payload["value"] == 5


def test_trace_returns_every_write_of_one_key_including_tombstones(tmp_path) -> None:
    stream = open_stream(tmp_path)
    stream.append("note", "k1", {"value": 1})
    stream.tombstone("k1")
    stream.append("note", "other", {"value": 9})
    stream.commit_upto(stream.record_count)

    history = stream.trace("k1")

    assert [record.seq for record in history] == [1, 2]
    assert history[-1].tombstone is True


def test_slice_and_group_by_kind_report_the_visible_stream(tmp_path) -> None:
    stream = open_stream(tmp_path)
    stream.append("alpha", "k1", {"value": 1})
    stream.append("beta", "k2", {"value": 2})
    stream.append("beta", "k3", {"value": 3})
    stream.commit_upto(stream.record_count)

    assert stream.kinds() == ["alpha", "beta"]
    assert stream.group_by_kind() == {"alpha": 1, "beta": 2}
    assert len(stream.slice(0, 10_000)) == 3
