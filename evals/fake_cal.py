"""Deterministic, in-memory Cal.com fixture for one evaluation case."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from itertools import count
import json
from pathlib import Path
from threading import Lock
from typing import TypedDict
from zoneinfo import ZoneInfo

from backend.cal_client import (
    Booking,
    BookingFilter,
    BookingPage,
    CalError,
    EventType,
    JsonObject,
    Profile,
    SlotBound,
    SlotRange,
    Timestamp,
    event_lengths,
)


FIXTURE_PATH = Path(__file__).with_suffix(".json")
DEFAULT_CLOCK = datetime.fromisoformat("2026-11-23T10:00:00+08:00")


class _StoredBooking(Booking, total=False):
    bookingFieldsResponses: dict[str, str]
    cancellationReason: str
    reschedulingReason: str
    rescheduledFromUid: str
    rescheduledToUid: str


def _as_utc(value: Timestamp) -> datetime:
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    if instant.utcoffset() is None:
        raise ValueError("A timestamp with an explicit UTC offset is required.")
    return instant.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _as_local_start(value: str, zone: ZoneInfo) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=zone).astimezone(timezone.utc)


def _slot_edge(value: SlotBound, *, end: bool = False) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError:
            return _as_utc(value)
    day = value + timedelta(days=1) if end else value
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


class FakeCal:
    """Load fixed calendar data and isolate mutations within this instance."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        fixture_overrides: Mapping[str, object] | None = None,
        time_zone: str = "Asia/Singapore",
        fixture_path: Path | None = None,
    ) -> None:
        fixture = json.loads((fixture_path or FIXTURE_PATH).read_text(encoding="utf-8"))
        if fixture["schema_version"] != 1:
            raise ValueError("Unsupported fake calendar schema version.")
        overrides: JsonObject = dict(fixture_overrides or {})
        self._clock = clock or (lambda: DEFAULT_CLOCK)
        self._profile: Profile = fixture["profile"]
        self._event_types: list[EventType] = fixture["event_types"]
        self._bookings: dict[str, _StoredBooking] = {}
        for key in overrides.get("active_bookings", []):
            booking = fixture["bookings"][key]
            self._bookings[booking["uid"]] = booking

        availability = fixture["availability"]
        self._event_type_id: int = availability["event_type_id"]
        self._duration = timedelta(minutes=availability["duration_minutes"])
        self._blocking_statuses = set(availability["occupancy"]["blocking_statuses"])
        host_zone = ZoneInfo(availability["time_zone"])
        self._starts = {
            _as_local_start(f"{day}T{start}", host_zone)
            for day, starts in availability["local_starts_by_date"].items()
            for start in starts
        }
        case_zone = ZoneInfo(time_zone)
        if "open_local_starts" in overrides:
            requested = {_as_local_start(start, case_zone) for start in overrides["open_local_starts"]}
            if not requested <= self._starts:
                raise ValueError("The requested fixture slots must exist in the calendar.")
            self._starts = requested
        self._starts_in_order = sorted(self._starts)

        self._create_faults: dict[datetime, JsonObject] = {}
        for entry in overrides.get("calendar_faults", []):
            fault = fixture["faults"][entry["name"]]
            if (
                fault["operation"] != "create_booking"
                or fault["trigger"] != "first_matching_attempt"
                or fault["effect"] != "occupy_requested_slot"
            ):
                raise ValueError("Unsupported fake calendar fault.")
            start = _as_local_start(entry["local_start"], case_zone)
            if start not in self._starts:
                raise ValueError("A calendar fault must target a fixture slot.")
            self._create_faults[start] = fault["error"]

        self._occupied: set[datetime] = set()
        self._ids = count(1)
        self._lock = Lock()

    def me(self) -> Profile:
        return deepcopy(self._profile)

    def event_types(self, username: str) -> list[EventType]:
        if username != self._profile["username"]:
            return []
        return deepcopy([event for event in self._event_types if not event.get("hidden")])

    def _event_type(self, event_type_id: int) -> EventType:
        for event in self._event_types:
            if event["id"] == event_type_id == self._event_type_id:
                return event
        raise CalError("not_found", "Cal.com could not find the requested event type.", status=404)

    def _length(self, event: EventType, minutes: int | None) -> timedelta:
        """Match Cal.com: a length must be one the event type offers."""
        if minutes is None:
            return self._duration
        if minutes not in event_lengths(event):
            raise CalError("invalid", "That meeting length is not offered by this event type.", status=400)
        return timedelta(minutes=minutes)

    def _slot_blocked(self, start: datetime, uid_to_ignore: str | None, length: timedelta | None = None) -> bool:
        end = start + (length or self._duration)
        for booking in self._bookings.values():
            if booking["uid"] == uid_to_ignore or booking["status"] not in self._blocking_statuses:
                continue
            if start < _as_utc(booking["end"]) and end > _as_utc(booking["start"]):
                return True
        return any(start < occupied + self._duration and end > occupied for occupied in self._occupied)

    def _scheduled_starts(self, begin: datetime, until: datetime) -> Iterable[datetime]:
        return (start for start in self._starts_in_order if begin <= start < until)

    def _is_scheduled_start(self, start: datetime) -> bool:
        return start in self._starts

    def _fits(self, start: datetime, length: timedelta) -> bool:
        """Whether a meeting of this length fits the host's working hours."""
        return True

    def slots(
        self,
        event_type_id: int,
        start: SlotBound,
        end: SlotBound,
        time_zone: str = "UTC",
        *,
        booking_uid_to_reschedule: str | None = None,
        fresh: bool = False,
        duration: int | None = None,
    ) -> dict[str, list[SlotRange]]:
        """Read the current fixture availability in the requested display zone."""
        length = self._length(self._event_type(event_type_id), duration)
        begin, until = _slot_edge(start), _slot_edge(end, end=True)
        zone = ZoneInfo(time_zone)
        now = _as_utc(self._clock())
        days: dict[str, list[SlotRange]] = {}
        with self._lock:
            for instant in self._scheduled_starts(begin, until):
                if instant <= now or not self._fits(instant, length):
                    continue
                if self._slot_blocked(instant, booking_uid_to_reschedule, length):
                    continue
                local_start = instant.astimezone(zone)
                local_end = (instant + length).astimezone(zone)
                days.setdefault(local_start.date().isoformat(), []).append(
                    {"start": _iso(local_start), "end": _iso(local_end)}
                )
        return days

    def _has_status(self, booking: Booking, status: BookingFilter | None, now: datetime) -> bool:
        if status is None:
            return True
        if status == "unconfirmed":
            return booking["status"] == "pending"
        if status == "cancelled":
            return booking["status"] == "cancelled"
        if booking["status"] not in self._blocking_statuses:
            return False
        if status == "upcoming":
            return _as_utc(booking["end"]) > now
        if status == "past":
            return _as_utc(booking["end"]) <= now
        return False

    def list_bookings(
        self,
        *,
        attendee_email: str | None = None,
        status: BookingFilter | None = "upcoming",
        limit: int = 20,
        cursor: str | None = None,
        after_start: Timestamp | None = None,
        before_end: Timestamp | None = None,
    ) -> BookingPage:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CalError("invalid", "Booking page size must be between 1 and 100.")
        offset = 0
        if cursor is not None:
            digits = cursor.removeprefix("fixture-page:")
            if digits == cursor or not digits.isascii() or not digits.isdigit() or len(digits) > 10:
                raise CalError("invalid", "Invalid fake calendar booking cursor.", status=400)
            offset = int(digits)

        try:
            lower = _as_utc(after_start) if after_start is not None else None
            upper = _as_utc(before_end) if before_end is not None else None
        except (TypeError, ValueError, OverflowError):
            raise CalError("invalid", "Booking bounds must have explicit UTC offsets.", status=400) from None
        if lower is not None and upper is not None and lower > upper:
            raise CalError("invalid", "Booking bounds are reversed.", status=400)
        email = attendee_email.casefold() if attendee_email is not None else None
        now = _as_utc(self._clock())
        with self._lock:
            matches = [
                booking for booking in self._bookings.values()
                if self._has_status(booking, status, now)
                and (lower is None or _as_utc(booking["start"]) >= lower)
                and (upper is None or _as_utc(booking["end"]) <= upper)
                and (
                    email is None
                    or any(attendee.get("email", "").casefold() == email for attendee in booking.get("attendees", []))
                )
            ]
            matches.sort(key=lambda booking: (_as_utc(booking["start"]), booking["uid"]))
            next_offset = offset + limit
            has_more = next_offset < len(matches)
            return {
                "status": "success",
                "data": deepcopy(matches[offset:next_offset]),
                "pagination": {
                    "hasMore": has_more,
                    "nextCursor": f"fixture-page:{next_offset}" if has_more else None,
                },
            }

    def _booking(self, uid: str) -> _StoredBooking:
        try:
            return self._bookings[uid]
        except KeyError:
            raise CalError("not_found", "Cal.com could not find the requested booking.", status=404) from None

    def booking(self, uid: str) -> Booking:
        with self._lock:
            return deepcopy(self._booking(uid))

    def _require_free(
        self, start: datetime, now: datetime, uid_to_ignore: str | None = None, length: timedelta | None = None,
    ) -> None:
        length = length or self._duration
        if start <= now or not self._is_scheduled_start(start) or not self._fits(start, length):
            raise CalError("invalid", "That time is outside the fixture availability.", status=400)
        if self._slot_blocked(start, uid_to_ignore, length):
            raise CalError("slot_taken", "That time is no longer available.", status=409)

    def create_booking(
        self,
        *,
        start: Timestamp,
        event_type_id: int,
        name: str,
        email: str,
        time_zone: str,
        notes: str | None = None,
        notes_field: str | None = None,
        length_in_minutes: int | None = None,
    ) -> Booking:
        event = self._event_type(event_type_id)
        length = self._length(event, length_in_minutes)
        instant = _as_utc(start)
        now = _as_utc(self._clock())
        with self._lock:
            self._require_free(instant, now, length=length)
            fault = self._create_faults.pop(instant, None)
            if fault is not None:
                self._occupied.add(instant)
                raise CalError(fault["code"], "That time is no longer available.", status=fault["status"])
            uid = f"fixture-created-{next(self._ids)}"
            booking: _StoredBooking = {
                "uid": uid,
                "title": event["title"],
                "eventTypeId": event_type_id,
                "start": _iso(instant),
                "end": _iso(instant + length),
                "status": "accepted",
                "attendees": [{"name": name, "email": email, "timeZone": time_zone, "language": "en"}],
            }
            if notes and notes_field:
                booking["bookingFieldsResponses"] = {notes_field: notes}
            self._bookings[uid] = booking
            return deepcopy(booking)

    def cancel_booking(self, uid: str, reason: str = "User requested cancellation") -> Booking:
        with self._lock:
            booking = self._booking(uid)
            booking["status"] = "cancelled"
            booking["cancellationReason"] = reason
            return deepcopy(booking)

    def reschedule_booking(
        self, uid: str, start: Timestamp, reason: str = "User requested reschedule"
    ) -> Booking:
        instant = _as_utc(start)
        now = _as_utc(self._clock())
        with self._lock:
            original = self._booking(uid)
            if original["status"] not in self._blocking_statuses:
                raise CalError("invalid", "The booking cannot be rescheduled.", status=400)
            # Like Cal.com, a move keeps the booking's original length.
            length = _as_utc(original["end"]) - _as_utc(original["start"])
            self._require_free(instant, now, uid, length)
            new_uid = f"fixture-created-{next(self._ids)}"
            replacement: _StoredBooking = {
                **original,
                "uid": new_uid,
                "start": _iso(instant),
                "end": _iso(instant + length),
                "rescheduledFromUid": uid,
                "reschedulingReason": reason,
            }
            original["status"] = "cancelled"
            original["rescheduledToUid"] = new_uid
            self._bookings[new_uid] = replacement
            return deepcopy(replacement)
