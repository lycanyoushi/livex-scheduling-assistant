"""The shared request and NDJSON event contract for the browser and CLI."""

from __future__ import annotations

from datetime import date
import re
from typing import Annotated, Literal, NotRequired, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, field_validator
from pydantic.alias_generators import to_camel


MAX_MESSAGE_LENGTH = 2_000
MAX_STATE_TOKEN_LENGTH = 256 * 1024
MAX_MODEL_TURNS = 40

SlotRef = Annotated[str, StringConstraints(pattern=r"^S[1-9][0-9]{0,23}$")]
BookingRef = Annotated[str, StringConstraints(pattern=r"^B[1-9][0-9]{0,23}$")]
ProposalId = Annotated[str, StringConstraints(pattern=r"^P[1-9][0-9]{0,23}$")]
ContactName = Annotated[str, StringConstraints(max_length=200)]
ContactEmail = Annotated[str, StringConstraints(max_length=254)]
ContactNotes = Annotated[str, StringConstraints(max_length=MAX_MESSAGE_LENGTH)]


def validate_time_zone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("Expected an IANA time zone.") from None
    return value


def _validate_calendar_date(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Expected a date in YYYY-MM-DD form.")
    date.fromisoformat(value)
    return value


TimeZoneName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128),
    AfterValidator(validate_time_zone),
]
CalendarDate = Annotated[str, AfterValidator(_validate_calendar_date)]


class _WireModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        alias_generator=to_camel,
        hide_input_in_errors=True,
    )


class PickSlot(_WireModel):
    type: Literal["pick_slot"]
    slot_ref: SlotRef


class MoreSlots(_WireModel):
    type: Literal["more_slots"]
    from_date: CalendarDate | None = None

    @field_validator("from_date", mode="before")
    @classmethod
    def _reject_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("Omit fromDate when no date is selected.")
        return value


class ConfirmProposal(_WireModel):
    type: Literal["confirm_proposal"]
    proposal_id: ProposalId
    name: ContactName | None = None
    email: ContactEmail | None = None
    notes: ContactNotes | None = None

    @field_validator("name", "email", "notes", mode="before")
    @classmethod
    def _reject_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("Omit optional contact fields without a value.")
        return value


class DismissProposal(_WireModel):
    type: Literal["dismiss_proposal"]
    proposal_id: ProposalId


class RequestCancel(_WireModel):
    type: Literal["request_cancel"]
    booking_ref: BookingRef


class RequestReschedule(_WireModel):
    type: Literal["request_reschedule"]
    booking_ref: BookingRef


UiAction = Annotated[
    PickSlot | MoreSlots | ConfirmProposal | DismissProposal | RequestCancel | RequestReschedule,
    Field(discriminator="type"),
]


class TextInput(_WireModel):
    kind: Literal["text"]
    text: Annotated[str, StringConstraints(min_length=1, max_length=MAX_MESSAGE_LENGTH)]

    @field_validator("text")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter a message before sending.")
        return value


class UiInput(_WireModel):
    kind: Literal["ui"]
    action: UiAction


class ChatRequest(_WireModel):
    state_token: Annotated[str, StringConstraints(min_length=1, max_length=MAX_STATE_TOKEN_LENGTH)] | None
    time_zone: TimeZoneName
    input: Annotated[TextInput | UiInput, Field(discriminator="kind")]


class SlotView(TypedDict):
    dayLabel: str
    timeLabel: str
    startLocalIso: str


class BookingView(TypedDict):
    ref: str
    title: str
    whenLabel: str
    status: str
    attendees: list[str]
    meetingUrl: NotRequired[str]
    startUtc: str
    endUtc: str


class ProposalView(TypedDict):
    id: str
    kind: Literal["book", "cancel", "reschedule"]
    title: str
    whenLabel: str
    timeZone: str
    durationMinutes: int
    slotRef: NotRequired[str]
    bookingRef: NotRequired[str]
    name: NotRequired[str]
    email: NotRequired[str]
    notes: NotRequired[str]


class PrescanMetrics(TypedDict):
    email: bool
    dates: list[str]
    partOfDay: NotRequired[str]
    intents: list[str]
    availabilityRequest: NotRequired[bool]
    exactTimes: NotRequired[list[str]]


class PrefetchMetrics(TypedDict):
    ms: float
    slots: int
    bookings: int
    timedOut: bool
    slotsChecked: NotRequired[bool]
    shownSlots: NotRequired[int]


class ModelCallMetrics(TypedDict):
    ttftMs: float
    totalMs: float
    inputTokens: int
    outputTokens: int
    cachedTokens: int
    thoughtTokens: int
    historyRotated: NotRequired[bool]
    status: str


class ToolCallMetrics(TypedDict):
    name: str
    ms: float
    ok: bool


class CalCallMetrics(TypedDict):
    endpoint: str
    ms: float
    cacheHit: bool
    status: int


class TurnMetrics(TypedDict):
    path: Literal["ui_action", "model"]
    prescan: PrescanMetrics
    prefetch: PrefetchMetrics
    modelCalls: list[ModelCallMetrics]
    toolCalls: list[ToolCallMetrics]
    calCalls: list[CalCallMetrics]
    unknownRefs: int
    totalMs: float
    outcome: Literal["ok", "failed"]
    fallback: NotRequired[Literal["backup_model", "code_only"]]
    errorCode: NotRequired[str]


class StatusEvent(TypedDict):
    type: Literal["status"]
    text: str


class RefsEvent(TypedDict):
    type: Literal["refs"]
    slots: NotRequired[dict[str, SlotView]]
    bookings: NotRequired[dict[str, BookingView]]


class TextDeltaEvent(TypedDict):
    type: Literal["text_delta"]
    text: str


class ProposalEvent(TypedDict):
    type: Literal["proposal"]
    proposal: ProposalView


class BookingResultEvent(TypedDict):
    type: Literal["booking_result"]
    kind: Literal["booked", "cancelled", "rescheduled"]
    booking: BookingView


class SlotChipsEvent(TypedDict):
    type: Literal["slot_chips"]
    refs: list[str]
    note: NotRequired[str]


class AssistantTextEvent(TypedDict):
    type: Literal["assistant_text"]
    text: str


class ErrorEvent(TypedDict):
    type: Literal["error"]
    code: str
    message: str
    retryable: bool
    fallback: NotRequired[Literal["booker"]]


class TurnEndEvent(TypedDict):
    type: Literal["turn_end"]
    stateToken: str
    metrics: TurnMetrics


ServerEvent = (
    StatusEvent | RefsEvent | TextDeltaEvent | ProposalEvent | BookingResultEvent
    | SlotChipsEvent | AssistantTextEvent | ErrorEvent | TurnEndEvent
)
