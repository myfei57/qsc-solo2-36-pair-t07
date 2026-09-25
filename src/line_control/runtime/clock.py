"""Monotonic logical clock used by every time dependent decision.

The service never reads a wall clock.  Windows, expiry checks and audit
ordering all derive from this counter, so replaying the same command sequence
produces the same result and the same numbers.
"""

from __future__ import annotations


class LogicalClock:
    """A counter that only ever moves forward."""

    __slots__ = ("_value",)

    def __init__(self, start: int = 0) -> None:
        if start < 0:
            raise ValueError("clock start must not be negative")
        self._value = int(start)

    @property
    def current(self) -> int:
        """Return the value the counter holds right now."""
        return self._value

    def tick(self, step: int = 1) -> int:
        """Advance the counter and return the new value."""
        if step <= 0:
            raise ValueError("clock step must be positive")
        self._value += int(step)
        return self._value

    def advance_to(self, value: int) -> int:
        """Adopt ``value`` when it is ahead of the counter.

        Restoring a persisted counter uses this so that windows opened before a
        restart keep their original issue marks.
        """
        if value > self._value:
            self._value = int(value)
        return self._value

    def snapshot(self) -> int:
        """Return a plain integer copy, safe to persist."""
        return self._value
