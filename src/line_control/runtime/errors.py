"""Rejection taxonomy shared by every subsystem.

Each error carries a stable ``code`` so the transport layer can map a refusal
to a status without reading prose, and so tests can assert on the reason
rather than on a message that may be reworded later.
"""

from __future__ import annotations

from typing import Any


class ControlError(Exception):
    """Base class for every deliberate refusal."""

    code = "control_error"
    status = 409

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = dict(context)

    def to_dict(self) -> dict[str, Any]:
        """Render the refusal as a JSON friendly mapping."""
        payload: dict[str, Any] = {"error": self.message, "code": self.code}
        if self.context:
            payload["context"] = self.context
        return payload


class ValidationError(ControlError):
    """A request carried a malformed or impossible argument."""

    code = "validation"
    status = 400


class UnknownReferenceError(ControlError):
    """A request named something the service has never seen."""

    code = "unknown_reference"
    status = 404


class OrderingError(ControlError):
    """A step was taken out of the order the process allows."""

    code = "ordering"
    status = 409


class NotDurableError(ControlError):
    """A dependent step ran before its prerequisite reached the record stream."""

    code = "not_durable"
    status = 409


class GateBlockedError(ControlError):
    """A pre-gate refused the step."""

    code = "gate_blocked"
    status = 409


class LatchEngagedError(ControlError):
    """A latch is set and has not been cleared."""

    code = "latch_engaged"
    status = 409


class StaleCredentialError(ControlError):
    """A confirmation, snapshot or baseline belongs to an older generation."""

    code = "stale_credential"
    status = 409


class ExpiredCredentialError(ControlError):
    """A confirmation outlived its validity window."""

    code = "expired_credential"
    status = 409


class DuplicateRecordError(ControlError):
    """An identifier that must be unique was presented twice."""

    code = "duplicate"
    status = 409


class LimitViolationError(ControlError):
    """A value fell outside the range the process tolerates."""

    code = "limit_violation"
    status = 409


class LightOffFailedError(ControlError):
    """Ignition was attempted and the flame did not establish."""

    code = "light_off_failed"
    status = 409


class StreamIntegrityError(ControlError):
    """The record stream no longer matches its own digest chain."""

    code = "stream_integrity"
    status = 500


STATUS_BY_CODE: dict[str, int] = {
    ValidationError.code: ValidationError.status,
    UnknownReferenceError.code: UnknownReferenceError.status,
    OrderingError.code: OrderingError.status,
    NotDurableError.code: NotDurableError.status,
    GateBlockedError.code: GateBlockedError.status,
    LatchEngagedError.code: LatchEngagedError.status,
    StaleCredentialError.code: StaleCredentialError.status,
    ExpiredCredentialError.code: ExpiredCredentialError.status,
    DuplicateRecordError.code: DuplicateRecordError.status,
    LimitViolationError.code: LimitViolationError.status,
    LightOffFailedError.code: LightOffFailedError.status,
    StreamIntegrityError.code: StreamIntegrityError.status,
}


def http_status(error: ControlError) -> int:
    """Return the transport status that matches a refusal."""
    return STATUS_BY_CODE.get(error.code, error.status)
