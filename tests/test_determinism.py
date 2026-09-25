"""Determinism: no wall clock, no randomness, identical replays."""

from __future__ import annotations

import re
from pathlib import Path

from line_control.line.supervisor import LineSupervisor

from tests.support import feed_ticket, open_line

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
FORBIDDEN_IMPORTS = re.compile(
    r"^\s*(?:import|from)\s+(time|datetime|random|uuid|secrets|socket)\b", re.MULTILINE
)
FORBIDDEN_CALLS = re.compile(r"\b(?:time|datetime|random|uuid)\.\w+")


def run_a_script(root: Path) -> tuple[list[str], dict[str, object]]:
    """Run the same script twice and return its digests and final state."""
    line = open_line(root)
    line.register_unit("u1")
    line.start_unit("u1", feed_ticket(line))
    line.exhaust.sample("u1", 640)
    line.bleed.set_valve("u1", 30)
    line.drive.set_load("u1", 55)
    digests = [record["digest"] for record in line.stream.export()]
    return digests, line.state("u1")


def test_two_runs_of_the_same_script_produce_the_same_record_digests(tmp_path) -> None:
    first, _ = run_a_script(tmp_path / "left")
    second, _ = run_a_script(tmp_path / "right")

    assert first == second
    assert len(first) > 20


def test_two_runs_of_the_same_script_produce_the_same_state(tmp_path) -> None:
    _, left = run_a_script(tmp_path / "left")
    _, right = run_a_script(tmp_path / "right")

    assert left == right


def test_production_source_never_reads_a_wall_clock_or_randomness() -> None:
    offenders: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if FORBIDDEN_IMPORTS.search(text) or FORBIDDEN_CALLS.search(text):
            offenders.append(path.name)

    assert offenders == []


def test_record_ticks_are_strictly_increasing(tmp_path) -> None:
    line = open_line(tmp_path)
    line.register_unit("u1")
    line.start_unit("u1", feed_ticket(line))

    ticks = [record.tick for record in line.stream.visible()]

    assert ticks == sorted(ticks)
    assert len(set(ticks)) == len(ticks)


def test_the_same_script_leaves_the_same_watermark(tmp_path) -> None:
    left = open_line(tmp_path / "left")
    left.register_unit("u1")
    left.start_unit("u1", feed_ticket(left))
    right = open_line(tmp_path / "right")
    right.register_unit("u1")
    right.start_unit("u1", feed_ticket(right))

    assert left.stream.watermark == right.stream.watermark


def test_supervisors_do_not_share_state(tmp_path) -> None:
    left = open_line(tmp_path / "left")
    right = open_line(tmp_path / "right")
    left.register_unit("u1")

    assert right.units() == []
    assert isinstance(right, LineSupervisor)
