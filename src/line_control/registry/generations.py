"""Per scope generation counters.

Every generation aware artefact carries the counter value that was current
when it was produced.  Bumping a scope is what makes older artefacts stale.
"""

from __future__ import annotations

from line_control.runtime.errors import ValidationError
from line_control.store.meta import MetaStore


class GenerationLedger:
    """Monotonic per scope generation numbers, persisted between boots."""

    __slots__ = ("_counters",)

    def __init__(self, counters: MetaStore) -> None:
        self._counters = counters

    def _key(self, scope: str) -> str:
        if not scope:
            raise ValidationError("generation scope is required")
        return f"generation.{scope}"

    def current(self, scope: str) -> int:
        """Return the generation a new artefact in ``scope`` would carry."""
        return int(self._counters.read(self._key(scope), 0))

    def bump(self, scope: str) -> int:
        """Advance a scope and return the new generation."""
        return self._counters.increment(self._key(scope))

    def restore(self, scope: str, value: int) -> int:
        """Set a scope forward when a recovered log proves a higher value."""
        current = self.current(scope)
        if int(value) > current:
            self._counters.write(self._key(scope), int(value))
            return int(value)
        return current

    def scopes(self) -> list[str]:
        """Return the scopes that already carry a generation."""
        prefix = "generation."
        return sorted(
            key[len(prefix):] for key in self._counters.keys() if key.startswith(prefix)
        )
