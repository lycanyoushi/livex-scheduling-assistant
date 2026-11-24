"""Validate model tool calls and turn calendar reads into short-lived references."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from threading import Lock
from typing import Annotated, Literal, Protocol, TypeVar
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from backend.cal_client import (
    Booking,
    BookingFilter,
    BookingPage,
    EventType,
    JsonObject,
    Profile,
    SlotBound,
    SlotRange,
)
from backend.prescan import EMAIL, PART_HOURS, DateHint, PartOfDay
from backend.protocol import (
    BookingRef,
    BookingView,
    CalendarDate,
    ContactEmail,
    ContactName,
    ContactNotes,
    ProposalEvent,
    ProposalView,
    RefsEvent,
    ServerEvent,
    SlotRef,
    SlotView,
)
from backend.state import MAX_SLOTS, BookingEntry, Proposal, SessionState, SlotEntry, add_booking, add_slot
from backend.time import local_bounds, parse_instant, slot_labels


_MUTABLE_BOOKING_STATUSES = {"accepted", "pending"}
_PARTS = ("any", "morning", "afternoon", "evening")
_PROPOSE_TOOLS = {"propose_booking", "propose_cancel", "propose_reschedule"}
_MAX_SLOT_CHOICES = 16
_MAX_BOOKING_CHOICES = 10
_STALE_SLOT_SECONDS = 120
WARM_SLOT_DAYS = 14
_Reason = Annotated[str, StringConstraints(max_length=2_000)]


class CalendarReader(Protocol):
    def me(self) -> Profile: ...

    def event_types(self, username: str) -> list[EventType]: ...

    def slots(
        self,
        event_type_id: int,
        start: SlotBound,
        end: SlotBound,
        time_zone: str = "UTC",
        *,
        booking_uid_to_reschedule: str | None = None,
        fresh: bool = False,
    ) -> dict[str, list[SlotRange]]: ...

    def list_bookings(
        self,
        *,
        attendee_email: str | None = None,
        status: BookingFilter | None = "upcoming",
        limit: int = 20,
        cursor: str | None = None,
    ) -> BookingPage: ...


@dataclass(frozen=True)
class ToolOutcome:
    result: JsonObject = field(default_factory=dict)
    events: list[ServerEvent] = field(default_factory=list)
    proposal: Proposal | None = None


class ToolRejected(ValueError):
    """An invalid model request with a safe function result."""

    def __init__(
        self,
        message: str,
        *,
        nearest: list[str] | None = None,
        events: list[ServerEvent] | None = None,
    ) -> None:
        super().__init__(message)
        self.result: JsonObject = {"error": message}
        if nearest is not None:
            self.result["nearest"] = nearest
        self.events = events if events is not None else []


class _ToolArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)


class _FindSlots(_ToolArgs):
    from_date: CalendarDate
    to_date: CalendarDate
    part_of_day: PartOfDay
    for_booking_ref: BookingRef | Literal[""]


class _FindSlotsWithEvent(_FindSlots):
    event_type: str


class _ListBookings(_ToolArgs):
    email: ContactEmail
    which: Literal["upcoming", "past"]


class _ProposeBooking(_ToolArgs):
    slot_ref: SlotRef
    name: ContactName
    email: ContactEmail
    notes: ContactNotes


class _ProposeCancel(_ToolArgs):
    booking_ref: BookingRef
    reason: _Reason


class _ProposeReschedule(_ProposeCancel):
    slot_ref: SlotRef


_Args = TypeVar("_Args", bound=_ToolArgs)


def _iso_utc(value: str | datetime) -> str:
    return parse_instant(value).isoformat().replace("+00:00", "Z")


def _plain_text(text: str, maximum: int) -> str:
    return " ".join(text.split())[:maximum]


def _valid_email(value: str) -> bool:
    return EMAIL.fullmatch(value) is not None


def _includes_attendee(entry: BookingEntry, email: str) -> bool:
    normalized = email.casefold()
    return any(value.casefold() == normalized for value in entry.attendee_emails)


def _meeting_url(data: Booking) -> str | None:
    for field in ("meetingUrl", "location"):
        value = data.get(field)
        if not isinstance(value, str) or not value or len(value) > 2_048:
            continue
        if any(ord(character) < 32 for character in value):
            continue
        try:
            link = urlsplit(value)
            if link.scheme in {"https", "http"} and link.hostname and not link.username:
                return value
        except ValueError:
            continue
    return None


def booking_entry(data: Booking) -> BookingEntry:
    """Map a Cal.com response to a signed-state booking record."""
    addresses = []
    for person in data.get("attendees") or []:
        address = person.get("email")
        if isinstance(address, str):
            address = address.strip()
            if len(address) <= 254 and _valid_email(address) and address not in addresses:
                addresses.append(address)
    return BookingEntry(
        uid=data["uid"],
        title=_plain_text(data["title"], 512) or "Meeting",
        start_utc=_iso_utc(data["start"]),
        end_utc=_iso_utc(data["end"]),
        status=data["status"],
        attendee_emails=addresses[:32],
        event_type_id=data.get("eventTypeId"),
        meeting_url=_meeting_url(data),
    )


def booking_view(ref: str, entry: BookingEntry, time_zone: str) -> BookingView:
    day, hours = slot_labels(entry.start_utc, entry.end_utc, time_zone)
    view: BookingView = {
        "ref": ref,
        "title": entry.title,
        "whenLabel": f"{day}, {hours}",
        "status": entry.status,
        "attendees": list(entry.attendee_emails),
        "startUtc": entry.start_utc,
        "endUtc": entry.end_utc,
    }
    if entry.meeting_url is not None:
        view["meetingUrl"] = entry.meeting_url
    return view


def slot_view(entry: SlotEntry, time_zone: str) -> SlotView:
    day, hours = slot_labels(entry.start_utc, entry.end_utc, time_zone)
    local_start = parse_instant(entry.start_utc).astimezone(ZoneInfo(time_zone))
    return {"dayLabel": day, "timeLabel": hours, "startLocalIso": local_start.isoformat()}


def nearest_slots(
    entries: Iterable[SlotEntry], hints: Sequence[DateHint], part: PartOfDay, time_zone: str,
) -> list[SlotEntry]:
    """Prefer the requested part of day, then other times on the requested day."""
    if not hints:
        return []
    zone = ZoneInfo(time_zone)

    def distance(local: datetime, hint: DateHint) -> tuple[int, int, bool, timedelta]:
        hours = PART_HOURS.get(part)
        if hours is None and hint.at is not None:
            hours = next((bounds for bounds in PART_HOURS.values() if bounds[0] <= hint.at.hour < bounds[1]), None)
        same_part = hours is None or hours[0] <= local.hour < hours[1]
        days = (local.date() - hint.day).days
        tier = 0 if same_part else 1 if days == 0 else 2
        wall_time = local.replace(tzinfo=None)
        if hint.at is not None:
            offset = abs(wall_time - datetime.combine(local.date(), hint.at))
        elif hours is not None:
            start, end = (datetime.combine(local.date(), time(hour)) for hour in hours)
            offset = max(start - wall_time, wall_time - end, timedelta())
        else:
            offset = timedelta()
        return tier, abs(days), days < 0, offset

    def rank(entry: SlotEntry):
        instant = parse_instant(entry.start_utc)
        local = instant.astimezone(zone)
        return min(distance(local, hint) for hint in hints), instant

    return sorted(entries, key=rank)[:3]


def proposal_view(
    proposal: Proposal, state: SessionState, event_types: Sequence[EventType] = ()
) -> ProposalView:
    booking = state.bookings.get(proposal.booking_ref) if proposal.booking_ref is not None else None
    slot = state.slots.get(proposal.slot_ref) if proposal.slot_ref is not None else None
    selected_time = slot if slot is not None else booking
    if selected_time is None or (proposal.slot_ref is not None and slot is None):
        raise ToolRejected("The proposal time is no longer available in this chat.")
    if proposal.booking_ref is not None and booking is None:
        raise ToolRejected("That booking is no longer available in this chat.")
    if booking is not None:
        title = booking.title
    else:
        title = next(
            (event["title"] for event in event_types if slot is not None and event["id"] == slot.event_type_id),
            "Meeting",
        )
    first, last = parse_instant(selected_time.start_utc), parse_instant(selected_time.end_utc)
    day, hours = slot_labels(first, last, state.time_zone)
    view: ProposalView = {
        "id": proposal.id,
        "kind": proposal.kind,
        "title": title,
        "whenLabel": f"{day}, {hours}",
        "timeZone": state.time_zone,
        "durationMinutes": int((last - first).total_seconds() // 60),
    }
    if proposal.slot_ref is not None:
        view["slotRef"] = proposal.slot_ref
    if proposal.booking_ref is not None:
        view["bookingRef"] = proposal.booking_ref
    if proposal.name is not None:
        view["name"] = proposal.name
    if proposal.email is not None:
        view["email"] = proposal.email
    if proposal.notes is not None:
        view["notes"] = proposal.notes
    return view


def _string(description: str, choices: Sequence[str] = ()) -> JsonObject:
    prop: JsonObject = {"type": "string", "description": description}
    if choices:
        prop["enum"] = list(choices)
    return prop


def _function(name: str, description: str, properties: dict[str, JsonObject]) -> JsonObject:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
        },
    }


def _validate(model: type[_Args], args: object) -> _Args:
    try:
        return model.model_validate(args)
    except ValidationError:
        raise ToolRejected("Invalid tool arguments. Use the required fields and short calendar references.") from None


class CalendarTools:
    def __init__(
        self,
        calendar: CalendarReader,
        state: SessionState,
        now: datetime,
        *,
        allowed_slugs: Iterable[str] = (),
        default_slug: str | None = None,
        deny_mutations: bool = False,
    ) -> None:
        self.calendar = calendar
        self.state = state
        self.now = parse_instant(now)
        self.time_zone = state.time_zone
        self._zone = ZoneInfo(self.time_zone)
        self._allowed_slugs = frozenset(allowed_slugs)
        self._default_slug = default_slug
        self._deny_mutations = deny_mutations
        self._state_lock = Lock()
        self._events_lock = Lock()
        self._event_types: list[EventType] | None = None
        self._default_only_schema = False

    def event_types(self) -> list[EventType]:
        with self._events_lock:
            if self._event_types is None:
                username = self.calendar.me()["username"]
                self._event_types = [
                    event for event in self.calendar.event_types(username)
                    if not event.get("hidden") and (not self._allowed_slugs or event["slug"] in self._allowed_slugs)
                ]
            return list(self._event_types)

    def default_event(self) -> EventType:
        events = self.event_types()
        if not events:
            raise ToolRejected("There are no available event types.")
        if self._default_slug is None:
            return events[0]
        for event in events:
            if event["slug"] == self._default_slug:
                return event
        raise ToolRejected("The configured event type is unavailable.")

    def schemas(self, *, allow_lazy_catalog: bool = False) -> list[JsonObject]:
        # A timed-out availability read may still be loading the catalog.
        catalog = self._event_types
        if allow_lazy_catalog and catalog is None:
            self._default_only_schema = True
            events = []
        else:
            events = self.event_types()
        slots = {
            "from_date": _string("First local day, in YYYY-MM-DD form."),
            "to_date": _string("Last local day, at most seven days after from_date, in YYYY-MM-DD form."),
            "part_of_day": _string("The requested part of the local day.", _PARTS),
            "for_booking_ref": _string("The booking ref to move, or an empty string for a new meeting."),
        }
        if len(events) > 1:
            slots["event_type"] = _string("The event type slug.", [event["slug"] for event in events])
        return [
            _function("find_slots", "Find available calendar slots in the user's time zone.", slots),
            _function("list_bookings", "List the current attendee's bookings.", {
                "email": _string("The current attendee's email, or an empty string to use the session email."),
                "which": _string("Which bookings to list.", ("upcoming", "past")),
            }),
            _function("propose_booking", "Prepare a new booking for the user to confirm.", {
                "slot_ref": _string("A short slot ref returned by find_slots."),
                "name": _string("The attendee's name, or an empty string when unknown."),
                "email": _string("The attendee's email, or an empty string to use the session email."),
                "notes": _string("A user-provided note, or an empty string."),
            }),
            _function("propose_cancel", "Prepare a cancellation for the user to confirm.", {
                "booking_ref": _string("A short booking ref returned by list_bookings."),
                "reason": _string("The user-provided reason, or an empty string."),
            }),
            _function("propose_reschedule", "Prepare to move a booking for the user to confirm.", {
                "booking_ref": _string("A short booking ref returned by list_bookings."),
                "slot_ref": _string("A slot ref returned by find_slots for this booking."),
                "reason": _string("The user-provided reason, or an empty string."),
            }),
        ]

    def execute(self, name: str, args: object) -> ToolOutcome:
        if self._deny_mutations and name in _PROPOSE_TOOLS:
            raise ToolRejected("I can only help with changes to your own bookings.")
        if name == "find_slots":
            model = _FindSlotsWithEvent if isinstance(args, dict) and "event_type" in args else _FindSlots
            return self._find_slots(_validate(model, args))
        if name == "list_bookings":
            return self._list_bookings(_validate(_ListBookings, args))
        if name == "propose_booking":
            return self._propose_booking(_validate(_ProposeBooking, args))
        if name == "propose_cancel":
            return self._propose_cancel(_validate(_ProposeCancel, args))
        if name == "propose_reschedule":
            return self._propose_reschedule(_validate(_ProposeReschedule, args))
        raise ToolRejected("Unknown scheduling tool.")

    def _owned_booking(self, ref: str) -> BookingEntry:
        booking = self.state.bookings.get(ref)
        email = self.state.user.email
        if booking is None or not email or not _includes_attendee(booking, email):
            raise ToolRejected("I can only change bookings that match your attendee email.")
        if booking.status not in _MUTABLE_BOOKING_STATUSES or parse_instant(booking.start_utc) <= self.now:
            raise ToolRejected("Only upcoming bookings can be changed.")
        return booking

    def _resolve_event(self, args: _FindSlots, booking: BookingEntry | None) -> EventType:
        events = self.event_types()
        if isinstance(args, _FindSlotsWithEvent):
            if len(events) <= 1:
                raise ToolRejected("Omit event_type when only one event type is available.")
            event = next((event for event in events if event["slug"] == args.event_type), None)
            if event is None:
                raise ToolRejected("Choose an available event type.")
        elif len(events) > 1 and not self._default_only_schema:
            raise ToolRejected("Choose an available event type.")
        elif self._default_only_schema and booking is not None:
            event = next((event for event in events if event["id"] == booking.event_type_id), None)
            if event is None:
                raise ToolRejected("Choose the event type attached to that booking.")
        else:
            event = self.default_event()
        if booking is not None and booking.event_type_id != event["id"]:
            raise ToolRejected("Choose the event type attached to that booking.")
        return event

    def read_slot_entries(
        self,
        event_type_id: int,
        first: date,
        last: date,
        part: PartOfDay,
        booking_ref: str | None,
        booking_uid: str | None,
        *,
        fresh: bool = False,
    ) -> list[SlotEntry]:
        try:
            begin, end = local_bounds(first, last, self.time_zone)
        except (ValueError, OverflowError):
            raise ToolRejected("Choose a valid calendar date range.") from None
        data = self.calendar.slots(
            event_type_id, begin, end, self.time_zone,
            booking_uid_to_reschedule=booking_uid, fresh=fresh,
        )
        hours = PART_HOURS.get(part)
        matches = {}
        for slots in data.values():
            for slot in slots:
                start = parse_instant(slot["start"])
                local_hour = start.astimezone(self._zone).hour
                if start <= self.now or not begin <= start < end:
                    continue
                if hours is not None and not hours[0] <= local_hour < hours[1]:
                    continue
                matches[start] = SlotEntry(
                    start_utc=_iso_utc(start),
                    end_utc=_iso_utc(slot["end"]),
                    event_type_id=event_type_id,
                    fetched_at=self.now.timestamp(),
                    for_booking_ref=booking_ref,
                )
        return [matches[start] for start in sorted(matches)]

    def register_slots(self, entries: Iterable[SlotEntry], *, reuse_known: bool = False) -> dict[str, SlotView]:
        def key(slot: SlotEntry) -> tuple[datetime, datetime, int, str | None]:
            return (parse_instant(slot.start_utc), parse_instant(slot.end_utc), slot.event_type_id, slot.for_booking_ref)

        fresh = list(entries)
        with self._state_lock:
            known = {key(entry): ref for ref, entry in self.state.slots.items()} if reuse_known else {}
            if known and any(key(entry) not in known for entry in fresh):
                # Reuse refs that can survive even if every incoming entry needs a new ref.
                cutoff = max(0, len(self.state.slots) + len(fresh) - MAX_SLOTS)
                pinned = self.state.proposal.slot_ref if self.state.proposal is not None else None
                evictable = [ref for ref in self.state.slots if ref != pinned]
                threatened = set(evictable[:cutoff])
                known = {key(entry): ref for ref, entry in self.state.slots.items() if ref not in threatened}
            views = {}
            for entry in fresh:
                ref = known.get(key(entry)) if known else None
                if ref is None:
                    ref = add_slot(self.state, entry)
                else:
                    self.state.slots[ref] = entry
                views[ref] = slot_view(entry, self.state.time_zone)
            return views

    def _find_slots(self, args: _FindSlots) -> ToolOutcome:
        first, last = date.fromisoformat(args.from_date), date.fromisoformat(args.to_date)
        if first > last or (last - first).days > 7:
            raise ToolRejected("Choose a date range of seven days or less.")
        booking_ref = args.for_booking_ref or None
        with self._state_lock:
            booking = self._owned_booking(booking_ref) if booking_ref is not None else None
        event = self._resolve_event(args, booking)
        uid = booking.uid if booking is not None else None
        today = self.now.astimezone(self._zone).date()
        entries = self.read_slot_entries(event["id"], max(first, today), last, args.part_of_day, booking_ref, uid) if last >= today else []
        next_open: str | None = None
        if not entries:
            try:
                resume = max(last + timedelta(days=1), today)
                end = resume + timedelta(days=6)
            except OverflowError:
                return ToolOutcome({"slots": [], "timeZone": self.state.time_zone})
            entries = self.read_slot_entries(event["id"], resume, end, args.part_of_day, booking_ref, uid)
            if entries:
                next_open = parse_instant(entries[0].start_utc).astimezone(self._zone).date().isoformat()
                entries = [
                    entry for entry in entries
                    if parse_instant(entry.start_utc).astimezone(self._zone).date().isoformat() == next_open
                ]
        refs = self.register_slots(entries[:_MAX_SLOT_CHOICES])
        result: JsonObject = {
            "slots": [{"ref": ref, **view} for ref, view in refs.items()],
            "timeZone": self.state.time_zone,
            "requestedFrom": args.from_date,
            "requestedTo": args.to_date,
        }
        if next_open is not None:
            result["nextOpenDate"] = next_open
        return ToolOutcome(result, [{"type": "refs", "slots": refs}] if refs else [])

    def _list_bookings(self, args: _ListBookings) -> ToolOutcome:
        with self._state_lock:
            email = self.state.user.email
        requested = args.email.strip()
        if not email or not _valid_email(email):
            raise ToolRejected("Ask for the attendee's email before looking up bookings.")
        if requested and (not _valid_email(requested) or requested.casefold() != email.casefold()):
            raise ToolRejected("I can only look up bookings matching your attendee email.")
        entries, has_more = self.read_booking_entries(email, args.which)
        refs = self.register_bookings(entries)
        with self._state_lock:
            if not refs:
                self.state.rescheduling_booking_ref = None
        result = {
            "bookings": [
                {
                    "ref": ref,
                    "title": _plain_text(EMAIL.sub("[email]", view["title"]), 80),
                    "whenLabel": view["whenLabel"],
                    "status": view["status"],
                }
                for ref, view in refs.items()
            ],
            "which": args.which,
            "hasMore": has_more,
        }
        event: RefsEvent = {"type": "refs", "bookings": refs}
        return ToolOutcome(result, [event])

    def read_booking_entries(self, email: str, status: BookingFilter) -> tuple[list[BookingEntry], bool]:
        page = self.calendar.list_bookings(attendee_email=email, status=status)
        entries = (booking_entry(booking) for booking in page["data"])
        owned = {entry.uid: entry for entry in entries if _includes_attendee(entry, email)}
        return (
            list(owned.values())[:_MAX_BOOKING_CHOICES],
            page["pagination"]["hasMore"] or len(owned) > _MAX_BOOKING_CHOICES,
        )

    def register_bookings(
        self, entries: Iterable[BookingEntry], *, reuse_known: bool = False, preferred_ref: str | None = None,
    ) -> dict[str, BookingView]:
        views: dict[str, BookingView] = {}
        with self._state_lock:
            known = {entry.uid: ref for ref, entry in self.state.bookings.items()} if reuse_known else {}
            if preferred_ref is not None and preferred_ref in self.state.bookings:
                known[self.state.bookings[preferred_ref].uid] = preferred_ref
            for entry in entries:
                ref = known.get(entry.uid)
                if ref is not None and ref in self.state.bookings:
                    self.state.bookings[ref] = entry
                else:
                    ref = add_booking(self.state, entry)
                views[ref] = booking_view(ref, entry, self.state.time_zone)
        return views

    def _check_slot(
        self, ref: str, booking_ref: str | None = None, booking: BookingEntry | None = None
    ) -> SlotEntry:
        with self._state_lock:
            slot = self.state.slots.get(ref)
        if slot is None or parse_instant(slot.start_utc) <= self.now:
            raise ToolRejected("Choose an upcoming slot from the times shown.")
        if slot.for_booking_ref != booking_ref or (
            booking is not None and slot.event_type_id != booking.event_type_id
        ):
            raise ToolRejected("Choose a slot offered for this specific booking request.")
        if self.now.timestamp() - slot.fetched_at <= _STALE_SLOT_SECONDS:
            return slot

        instant = parse_instant(slot.start_utc)
        local = instant.astimezone(self._zone)
        day = local.date()
        today = self.now.astimezone(self._zone).date()
        last_warm_day = today + timedelta(days=WARM_SLOT_DAYS - 1)
        within_warm_window = today <= day <= last_warm_day
        options = self.read_slot_entries(
            slot.event_type_id, today if within_warm_window else day, last_warm_day if within_warm_window else day,
            "any", booking_ref,
            booking.uid if booking is not None else None, fresh=True,
        )
        original_end = parse_instant(slot.end_utc)
        if any(parse_instant(entry.start_utc) == instant and parse_instant(entry.end_utc) == original_end for entry in options):
            with self._state_lock:
                if self.state.slots.get(ref) is not slot:
                    raise ToolRejected("Choose an upcoming slot from the times shown.")
                slot.fetched_at = self.now.timestamp()
            return slot

        if not within_warm_window:
            options = self.read_slot_entries(
                slot.event_type_id, today, last_warm_day, "any", booking_ref,
                booking.uid if booking is not None else None,
            )
        nearest = nearest_slots(
            (entry for entry in options if parse_instant(entry.start_utc) != instant),
            (DateHint(day, local.time()),), "any", self.time_zone,
        )
        refs = self.register_slots(nearest)
        event: RefsEvent = {"type": "refs", "slots": refs}
        raise ToolRejected("That time is no longer available.", nearest=list(refs), events=[event] if refs else [])

    def proposal_view(self, proposal: Proposal) -> ProposalView:
        events = self.event_types() if proposal.kind == "book" else []
        with self._state_lock:
            return proposal_view(proposal, self.state, events)

    def _propose(
        self,
        kind: Literal["book", "cancel", "reschedule"],
        *,
        slot_ref: str | None = None,
        booking_ref: str | None = None,
        name: str | None = None,
        email: str | None = None,
        notes: str | None = None,
        reason: str | None = None,
    ) -> ToolOutcome:
        events = self.event_types() if kind == "book" else []
        with self._state_lock:
            count = self.state.counters.p + 1
            proposal = Proposal(
                id=f"P{count}", kind=kind, slot_ref=slot_ref, booking_ref=booking_ref,
                name=name, email=email, notes=notes, reason=reason, created_at=self.now.timestamp(),
            )
            view = proposal_view(proposal, self.state, events)
            self.state.counters.p = count
            self.state.proposal = proposal
        event: ProposalEvent = {"type": "proposal", "proposal": view}
        return ToolOutcome(events=[event], proposal=proposal)

    def _propose_booking(self, args: _ProposeBooking) -> ToolOutcome:
        self._check_slot(args.slot_ref)
        supplied_email = args.email.strip()
        if supplied_email and not _valid_email(supplied_email):
            raise ToolRejected("Use a valid attendee email or leave it empty for confirmation.")
        with self._state_lock:
            name = args.name.strip() or self.state.user.name
            email = self.state.user.email or supplied_email or None
        if email and not _valid_email(email):
            raise ToolRejected("Use a valid attendee email or leave it empty for confirmation.")
        return self._propose("book", slot_ref=args.slot_ref, name=name, email=email, notes=args.notes.strip() or None)

    def _propose_cancel(self, args: _ProposeCancel) -> ToolOutcome:
        with self._state_lock:
            self._owned_booking(args.booking_ref)
        return self._propose("cancel", booking_ref=args.booking_ref, reason=args.reason.strip() or None)

    def _propose_reschedule(self, args: _ProposeReschedule) -> ToolOutcome:
        with self._state_lock:
            booking = self._owned_booking(args.booking_ref)
        self._check_slot(args.slot_ref, args.booking_ref, booking)
        return self._propose(
            "reschedule", booking_ref=args.booking_ref, slot_ref=args.slot_ref, reason=args.reason.strip() or None
        )
