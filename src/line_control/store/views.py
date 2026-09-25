"""Turn a flat list of records into a keyed view or a per key trace."""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from line_control.store.records import Record


class KeyView:
    """The visible value of every key at one point of the stream."""

    __slots__ = ("_entries",)

    def __init__(self, entries: Mapping[str, Record]) -> None:
        self._entries = dict(entries)

    def keys(self) -> list[str]:
        """Return the visible keys in sorted order."""
        return sorted(self._entries)

    def current(self, key: str) -> Record | None:
        """Return the record that decides the value of ``key``."""
        return self._entries.get(key)

    def generation_of(self, key: str) -> int:
        """Return the generation carried by the deciding record."""
        record = self._entries.get(key)
        if record is None:
            return 0
        return record.generation

    def as_mapping(self) -> dict[str, dict[str, object]]:
        """Return a plain mapping of key to payload."""
        return {key: dict(record.payload) for key, record in self._entries.items()}


def materialize(records: Iterable[Record]) -> dict[str, Record]:
    """Compute the live value of every key from an ordered record list.

    A tombstone removes the key from the view; a later write brings it back.
    """
    entries: dict[str, Record] = {}
    for record in records:
        if record.tombstone:
            entries.pop(record.key, None)
        else:
            entries[record.key] = record
    return entries


def build_view(records: Iterable[Record]) -> KeyView:
    """Build a :class:`KeyView` from an ordered record list."""
    return KeyView(materialize(records))


def trace_for(records: Sequence[Record], key: str) -> list[Record]:
    """Return every record that touched ``key``, tombstones included."""
    return [record for record in records if record.key == key]
