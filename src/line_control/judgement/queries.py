"""Filtering rules for the record stream."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from line_control.store.records import Record


def record_unit(record: Record) -> str:
    """Return the unit a record belongs to, empty when it names none."""
    named = record.payload.get("unit")
    if isinstance(named, str) and named:
        return named
    parts = record.key.split(":")
    if len(parts) >= 3:
        return parts[2]
    return ""


@dataclass(frozen=True)
class RecordQuery:
    """A declarative filter over records."""

    unit: str | None = None
    kind: str | None = None
    key_prefix: str | None = None
    start_tick: int | None = None
    end_tick: int | None = None
    generation: int | None = None
    min_generation: int | None = None
    include_tombstones: bool = False
    limit: int | None = None

    def matches(self, record: Record) -> bool:
        """Report whether one record passes every active condition."""
        if self.unit is not None and record_unit(record) != self.unit:
            return False
        if self.kind is not None and record.kind != self.kind:
            return False
        if self.key_prefix is not None and not record.key.startswith(self.key_prefix):
            return False
        if self.start_tick is not None and record.tick < self.start_tick:
            return False
        if self.end_tick is not None and record.tick > self.end_tick:
            return False
        if self.generation is not None and record.generation != self.generation:
            return False
        if self.min_generation is not None and record.generation < self.min_generation:
            return False
        if record.tombstone and not self.include_tombstones:
            return False
        return True

    def apply(self, records: Iterable[Record]) -> list[Record]:
        """Return the records that pass the filter, newest last."""
        selected = [record for record in records if self.matches(record)]
        if self.limit is not None:
            if self.limit < 0:
                raise ValueError("query limit must not be negative")
            return selected[-self.limit:] if self.limit else []
        return selected

    def describe(self) -> dict[str, Any]:
        """Render the active conditions for the wire."""
        described: dict[str, Any] = {}
        for field in (
            "unit",
            "kind",
            "key_prefix",
            "start_tick",
            "end_tick",
            "generation",
            "min_generation",
            "limit",
        ):
            value = getattr(self, field)
            if value is not None:
                described[field] = value
        described["include_tombstones"] = self.include_tombstones
        return described

    @classmethod
    def from_parameters(cls, parameters: dict[str, Sequence[str]]) -> "RecordQuery":
        """Build a query from raw string parameters."""

        def first(name: str) -> str | None:
            values = parameters.get(name)
            if not values:
                return None
            value = values[0]
            return value if value != "" else None

        def number(name: str) -> int | None:
            raw = first(name)
            return None if raw is None else int(raw)

        def flag(name: str) -> bool:
            raw = first(name)
            return raw is not None and raw.lower() in {"1", "true", "yes"}

        return cls(
            unit=first("unit"),
            kind=first("kind"),
            key_prefix=first("keyPrefix"),
            start_tick=number("startTick"),
            end_tick=number("endTick"),
            generation=number("generation"),
            min_generation=number("minGeneration"),
            include_tombstones=flag("includeTombstones"),
            limit=number("limit"),
        )
