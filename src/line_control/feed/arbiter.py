"""Arbitration between the governor demand and the protection demand.

The protection channel may only ever pull the setpoint down.  Whichever
channel is lower wins, and the result records which one it was so an operator
can see why the valve moved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from line_control.runtime.errors import LimitViolationError

GOVERNOR = "governor"
PROTECTION = "protection"


@dataclass(frozen=True)
class Demand:
    """The arbitrated demand handed to the feed valve."""

    unit: str
    value: int
    source: str
    governor: int
    protection: int

    @property
    def limited(self) -> bool:
        """Report whether the protection channel overrode the governor."""
        return self.source == PROTECTION

    def to_dict(self) -> dict[str, Any]:
        """Render the demand for the wire."""
        return {
            "unit": self.unit,
            "value": self.value,
            "source": self.source,
            "governor": self.governor,
            "protection": self.protection,
            "limited": self.limited,
        }


def arbitrate(unit: str, governor: int, protection: int) -> Demand:
    """Return the lower of the two channels and name the winner."""
    if not unit:
        raise LimitViolationError("a unit is required to arbitrate a demand", unit=unit)
    governor_value = int(governor)
    protection_value = int(protection)
    if protection_value and protection_value < governor_value:
        return Demand(unit, protection_value, PROTECTION, governor_value, protection_value)
    return Demand(unit, governor_value, GOVERNOR, governor_value, protection_value)
