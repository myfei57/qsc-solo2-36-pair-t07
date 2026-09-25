"""Byte level persistence helpers for the record stream."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping


class JsonLineWriter:
    """Append JSON objects as newline delimited text."""

    __slots__ = ("_path", "_durable")

    def __init__(self, path: Path, durable: bool = True) -> None:
        self._path = Path(path)
        self._durable = durable

    def append(self, payload: Mapping[str, Any]) -> None:
        """Append one line and push it to the filesystem."""
        line = json.dumps(dict(payload), sort_keys=True, ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")
            handle.flush()
            if self._durable:
                os.fsync(handle.fileno())

    def rewrite(self, payloads: Iterable[Mapping[str, Any]]) -> None:
        """Replace the segment with an exact sequence of lines."""
        scratch = self._path.with_suffix(self._path.suffix + ".rewrite")
        with scratch.open("w", encoding="utf-8") as handle:
            for payload in payloads:
                handle.write(json.dumps(dict(payload), sort_keys=True, ensure_ascii=False))
                handle.write("\n")
            handle.flush()
            if self._durable:
                os.fsync(handle.fileno())
        os.replace(scratch, self._path)


class JsonLineReader:
    """Read newline delimited JSON objects back into memory."""

    __slots__ = ("_path",)

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def read_all(self) -> list[dict[str, Any]]:
        """Return every parseable line in file order."""
        if not self._path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                out.append(json.loads(text))
        return out


class AtomicJsonFile:
    """Read and replace a JSON document without ever leaving it half written."""

    __slots__ = ("_path", "_durable")

    def __init__(self, path: Path, durable: bool = True) -> None:
        self._path = Path(path)
        self._durable = durable

    def read(self, default: Any) -> Any:
        """Return the stored document or ``default`` when nothing is stored."""
        if not self._path.exists():
            return default
        raw = self._path.read_text(encoding="utf-8").strip()
        if not raw:
            return default
        return json.loads(raw)

    def write(self, value: Any) -> None:
        """Write through a scratch file and swap it into place."""
        scratch = self._path.with_suffix(self._path.suffix + ".tmp")
        scratch.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_text(
            json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if self._durable:
            with scratch.open("r+b") as handle:
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(scratch, self._path)
