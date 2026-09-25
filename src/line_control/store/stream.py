"""Append only record stream with a commit watermark.

Writes land in the log first and stay invisible until the watermark passes
them.  A restart replays only what the watermark covered, so a step that was
never committed never becomes visible just because it reached the disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from line_control.runtime.clock import LogicalClock
from line_control.runtime.errors import ValidationError
from line_control.store.records import GENESIS_DIGEST, Record
from line_control.store.segments import AtomicJsonFile, JsonLineReader, JsonLineWriter
from line_control.store.views import KeyView, build_view, trace_for

LOG_NAME = "records.jsonl"
WATERMARK_NAME = "watermark.json"


class RecordStream:
    """The only place that appends to the durable record log."""

    def __init__(self, directory: Path, clock: LogicalClock, durable: bool = True) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._writer = JsonLineWriter(self._dir / LOG_NAME, durable=durable)
        self._reader = JsonLineReader(self._dir / LOG_NAME)
        self._watermark_file = AtomicJsonFile(self._dir / WATERMARK_NAME, durable=durable)
        self._records: list[Record] = []
        self._watermark = 0

    # ------------------------------------------------------------------ open
    @classmethod
    def open(
        cls,
        directory: Path,
        clock: LogicalClock,
        durable: bool = True,
        recover: bool = True,
    ) -> "RecordStream":
        """Open a stream and replay it up to the last committed watermark."""
        stream = cls(directory, clock, durable=durable)
        stream._load(recover=recover)
        return stream

    def _load(self, recover: bool) -> None:
        stored = self._watermark_file.read({"watermark": 0})
        self._watermark = int(stored.get("watermark", 0))
        replayed: list[Record] = []
        for line in self._reader.read_all():
            record = Record.from_dict(line)
            if len(replayed) and record.previous != replayed[-1].digest:
                # The tail is torn; everything from here on is unusable.
                break
            replayed.append(record)
        self._records = replayed
        if recover:
            self.discard_uncommitted()

    # ---------------------------------------------------------------- append
    def append(
        self,
        kind: str,
        key: str,
        payload: Mapping[str, Any] | None = None,
        generation: int = 0,
        tombstone: bool = False,
    ) -> Record:
        """Append one record and return it. It is not visible until committed."""
        if not kind:
            raise ValidationError("record kind is required")
        if not key:
            raise ValidationError("record key is required")
        previous = self._records[-1].digest if self._records else GENESIS_DIGEST
        record = Record.build(
            seq=len(self._records) + 1,
            kind=kind,
            key=key,
            payload=payload or {},
            generation=int(generation),
            tick=self._clock.tick(),
            tombstone=bool(tombstone),
            previous=previous,
        )
        self._records.append(record)
        self._writer.append(record.to_dict())
        return record

    def tombstone(self, key: str, generation: int = 0, kind: str = "tombstone") -> Record:
        """Append a record that removes ``key`` from the visible view."""
        return self.append(kind, key, {}, generation=generation, tombstone=True)

    # -------------------------------------------------------------- watermark
    @property
    def watermark(self) -> int:
        """Return the highest sequence number visible to readers."""
        return self._watermark

    def commit_upto(self, seq: int) -> int:
        """Move the watermark forward, never backward."""
        if seq < 0:
            raise ValidationError("commit target must not be negative")
        target = min(int(seq), len(self._records))
        if target > self._watermark:
            self._watermark = target
            self._watermark_file.write({"watermark": self._watermark})
        return self._watermark

    # ------------------------------------------------------------ read paths
    @property
    def record_count(self) -> int:
        """Return the number of records held in memory, committed or not."""
        return len(self._records)

    @property
    def pending_count(self) -> int:
        """Return how many appended records the watermark does not cover."""
        return len(self._records) - self._watermark

    def all_records(self) -> list[Record]:
        """Return every record in sequence order, including pending ones."""
        return list(self._records)

    def visible(self, kind: str | None = None) -> list[Record]:
        """Return the committed records, optionally narrowed by kind."""
        return [
            record
            for record in self._records
            if record.seq <= self._watermark and (kind is None or record.kind == kind)
        ]

    def pending(self, kind: str | None = None) -> list[Record]:
        """Return the records the watermark does not yet cover."""
        return [
            record
            for record in self._records
            if record.seq > self._watermark and (kind is None or record.kind == kind)
        ]

    def replay(self, since_seq: int = 0, kind: str | None = None) -> list[Record]:
        """Return committed records with a sequence above ``since_seq``."""
        return [
            record
            for record in self.visible(kind)
            if record.seq > since_seq
        ]

    def visible_view(self) -> KeyView:
        """Return the keyed view of everything the watermark covers."""
        return build_view(self.visible())

    def view_at(self, watermark: int) -> KeyView:
        """Return the keyed view as it stood at an earlier watermark."""
        return build_view(
            record for record in self._records if record.visible_at(watermark)
        )

    def trace(self, key: str, committed_only: bool = True) -> list[Record]:
        """Return the write history of one key."""
        source = self.visible() if committed_only else self.all_records()
        return trace_for(source, key)

    # ------------------------------------------------------------- rollback
    def discard_uncommitted(self) -> int:
        """Drop every record above the watermark and rewrite the log.

        This is the rollback path: a step that was appended but never committed
        leaves no trace once it is discarded.
        """
        if self.pending_count == 0:
            return 0
        dropped = len(self._records) - self._watermark
        self._records = self._records[: self._watermark]
        self._writer.rewrite(record.to_dict() for record in self._records)
        return dropped

    # ------------------------------------------------------------- validate
    def verify_chain(self) -> int:
        """Recheck the digest chain and return the number of records walked."""
        previous = GENESIS_DIGEST
        for index, record in enumerate(self._records, start=1):
            if record.seq != index:
                raise ValidationError("record sequence is not contiguous", seq=record.seq)
            if record.previous != previous:
                raise ValidationError("record chain is broken", seq=record.seq)
            previous = record.digest
        return len(self._records)

    def kinds(self) -> list[str]:
        """Return the distinct record kinds present in the visible stream."""
        return sorted({record.kind for record in self.visible()})

    def slice(self, start_tick: int, end_tick: int) -> list[Record]:
        """Return visible records whose tick falls inside a closed interval."""
        return [
            record
            for record in self.visible()
            if start_tick <= record.tick <= end_tick
        ]

    def group_by_kind(self) -> dict[str, int]:
        """Count visible records per kind."""
        counts: dict[str, int] = {}
        for record in self.visible():
            counts[record.kind] = counts.get(record.kind, 0) + 1
        return counts

    def export(self) -> list[dict[str, Any]]:
        """Return the committed stream as plain dictionaries."""
        return [record.to_dict() for record in self.visible()]
