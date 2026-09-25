"""Batch identity and one decision per batch.

Opening a batch claims its identifier for good: a second open with the same
identifier is refused, so two concurrent callers cannot both believe they own
the same run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from line_control.runtime.clock import LogicalClock
from line_control.runtime.errors import (
    DuplicateRecordError,
    UnknownReferenceError,
    ValidationError,
)
from line_control.runtime.keys import scope_key
from line_control.store.stream import RecordStream


@dataclass(frozen=True)
class Batch:
    """One named run of one unit."""

    batch_id: str
    unit: str
    subject: str
    opened_tick: int
    closed_tick: int | None = None
    decision: str = ""
    generation: int = 0

    @property
    def is_open(self) -> bool:
        """Report whether the batch is still collecting."""
        return self.closed_tick is None

    def to_dict(self) -> dict[str, Any]:
        """Render the batch for the wire."""
        return {
            "batchId": self.batch_id,
            "unit": self.unit,
            "subject": self.subject,
            "openedTick": self.opened_tick,
            "closedTick": self.closed_tick,
            "decision": self.decision,
            "generation": self.generation,
            "open": self.is_open,
        }


class BatchRegistry:
    """Claims batch identifiers and stores one decision per batch."""

    def __init__(self, stream: RecordStream, clock: LogicalClock) -> None:
        self._stream = stream
        self._clock = clock

    def key(self, batch_id: str) -> str:
        """Return the stream key of one batch."""
        return scope_key("batch", batch_id)

    # ----------------------------------------------------------- write paths
    def open(self, batch_id: str, unit: str, subject: str = "", generation: int = 0) -> Batch:
        """Claim a batch identifier, refusing one that is already claimed."""
        if not batch_id or not unit:
            raise ValidationError("batch identifier and unit are required")
        existing = self.read(batch_id)
        if existing is not None:
            raise DuplicateRecordError(
                f"batch {batch_id} was already opened",
                batch_id=batch_id,
                unit=existing.unit,
                opened_tick=existing.opened_tick,
            )
        tick = self._clock.tick()
        record = self._stream.append(
            "batch.open",
            self.key(batch_id),
            {
                "batch_id": batch_id,
                "unit": unit,
                "subject": subject,
                "opened_tick": tick,
                "generation": int(generation),
            },
        )
        self._stream.commit_upto(record.seq)
        return Batch(batch_id, unit, subject, tick, generation=int(generation))

    def resolve(self, batch_id: str, decision: str) -> Batch:
        """Close a batch with its decision, refusing one that is already closed."""
        batch = self.require(batch_id)
        if not batch.is_open:
            raise DuplicateRecordError(
                f"batch {batch_id} already carries a decision",
                batch_id=batch_id,
                decision=batch.decision,
            )
        if not decision:
            raise ValidationError("a decision is required", batch_id=batch_id)
        tick = self._clock.tick()
        record = self._stream.append(
            "batch.close",
            self.key(batch_id),
            {
                "batch_id": batch_id,
                "unit": batch.unit,
                "subject": batch.subject,
                "opened_tick": batch.opened_tick,
                "closed_tick": tick,
                "decision": decision,
                "generation": batch.generation,
            },
        )
        self._stream.commit_upto(record.seq)
        return Batch(
            batch_id,
            batch.unit,
            batch.subject,
            batch.opened_tick,
            closed_tick=tick,
            decision=decision,
            generation=batch.generation,
        )

    # ------------------------------------------------------------ read paths
    def read(self, batch_id: str) -> Batch | None:
        """Return a batch, or ``None`` when the identifier is free."""
        record = self._stream.visible_view().current(self.key(batch_id))
        if record is None:
            return None
        payload = record.payload
        return Batch(
            batch_id=batch_id,
            unit=str(payload.get("unit", "")),
            subject=str(payload.get("subject", "")),
            opened_tick=int(payload.get("opened_tick", 0)),
            closed_tick=payload.get("closed_tick"),
            decision=str(payload.get("decision", "")),
            generation=int(payload.get("generation", 0)),
        )

    def require(self, batch_id: str) -> Batch:
        """Return a batch, refusing an identifier that was never claimed."""
        batch = self.read(batch_id)
        if batch is None:
            raise UnknownReferenceError(
                f"batch {batch_id} was never opened", batch_id=batch_id
            )
        return batch

    def open_batches(self, unit: str | None = None) -> list[Batch]:
        """Return the batches that are still open, optionally for one unit."""
        out: list[Batch] = []
        for record in self._stream.visible("batch.open"):
            batch = self.read(str(record.payload.get("batch_id", "")))
            if batch is None or not batch.is_open:
                continue
            if unit is not None and batch.unit != unit:
                continue
            out.append(batch)
        return out

    def count(self) -> int:
        """Return how many batch identifiers have ever been claimed."""
        return len(self._stream.visible("batch.open"))
