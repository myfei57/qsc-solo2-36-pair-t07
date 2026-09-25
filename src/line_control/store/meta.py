"""Small persisted dictionary used for counters and recovery marks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from line_control.store.segments import AtomicJsonFile


class MetaStore:
    """A JSON document holding integer counters and small recovery marks."""

    __slots__ = ("_file", "_values")

    def __init__(self, path: Path, durable: bool = True) -> None:
        self._file = AtomicJsonFile(Path(path), durable=durable)
        raw = self._file.read({})
        self._values: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}

    def read(self, key: str, default: Any = 0) -> Any:
        """Return a stored value, or ``default`` when the key is absent."""
        return self._values.get(key, default)

    def write(self, key: str, value: Any) -> None:
        """Store a value and persist the document."""
        self._values[key] = value
        self._file.write(self._values)

    def increment(self, key: str, delta: int = 1) -> int:
        """Add to an integer counter and return the new value."""
        current = self._values.get(key, 0)
        if not isinstance(current, int):
            current = 0
        updated = current + int(delta)
        self.write(key, updated)
        return updated

    def keys(self) -> list[str]:
        """Return every stored key in sorted order."""
        return sorted(self._values)

    def snapshot(self) -> dict[str, Any]:
        """Return a detached copy of the whole document."""
        return dict(self._values)
