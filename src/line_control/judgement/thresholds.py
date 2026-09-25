"""Windowed comparison of a measured channel against an operating band.

Samples are stored in the record stream, so a verdict can be recomputed after
a restart and a verdict over an old window stays reproducible.  Only samples
inside the window ending at the current tick take part in a comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from line_control.runtime.clock import LogicalClock
from line_control.runtime.errors import ValidationError
from line_control.runtime.keys import scope_key
from line_control.store.stream import RecordStream


@dataclass(frozen=True)
class Band:
    """An inclusive operating band."""

    low: float
    high: float

    def __post_init__(self) -> None:
        if self.low > self.high:
            raise ValidationError(
                "band low edge is above its high edge", low=self.low, high=self.high
            )

    def contains(self, value: float) -> bool:
        """Report whether a value sits inside the band."""
        return self.low <= value <= self.high

    def overshoot(self, value: float) -> float:
        """Return how far a value falls outside the band, zero when inside."""
        if value > self.high:
            return value - self.high
        if value < self.low:
            return self.low - value
        return 0.0

    def to_dict(self) -> dict[str, float]:
        """Render the band for the wire."""
        return {"low": self.low, "high": self.high}


@dataclass(frozen=True)
class Sample:
    """One observation of a channel."""

    unit: str
    value: float
    tick: int

    def to_dict(self) -> dict[str, Any]:
        """Render the sample for the wire."""
        return {"unit": self.unit, "value": self.value, "tick": self.tick}


@dataclass(frozen=True)
class ThresholdVerdict:
    """The result of comparing a window of samples against a band."""

    name: str
    unit: str
    band: Band
    window: int
    sample_count: int
    average: float
    peak: float
    low: float
    breaches: tuple[Sample, ...]
    latest: float

    @property
    def within(self) -> bool:
        """Report whether every sample in the window stayed inside the band."""
        return not self.breaches

    @property
    def overshoot(self) -> float:
        """Return the largest distance outside the band seen in the window."""
        if not self.breaches:
            return 0.0
        return max(self.band.overshoot(sample.value) for sample in self.breaches)

    def to_dict(self) -> dict[str, Any]:
        """Render the verdict for the wire."""
        return {
            "name": self.name,
            "unit": self.unit,
            "band": self.band.to_dict(),
            "window": self.window,
            "sampleCount": self.sample_count,
            "average": self.average,
            "peak": self.peak,
            "low": self.low,
            "latest": self.latest,
            "within": self.within,
            "overshoot": self.overshoot,
            "breaches": [sample.to_dict() for sample in self.breaches],
        }


class ThresholdWindow:
    """A named channel judged against a band over a sliding window."""

    def __init__(
        self,
        name: str,
        band: Band,
        window: int,
        stream: RecordStream,
        clock: LogicalClock,
    ) -> None:
        if not name:
            raise ValidationError("threshold name is required")
        if window <= 0:
            raise ValidationError("threshold window must be positive", name=name)
        self._name = name
        self._band = band
        self._window = window
        self._stream = stream
        self._clock = clock

    @property
    def name(self) -> str:
        return self._name

    @property
    def band(self) -> Band:
        return self._band

    @property
    def window(self) -> int:
        return self._window

    def set_band(self, band: Band) -> Band:
        """Retune the operating band, keeping the stored samples in place."""
        self._band = band
        return band

    def key(self, unit: str) -> str:
        """Return the stream key holding the samples of one unit."""
        return scope_key("sample", self._name, unit)

    # ----------------------------------------------------------- write paths
    def observe(self, unit: str, value: float, tick: int | None = None) -> Sample:
        """Record one measurement."""
        if not unit:
            raise ValidationError("unit is required", name=self._name)
        moment = self._clock.tick() if tick is None else int(tick)
        record = self._stream.append(
            "sample",
            self.key(unit),
            {"unit": unit, "value": float(value), "channel": self._name},
        )
        self._stream.commit_upto(record.seq)
        return Sample(unit=unit, value=float(value), tick=moment)

    # ------------------------------------------------------------ read paths
    def samples(self, unit: str, include_stale: bool = False) -> list[Sample]:
        """Return the stored samples of a unit, oldest first.

        A reset tombstone drops everything recorded before it, so a channel
        that was cleared starts its window empty.
        """
        out: list[Sample] = []
        for record in self._stream.visible():
            if record.key != self.key(unit):
                continue
            if record.tombstone:
                out.clear()
                continue
            if record.kind != "sample":
                continue
            if not include_stale and not self._in_window(record.tick):
                continue
            out.append(
                Sample(
                    unit=unit,
                    value=float(record.payload.get("value", 0.0)),
                    tick=record.tick,
                )
            )
        return out

    def evaluate(self, unit: str) -> ThresholdVerdict:
        """Compare the samples inside the window against the band."""
        window_samples = self.samples(unit)
        if not window_samples:
            return ThresholdVerdict(
                name=self._name,
                unit=unit,
                band=self._band,
                window=self._window,
                sample_count=0,
                average=0.0,
                peak=0.0,
                low=0.0,
                breaches=(),
                latest=0.0,
            )
        values = [sample.value for sample in window_samples]
        breaches = tuple(
            sample for sample in window_samples if not self._band.contains(sample.value)
        )
        return ThresholdVerdict(
            name=self._name,
            unit=unit,
            band=self._band,
            window=self._window,
            sample_count=len(window_samples),
            average=sum(values) / len(values),
            peak=max(values),
            low=min(values),
            breaches=breaches,
            latest=window_samples[-1].value,
        )

    def reset(self, unit: str) -> int:
        """Tombstone a channel so the next window starts empty."""
        record = self._stream.tombstone(self.key(unit), kind="sample.reset")
        self._stream.commit_upto(record.seq)
        return record.seq

    def _in_window(self, tick: int) -> bool:
        return self._clock.current - tick <= self._window
