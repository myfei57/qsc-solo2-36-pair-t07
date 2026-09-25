"""Efficiency curve helpers for the compressor."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from line_control.registry.baselines import CurvePoint
from line_control.runtime.errors import ValidationError

DEFAULT_POINTS: tuple[tuple[int, float], ...] = ((0, 0.70), (50, 0.72), (100, 0.75))
SURGE_OFFSET = 0.50
SURGE_SLOPE = 0.002


def default_points() -> list[CurvePoint]:
    """Return a fresh copy of the shipped efficiency curve."""
    return [CurvePoint(load=load, value=value) for load, value in DEFAULT_POINTS]


def surge_line(load: int) -> float:
    """Return the surge line value at a load."""
    return SURGE_OFFSET + SURGE_SLOPE * float(load)


def normalise_points(raw: Iterable[Any]) -> list[CurvePoint]:
    """Coerce wire input into ordered curve points, refusing a bad shape."""
    points: list[CurvePoint] = []
    for entry in raw:
        if isinstance(entry, CurvePoint):
            points.append(entry)
            continue
        if isinstance(entry, Mapping):
            if "load" not in entry or "value" not in entry:
                raise ValidationError("a curve point needs a load and a value", point=dict(entry))
            points.append(CurvePoint(load=int(entry["load"]), value=float(entry["value"])))
            continue
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            points.append(CurvePoint(load=int(entry[0]), value=float(entry[1])))
            continue
        raise ValidationError("unsupported curve point", point=entry)
    if len(points) < 2:
        raise ValidationError("a curve needs at least two points")
    points.sort(key=lambda point: point.load)
    return points
