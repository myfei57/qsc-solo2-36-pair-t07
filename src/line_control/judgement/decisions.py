"""Rules evaluated against the current view or a historical one.

The same rule table can be pointed at the live state or at the state as it
stood at an earlier watermark, which is what lets an operator ask "what did
the interlock see at the time" without replaying the plant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from line_control.runtime.clock import LogicalClock
from line_control.runtime.errors import UnknownReferenceError, ValidationError
from line_control.runtime.keys import scope_key
from line_control.store.stream import RecordStream
from line_control.store.views import KeyView

CURRENT = "current"
HISTORICAL = "historical"


@dataclass(frozen=True)
class DecisionContext:
    """Everything a rule may look at."""

    unit: str
    mode: str
    watermark: int
    generation: int
    values: Mapping[str, Mapping[str, Any] | None] = field(default_factory=dict)

    def payload(self, key: str) -> dict[str, Any]:
        """Return the observed record payload for a key, empty when absent."""
        observed = self.values.get(key)
        if observed is None:
            return {}
        return dict(observed)

    def key(self, *parts: object) -> str:
        """Build a stream key in the same form the context was collected with."""
        return scope_key(*parts)

    def present(self, key: str) -> bool:
        """Report whether the key carried a visible record."""
        return self.values.get(key) is not None

    def flag(self, key: str, field_name: str, default: bool = False) -> bool:
        """Return one observed flag."""
        return bool(self.payload(key).get(field_name, default))

    def number(self, key: str, field_name: str = "value", default: float = 0.0) -> float:
        """Return one observed value coerced to a number."""
        raw = self.payload(key).get(field_name, default)
        if isinstance(raw, bool):
            return 1.0 if raw else 0.0
        if isinstance(raw, (int, float)):
            return float(raw)
        try:
            return float(str(raw))
        except (TypeError, ValueError):
            return default

    def text(self, key: str, field_name: str, default: str = "") -> str:
        """Return one observed value coerced to text."""
        raw = self.payload(key).get(field_name, default)
        return default if raw is None else str(raw)


@dataclass(frozen=True)
class DecisionRule:
    """One named rule that yields an outcome when its predicate holds."""

    name: str
    outcome: str
    predicate: Callable[[DecisionContext], bool]
    priority: int = 0


@dataclass(frozen=True)
class Decision:
    """The outcome of running a rule table."""

    table: str
    rule: str
    outcome: str
    unit: str
    mode: str
    watermark: int

    def to_dict(self) -> dict[str, Any]:
        """Render the decision for the wire."""
        return {
            "table": self.table,
            "rule": self.rule,
            "outcome": self.outcome,
            "unit": self.unit,
            "mode": self.mode,
            "watermark": self.watermark,
        }


class DecisionTable:
    """An ordered list of rules evaluated by descending priority."""

    def __init__(self, name: str, fallback: str = "hold") -> None:
        if not name:
            raise ValidationError("decision table name is required")
        self._name = name
        self._fallback = fallback
        self._rules: list[DecisionRule] = []

    @property
    def name(self) -> str:
        return self._name

    def add(self, rule: DecisionRule) -> DecisionRule:
        """Append a rule to the table."""
        self._rules.append(rule)
        return rule

    def rules(self) -> list[DecisionRule]:
        """Return the rules in evaluation order."""
        return list(self._rules)

    def decide(self, context: DecisionContext) -> Decision:
        """Run the table against a context and return the first match."""
        ordered: Sequence[DecisionRule] = sorted(
            self._rules, key=lambda rule: rule.priority, reverse=True
        )
        for rule in ordered:
            if rule.predicate(context):
                return Decision(
                    table=self._name,
                    rule=rule.name,
                    outcome=rule.outcome,
                    unit=context.unit,
                    mode=context.mode,
                    watermark=context.watermark,
                )
        return Decision(
            table=self._name,
            rule="fallback",
            outcome=self._fallback,
            unit=context.unit,
            mode=context.mode,
            watermark=context.watermark,
        )


class DecisionService:
    """Builds decision contexts from the stream, then runs a table."""

    def __init__(self, stream: RecordStream, clock: LogicalClock) -> None:
        self._stream = stream
        self._clock = clock
        self._tables: dict[str, DecisionTable] = {}

    def register(self, table: DecisionTable) -> DecisionTable:
        """Make a table available by name."""
        self._tables[table.name] = table
        return table

    def table(self, name: str) -> DecisionTable:
        """Return a registered table."""
        try:
            return self._tables[name]
        except KeyError as missing:
            raise UnknownReferenceError(
                f"decision table {name} is not registered", table=name
            ) from missing

    def table_names(self) -> list[str]:
        """Return every registered table name."""
        return sorted(self._tables)

    def view_for(self, mode: str, watermark: int | None = None) -> tuple[KeyView, int]:
        """Return the view a mode resolves to, plus the watermark behind it."""
        if mode == CURRENT:
            return self._stream.visible_view(), self._stream.watermark
        if mode == HISTORICAL:
            target = self._stream.watermark if watermark is None else int(watermark)
            return self._stream.view_at(target), target
        raise ValidationError("unknown decision mode", mode=mode)

    def context(
        self,
        unit: str,
        mode: str = CURRENT,
        watermark: int | None = None,
        keys: Sequence[str] = (),
    ) -> DecisionContext:
        """Collect the observed values a rule table may consult."""
        view, resolved = self.view_for(mode, watermark)
        values: dict[str, Any] = {}
        for key in keys:
            record = view.current(key)
            values[key] = None if record is None else dict(record.payload)
        return DecisionContext(
            unit=unit,
            mode=mode,
            watermark=resolved,
            generation=view.generation_of(keys[0]) if keys else 0,
            values=values,
        )

    def decide(
        self,
        table: str,
        unit: str,
        mode: str = CURRENT,
        watermark: int | None = None,
        keys: Sequence[str] = (),
    ) -> Decision:
        """Run a named table against a freshly collected context."""
        context = self.context(unit, mode=mode, watermark=watermark, keys=keys)
        return self.table(table).decide(context)

    def compare(
        self,
        table: str,
        unit: str,
        watermark: int,
        keys: Sequence[str] = (),
    ) -> tuple[Decision, Decision]:
        """Return the decision now and the decision as it stood historically."""
        now = self.decide(table, unit, mode=CURRENT, keys=keys)
        then = self.decide(table, unit, mode=HISTORICAL, watermark=watermark, keys=keys)
        return now, then
