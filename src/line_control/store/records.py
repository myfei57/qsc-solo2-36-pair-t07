"""The record shape written into the append only stream."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from line_control.runtime.errors import StreamIntegrityError

GENESIS_DIGEST = "0" * 64


def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    """Render a payload so that equal content always hashes the same way."""
    return json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def chain_digest(previous: str, body: Mapping[str, Any]) -> str:
    """Fold a record body onto the digest of its predecessor."""
    material = previous.encode("utf-8") + b"|" + canonical_bytes(body)
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True)
class Record:
    """One immutable entry of the append only stream."""

    seq: int
    kind: str
    key: str
    payload: dict[str, Any]
    generation: int
    tick: int
    tombstone: bool
    previous: str
    digest: str

    def visible_at(self, watermark: int) -> bool:
        """Report whether a commit watermark already covers this record."""
        return self.seq <= watermark

    def body(self) -> dict[str, Any]:
        """Return the fields that participate in the digest."""
        return {
            "seq": self.seq,
            "kind": self.kind,
            "key": self.key,
            "payload": self.payload,
            "generation": self.generation,
            "tick": self.tick,
            "tombstone": self.tombstone,
            "previous": self.previous,
        }

    def to_dict(self) -> dict[str, Any]:
        """Render the record for the wire and for the segment file."""
        rendered = self.body()
        rendered["digest"] = self.digest
        return rendered

    @classmethod
    def build(
        cls,
        *,
        seq: int,
        kind: str,
        key: str,
        payload: Mapping[str, Any],
        generation: int,
        tick: int,
        tombstone: bool,
        previous: str,
    ) -> "Record":
        """Compose a record and seal it with its digest."""
        draft = cls(
            seq=seq,
            kind=kind,
            key=key,
            payload=dict(payload),
            generation=generation,
            tick=tick,
            tombstone=tombstone,
            previous=previous,
            digest="",
        )
        sealed = chain_digest(previous, draft.body())
        return cls(
            seq=seq,
            kind=kind,
            key=key,
            payload=dict(payload),
            generation=generation,
            tick=tick,
            tombstone=tombstone,
            previous=previous,
            digest=sealed,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Record":
        """Rebuild a record from a segment line and verify its digest."""
        required = ("seq", "kind", "key", "tick", "previous", "digest")
        missing = [field for field in required if field not in raw]
        if missing:
            raise StreamIntegrityError(
                "record line is missing fields", missing=missing
            )
        payload = raw.get("payload") or {}
        if not isinstance(payload, dict):
            raise StreamIntegrityError("record payload must be an object")
        record = cls(
            seq=int(raw["seq"]),
            kind=str(raw["kind"]),
            key=str(raw["key"]),
            payload=dict(payload),
            generation=int(raw.get("generation", 0)),
            tick=int(raw["tick"]),
            tombstone=bool(raw.get("tombstone", False)),
            previous=str(raw["previous"]),
            digest=str(raw["digest"]),
        )
        expected = chain_digest(record.previous, record.body())
        if expected != record.digest:
            raise StreamIntegrityError(
                "record digest does not match its body",
                seq=record.seq,
                expected=expected,
                found=record.digest,
            )
        return record
