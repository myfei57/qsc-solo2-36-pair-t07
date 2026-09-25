"""Shared primitives: ordering clock, ticket issuance, error taxonomy."""

from line_control.runtime.clock import LogicalClock
from line_control.runtime.errors import (
    ControlError,
    DuplicateRecordError,
    ExpiredCredentialError,
    GateBlockedError,
    LatchEngagedError,
    LightOffFailedError,
    LimitViolationError,
    NotDurableError,
    OrderingError,
    StaleCredentialError,
    StreamIntegrityError,
    UnknownReferenceError,
    ValidationError,
    http_status,
)
from line_control.runtime.keys import normalize_key, scope_key, slug
from line_control.runtime.tickets import TicketGenerator

__all__ = [
    "ControlError",
    "DuplicateRecordError",
    "ExpiredCredentialError",
    "GateBlockedError",
    "LatchEngagedError",
    "LightOffFailedError",
    "LimitViolationError",
    "LogicalClock",
    "NotDurableError",
    "OrderingError",
    "StaleCredentialError",
    "StreamIntegrityError",
    "TicketGenerator",
    "UnknownReferenceError",
    "ValidationError",
    "http_status",
    "normalize_key",
    "scope_key",
    "slug",
]
