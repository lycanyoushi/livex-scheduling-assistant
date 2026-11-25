"""Mutable server state carried in a signed, expiring session token."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
import hmac
import re
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator
from pydantic.alias_generators import to_camel

from backend.protocol import (
    MAX_STATE_TOKEN_LENGTH,
    BookingRef,
    CalendarDate,
    ContactEmail,
    ContactName,
    ContactNotes,
    ProposalId,
    SlotRef,
    TimeZoneName,
)


MAX_SLOTS = 40
MAX_BOOKINGS = 20
MAX_NOTES = 10
MAX_COMPLETED = 10
SESSION_TTL = timedelta(days=1)
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")

_Counter = Annotated[int, Field(ge=0, lt=10**24)]
_EventTypeId = Annotated[int, Field(gt=0)]
_EpochSeconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
_Summary = Annotated[str, StringConstraints(max_length=2_000)]
_OpaqueId = Annotated[str, StringConstraints(min_length=1, max_length=512)]
_Fingerprint = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]


def _validate_utc_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("Expected an ISO 8601 timestamp in UTC.")
    return value


_UtcTimestamp = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|\+00:00)$"
    ),
    AfterValidator(_validate_utc_timestamp),
]


class StateError(ValueError):
    """The signed token is malformed, invalid, or expired."""


class _StateModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        alias_generator=to_camel,
        validate_by_name=True,
        hide_input_in_errors=True,
    )


class UserState(_StateModel):
    name: ContactName | None = None
    email: ContactEmail | None = None
    conversation_name: ContactName | None = None


class GuestDraft(_StateModel):
    name: ContactName | None = None
    email: ContactEmail | None = None
    notes: ContactNotes | None = None
    duration_minutes: Annotated[int, Field(gt=0, le=1440)] | None = None
    slot_ref: SlotRef | None = None


class _TimeRange(_StateModel):
    start_utc: _UtcTimestamp
    end_utc: _UtcTimestamp

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if datetime.fromisoformat(self.start_utc) >= datetime.fromisoformat(self.end_utc):
            raise ValueError("The end must be later than the start.")
        return self


class SlotEntry(_TimeRange):
    event_type_id: _EventTypeId
    fetched_at: _EpochSeconds
    for_booking_ref: BookingRef | None = None


class BookingEntry(_TimeRange):
    uid: _OpaqueId
    title: Annotated[str, StringConstraints(max_length=512)]
    status: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    attendee_emails: Annotated[list[ContactEmail], Field(max_length=32)]
    guest_name: ContactName | None = None
    event_type_id: _EventTypeId | None = None
    meeting_url: Annotated[str, StringConstraints(max_length=2_048)] | None = None


class Counters(_StateModel):
    s: _Counter = 0
    b: _Counter = 0
    p: _Counter = 0
    turns: _Counter = 0


class Proposal(_StateModel):
    id: ProposalId
    kind: Literal["book", "cancel", "reschedule"]
    slot_ref: SlotRef | None = None
    booking_ref: BookingRef | None = None
    name: ContactName | None = None
    email: ContactEmail | None = None
    notes: ContactNotes | None = None
    reason: _Summary | None = None
    created_at: _EpochSeconds

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        if (self.slot_ref is not None) != (self.kind in ("book", "reschedule")):
            raise ValueError("The proposal has an invalid slot reference.")
        if (self.booking_ref is not None) != (self.kind in ("cancel", "reschedule")):
            raise ValueError("The proposal has an invalid booking reference.")
        return self


class ConfirmedOutcome(_StateModel):
    outcome: Literal["confirmed"]
    result: _Summary


class DismissedOutcome(_StateModel):
    outcome: Literal["dismissed"]


class SupersededOutcome(_StateModel):
    outcome: Literal["superseded"]
    by: ProposalId


class NoActionYetOutcome(_StateModel):
    outcome: Literal["no_action_yet"]
    note: _Summary


PendingOutcome = Annotated[
    ConfirmedOutcome | DismissedOutcome | SupersededOutcome | NoActionYetOutcome,
    Field(discriminator="outcome"),
]


class PendingCall(_StateModel):
    id: _OpaqueId
    name: Literal["propose_booking", "propose_cancel", "propose_reschedule"]
    proposal_id: ProposalId
    outcome: PendingOutcome | None = None


class ModelContext(_StateModel):
    local_date: CalendarDate
    time_zone: TimeZoneName
    slots: Annotated[dict[SlotRef, _Fingerprint], Field(max_length=MAX_SLOTS)] = Field(default_factory=dict)
    bookings: Annotated[dict[BookingRef, _Fingerprint], Field(max_length=MAX_BOOKINGS)] = Field(default_factory=dict)


class SessionState(_StateModel):
    v: Literal[2]
    session_id: _OpaqueId
    time_zone: TimeZoneName
    expires_at: Annotated[int, Field(gt=0)]
    user: UserState = Field(default_factory=UserState)
    guest_draft: GuestDraft | None = None
    last_offered_slots: Annotated[list[SlotRef], Field(max_length=4)] = Field(default_factory=list)
    slots: Annotated[dict[SlotRef, SlotEntry], Field(max_length=MAX_SLOTS)] = Field(default_factory=dict)
    bookings: Annotated[dict[BookingRef, BookingEntry], Field(max_length=MAX_BOOKINGS)] = Field(default_factory=dict)
    counters: Counters = Field(default_factory=Counters)
    proposal: Proposal | None = None
    rescheduling_booking_ref: BookingRef | None = None
    interaction_id: _OpaqueId | None = None
    model_context: ModelContext | None = None
    last_model_input_tokens: Annotated[int, Field(ge=0)] = 0
    pending_call: PendingCall | None = None
    notes: Annotated[list[_Summary], Field(max_length=MAX_NOTES)] = Field(default_factory=list)
    completed: Annotated[dict[ProposalId, _Summary], Field(max_length=MAX_COMPLETED)] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_counters(self) -> Self:
        proposals = list(self.completed)
        if self.proposal is not None:
            proposals.append(self.proposal.id)
        if self.pending_call is not None:
            proposals.append(self.pending_call.proposal_id)

        for refs, counter in (
            (self.slots, self.counters.s),
            (self.bookings, self.counters.b),
            (proposals, self.counters.p),
        ):
            if any(int(ref[1:]) > counter for ref in refs):
                raise ValueError("A reference exceeds its allocation counter.")
        return self


def _timestamp(now: datetime) -> float:
    if now.utcoffset() is None:
        raise ValueError("Session clocks must provide a time zone.")
    return now.timestamp()


def new_state(time_zone: str, now: datetime) -> SessionState:
    return SessionState(
        v=2,
        session_id=uuid4().hex,
        time_zone=time_zone,
        expires_at=int(_timestamp(now) + SESSION_TTL.total_seconds()),
    )


def add_slot(state: SessionState, entry: SlotEntry) -> str:
    state.counters.s += 1
    ref = f"S{state.counters.s}"
    state.slots[ref] = entry
    if len(state.slots) > MAX_SLOTS:
        protected = {
            state.proposal.slot_ref if state.proposal is not None else None,
            state.guest_draft.slot_ref if state.guest_draft is not None else None,
        }
        del state.slots[next(old for old in state.slots if old not in protected)]
    return ref


def add_booking(state: SessionState, entry: BookingEntry) -> str:
    state.counters.b += 1
    ref = f"B{state.counters.b}"
    state.bookings[ref] = entry
    if len(state.bookings) > MAX_BOOKINGS:
        protected = {state.rescheduling_booking_ref, state.proposal.booking_ref if state.proposal is not None else None}
        del state.bookings[next(old for old in state.bookings if old not in protected)]
    return ref


def _key_bytes(secret: str | bytes) -> bytes:
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    if len(key) < 32:
        raise ValueError("STATE_SIGNING_SECRET must be at least 32 bytes.")
    return key


def _encode_base64url(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def _signature(encoded_payload: bytes, secret: str | bytes) -> bytes:
    return _encode_base64url(hmac.digest(_key_bytes(secret), encoded_payload, "sha256"))


def sign_state(state: SessionState, secret: str | bytes) -> str:
    try:
        checked = SessionState.model_validate(state.model_dump(warnings=False))
    except ValidationError:
        raise StateError("Invalid session state.") from None
    json_payload = checked.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
    payload = _encode_base64url(json_payload)
    token = b".".join((payload, _signature(payload, secret))).decode("ascii")
    if len(token) > MAX_STATE_TOKEN_LENGTH:
        raise StateError("Session state exceeds the token size limit.")
    return token


def verify_state(token: str, secret: str | bytes, now: datetime) -> SessionState:
    if not isinstance(token, str) or len(token) > MAX_STATE_TOKEN_LENGTH:
        raise StateError("Invalid session token.")
    encoded, separator, signature = token.partition(".")
    if (
        not separator
        or len(signature) != 43
        or not _BASE64URL.fullmatch(encoded)
        or not _BASE64URL.fullmatch(signature)
    ):
        raise StateError("Invalid session token.")

    payload = encoded.encode("ascii")
    if not hmac.compare_digest(_signature(payload, secret), signature.encode("ascii")):
        raise StateError("Invalid session signature.")

    try:
        raw = base64.b64decode(payload + b"=" * (-len(payload) % 4), altchars=b"-_", validate=True)
        state = SessionState.model_validate_json(raw)
    except ValueError:
        raise StateError("Invalid session state.") from None
    if _timestamp(now) >= state.expires_at:
        raise StateError("The session has expired.")
    return state
