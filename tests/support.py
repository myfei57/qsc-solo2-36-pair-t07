"""Helpers shared by the test modules."""

from __future__ import annotations

from pathlib import Path

from line_control.line.supervisor import LineSupervisor

FEED_SCOPE = "feed_gate"
FEED_SUBJECT = "feed.open"
RECALIBRATE_SUBJECT = "compressor.recalibrate"


def open_line(root: Path, durable: bool = False) -> LineSupervisor:
    """Open a supervisor on a scratch directory."""
    return LineSupervisor(root / "var", durable=durable)


def feed_ticket(line: LineSupervisor, unit: str = "u1", window: int | None = None) -> str:
    """Issue a feed open confirmation for a unit."""
    scope = f"{FEED_SCOPE}:{unit}"
    if window is None:
        return line.confirmations.issue(scope, FEED_SUBJECT).ticket
    return line.confirmations.issue(scope, FEED_SUBJECT, window=window).ticket


def recalibrate_ticket(line: LineSupervisor, unit: str = "u1") -> str:
    """Issue a calibration confirmation for a unit."""
    return line.confirmations.issue(
        line.compressor.scope(unit), RECALIBRATE_SUBJECT
    ).ticket


def prime_for_feed(line: LineSupervisor, unit: str = "u1") -> None:
    """Bring a unit to the point where the feed gate can open."""
    line.lube.prelube(unit)
    line.lube.establish(unit, 40)
    line.lube.set_tank_level(unit, 80)
    line.seal.establish(unit, 30)
    line.compressor.advance(unit, "start")
    line.compressor.persist(unit, "start", 0, 3000)
