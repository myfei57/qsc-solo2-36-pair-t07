"""Ticket issuance that survives a restart.

Every issued ticket consumes a counter stored outside the process, so two
consecutive boots of the same data directory never hand out the same ticket.
"""

from __future__ import annotations

from typing import Protocol


class CounterStore(Protocol):
    """The slice of the metadata store that ticket issuance depends on."""

    def increment(self, key: str, delta: int = 1) -> int:
        ...

    def read(self, key: str, default: int = 0) -> int:
        ...


class TicketGenerator:
    """Hand out zero padded, prefixed ticket strings."""

    __slots__ = ("_counters", "_prefix", "_width")

    def __init__(self, counters: CounterStore, prefix: str, width: int = 6) -> None:
        if not prefix:
            raise ValueError("ticket prefix must not be empty")
        if width <= 0:
            raise ValueError("ticket width must be positive")
        self._counters = counters
        self._prefix = prefix
        self._width = width

    @property
    def prefix(self) -> str:
        return self._prefix

    @property
    def counter_key(self) -> str:
        return f"ticket.{self._prefix}"

    def issue(self) -> str:
        """Consume the next counter value and format a ticket."""
        return self.format(self._counters.increment(self.counter_key))

    def issued(self) -> int:
        """Return how many tickets have ever been handed out."""
        return self._counters.read(self.counter_key, 0)

    def format(self, value: int) -> str:
        """Render a counter value as a ticket string."""
        return f"{self._prefix}-{int(value):0{self._width}d}"
