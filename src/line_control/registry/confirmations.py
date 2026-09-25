"""Single use, expiring confirmations.

An operator confirmation is only good once, only for the scope it was issued
for, and only for a limited number of clock ticks.  Stale, expired, unknown
and already redeemed tickets are refused with distinct codes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from line_control.registry.generations import GenerationLedger
from line_control.runtime.clock import LogicalClock
from line_control.runtime.errors import (
    DuplicateRecordError,
    ExpiredCredentialError,
    StaleCredentialError,
    UnknownReferenceError,
    ValidationError,
)
from line_control.runtime.keys import scope_key
from line_control.runtime.tickets import TicketGenerator
from line_control.store.stream import RecordStream

DEFAULT_WINDOW = 64


@dataclass(frozen=True)
class Confirmation:
    """A ticket that authorises one step in one scope."""

    ticket: str
    scope: str
    subject: str
    generation: int
    issued_tick: int
    expires_tick: int
    consumed_tick: int | None = None
    revoked: bool = False

    @property
    def key(self) -> str:
        """Return the stream key this confirmation is stored under."""
        return scope_key("confirm", self.ticket)

    def is_expired(self, tick: int) -> bool:
        """Report whether the validity window has elapsed."""
        return tick > self.expires_tick

    def is_consumed(self) -> bool:
        """Report whether the ticket was already spent."""
        return self.consumed_tick is not None

    def to_dict(self) -> dict[str, Any]:
        """Render the confirmation for the wire."""
        return {
            "ticket": self.ticket,
            "scope": self.scope,
            "subject": self.subject,
            "generation": self.generation,
            "issued_tick": self.issued_tick,
            "expires_tick": self.expires_tick,
            "consumed_tick": self.consumed_tick,
            "revoked": self.revoked,
        }


class ConfirmationBoard:
    """Issues and redeems confirmations, reading state back from the stream."""

    def __init__(
        self,
        stream: RecordStream,
        clock: LogicalClock,
        ledger: GenerationLedger,
        tickets: TicketGenerator,
    ) -> None:
        self._stream = stream
        self._clock = clock
        self._ledger = ledger
        self._tickets = tickets

    # ---------------------------------------------------------------- issuing
    def issue(self, scope: str, subject: str, window: int = DEFAULT_WINDOW) -> Confirmation:
        """Hand out a confirmation that expires after ``window`` ticks."""
        if not scope or not subject:
            raise ValidationError("confirmation scope and subject are required")
        if window <= 0:
            raise ValidationError("confirmation window must be positive")
        ticket = self._tickets.issue()
        issued_tick = self._clock.current
        generation = self._ledger.current(scope)
        record = self._stream.append(
            "confirm.issue",
            scope_key("confirm", ticket),
            {
                "ticket": ticket,
                "scope": scope,
                "subject": subject,
                "issued_tick": issued_tick,
                "expires_tick": issued_tick + window,
                "generation": generation,
            },
            generation=generation,
        )
        self._stream.commit_upto(record.seq)
        return Confirmation(
            ticket=ticket,
            scope=scope,
            subject=subject,
            generation=generation,
            issued_tick=issued_tick,
            expires_tick=issued_tick + window,
        )

    # ---------------------------------------------------------------- reading
    def read(self, ticket: str) -> Confirmation | None:
        """Return a confirmation by ticket, or ``None`` when unknown."""
        record = self._stream.visible_view().current(scope_key("confirm", ticket))
        if record is None:
            return None
        payload = record.payload
        return Confirmation(
            ticket=str(payload.get("ticket", ticket)),
            scope=str(payload.get("scope", "")),
            subject=str(payload.get("subject", "")),
            generation=int(payload.get("generation", 0)),
            issued_tick=int(payload.get("issued_tick", 0)),
            expires_tick=int(payload.get("expires_tick", 0)),
            consumed_tick=payload.get("consumed_tick"),
            revoked=bool(payload.get("revoked", False)),
        )

    def outstanding(self, scope: str | None = None) -> list[Confirmation]:
        """Return confirmations that are neither consumed nor expired."""
        out: list[Confirmation] = []
        for record in self._stream.visible("confirm.issue"):
            confirmation = self.read(str(record.payload.get("ticket", "")))
            if confirmation is None:
                continue
            if scope is not None and confirmation.scope != scope:
                continue
            if (
                confirmation.revoked
                or confirmation.is_consumed()
                or confirmation.is_expired(self._clock.current)
            ):
                continue
            out.append(confirmation)
        return out

    # -------------------------------------------------------------- redeeming
    def assert_usable(self, ticket: str, subject: str | None = None) -> Confirmation:
        """Refuse an unknown, stale, expired or already redeemed confirmation."""
        confirmation = self.read(ticket)
        if confirmation is None:
            raise UnknownReferenceError(
                f"confirmation {ticket} was never issued", ticket=ticket
            )
        if subject is not None and confirmation.subject != subject:
            raise UnknownReferenceError(
                f"confirmation {ticket} was issued for another subject",
                ticket=ticket,
                expected=subject,
                found=confirmation.subject,
            )
        if confirmation.is_consumed():
            raise DuplicateRecordError(
                f"confirmation {ticket} was already redeemed",
                ticket=ticket,
                consumed_tick=confirmation.consumed_tick,
            )
        if confirmation.revoked:
            raise StaleCredentialError(
                f"confirmation {ticket} was withdrawn",
                ticket=ticket,
            )
        if confirmation.is_expired(self._clock.current):
            raise ExpiredCredentialError(
                f"confirmation {ticket} outlived its window",
                ticket=ticket,
                expires_tick=confirmation.expires_tick,
                tick=self._clock.current,
            )
        current = self._ledger.current(confirmation.scope)
        if confirmation.generation != current:
            raise StaleCredentialError(
                f"confirmation {ticket} belongs to an older generation",
                ticket=ticket,
                confirmation_generation=confirmation.generation,
                current_generation=current,
            )
        return confirmation

    def consume(self, ticket: str, subject: str | None = None) -> Confirmation:
        """Redeem a confirmation exactly once."""
        confirmation = self.assert_usable(ticket, subject)
        consumed_tick = self._clock.tick()
        record = self._stream.append(
            "confirm.consume",
            scope_key("confirm", ticket),
            {
                "ticket": ticket,
                "scope": confirmation.scope,
                "subject": confirmation.subject,
                "issued_tick": confirmation.issued_tick,
                "expires_tick": confirmation.expires_tick,
                "consumed_tick": consumed_tick,
                "generation": confirmation.generation,
            },
            generation=confirmation.generation,
        )
        self._stream.commit_upto(record.seq)
        return Confirmation(
            ticket=confirmation.ticket,
            scope=confirmation.scope,
            subject=confirmation.subject,
            generation=confirmation.generation,
            issued_tick=confirmation.issued_tick,
            expires_tick=confirmation.expires_tick,
            consumed_tick=consumed_tick,
        )

    def reissue(self, ticket: str, window: int = DEFAULT_WINDOW) -> Confirmation:
        """Withdraw a confirmation and hand out a fresh ticket in the same scope."""
        previous = self.read(ticket)
        if previous is None:
            raise UnknownReferenceError(
                f"confirmation {ticket} was never issued", ticket=ticket
            )
        record = self._stream.append(
            "confirm.revoke",
            scope_key("confirm", ticket),
            {
                "ticket": ticket,
                "scope": previous.scope,
                "subject": previous.subject,
                "generation": previous.generation,
                "issued_tick": previous.issued_tick,
                "expires_tick": previous.expires_tick,
                "revoked": True,
            },
            generation=previous.generation,
        )
        self._stream.commit_upto(record.seq)
        return self.issue(previous.scope, previous.subject, window=window)
