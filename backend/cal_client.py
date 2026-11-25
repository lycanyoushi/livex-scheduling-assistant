"""Cal.com API v2 client for the scheduling assistant."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from http.client import HTTPException
import json
from threading import Lock
import time
from typing import Any, Literal, NotRequired, TypedDict, TypeVar, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


BOOKINGS_VERSION = "2026-02-25"
BOOKING_LIST_VERSION = "2026-05-01"
EVENT_TYPES_VERSION = "2026-06-12"
SLOTS_VERSION = "2024-09-04"
USER_AGENT = "LiveX-Calendar-Assistant/1.0"
TIMEOUT_SECONDS = 6
PROFILE_TTL = 3600
SLOTS_TTL = 60
CACHE_CAPACITY = 256

ErrorCode = Literal["unavailable", "not_found", "invalid", "rate_limited", "upstream", "slot_taken"]
BookingFilter = Literal["upcoming", "recurring", "past", "cancelled", "unconfirmed"]
Timestamp = str | datetime
SlotBound = str | date | datetime
JsonObject = dict[str, Any]
Transport = Callable[[Request, float], tuple[int, bytes]]
CacheKey = tuple[object, ...]
T = TypeVar("T")


class Profile(TypedDict):
    username: str
    id: NotRequired[int]
    name: NotRequired[str | None]
    email: NotRequired[str | None]
    timeZone: NotRequired[str | None]
    avatarUrl: NotRequired[str | None]


class EventType(TypedDict):
    id: int
    slug: str
    title: str
    lengthInMinutes: int
    hidden: NotRequired[bool]
    bookingUrl: NotRequired[str | None]
    bookingFields: NotRequired[list[JsonObject] | None]


class SlotRange(TypedDict):
    start: str
    end: str


class Booking(TypedDict):
    uid: str
    title: str
    start: str
    end: str
    status: str
    eventTypeId: NotRequired[int | None]
    attendees: NotRequired[list[JsonObject]]
    hosts: NotRequired[list[JsonObject]]
    location: NotRequired[str | None]
    meetingUrl: NotRequired[str | None]


class BookingPagination(TypedDict):
    nextCursor: str | None
    hasMore: bool


class BookingPage(TypedDict):
    status: Literal["success"]
    data: list[Booking]
    pagination: BookingPagination


class CalError(Exception):
    """Failure with a user-safe message and separate raw upstream details."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        status: int | None = None,
        body: object = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.body = body


@dataclass(frozen=True)
class CalObservation:
    """A network attempt or cache hit for the caller's metrics."""

    method: str
    path: str
    status: int | None
    body: object = field(repr=False)
    duration_ms: float
    cache_hit: bool = False


@dataclass(frozen=True)
class _Response:
    status: int
    body: JsonObject

    def parse(self, validator: Callable[[Any], T], *, envelope: bool = False) -> T:
        try:
            return validator(self.body if envelope else self.body["data"])
        except (TypeError, ValueError, KeyError):
            raise CalError(
                "upstream",
                "Cal.com returned an unexpected response.",
                status=self.status,
                body=self.body,
            ) from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Send the API key only to the configured Cal.com origin.
        return None


_OPENER = build_opener(_NoRedirect())


def _send(request: Request, timeout: float) -> tuple[int, bytes]:
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as response:
        with response:
            return response.code, response.read()


def _aware_datetime(value: Timestamp) -> datetime:
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise ValueError("A timestamp with an explicit UTC offset is required.")
    return instant


def _utc_iso(value: Timestamp) -> str:
    return _aware_datetime(value).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _slot_bound(value: SlotBound) -> str:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    return _utc_iso(value)


def _booking_path(uid: str) -> str:
    if not uid or uid in {".", ".."}:
        raise CalError("invalid", "A booking UID is required.")
    return f"/v2/bookings/{quote(uid, safe='')}"


def _object(value: object) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError("Expected an object.")
    return value


def _array(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("Expected an array.")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Expected a nonempty string.")
    return value


def _positive_int(value: object) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("Expected a positive integer.")


def _parse_profile(value: object) -> Profile:
    item = _object(value)
    _string(item["username"])
    if "id" in item:
        _positive_int(item["id"])
    for field in ("name", "email", "timeZone", "avatarUrl"):
        if item.get(field) is not None and not isinstance(item[field], str):
            raise ValueError("Expected profile text.")
    return cast(Profile, item)


def _parse_event_types(value: object) -> list[EventType]:
    visible = []
    for entry in _array(value):
        item = _object(entry)
        if not isinstance(item.get("hidden", False), bool):
            raise ValueError("Expected an event visibility flag.")
        if item.get("hidden"):
            continue
        _positive_int(item["id"])
        _positive_int(item["lengthInMinutes"])
        _string(item["slug"])
        _string(item["title"])
        if item.get("bookingUrl") is not None:
            _string(item["bookingUrl"])
        if item.get("bookingFields") is not None:
            for field in _array(item["bookingFields"]):
                _object(field)
        visible.append(cast(EventType, item))
    return visible


def _check_interval(item: JsonObject) -> None:
    start = _aware_datetime(_string(item["start"]))
    end = _aware_datetime(_string(item["end"]))
    if start >= end:
        raise ValueError("Expected an interval with a positive duration.")


def _parse_slots(value: object) -> dict[str, list[SlotRange]]:
    slots = _object(value)
    for day, entries in slots.items():
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("Expected an ISO slot date.")
        for entry in _array(entries):
            _check_interval(_object(entry))
    return cast(dict[str, list[SlotRange]], slots)


def _parse_booking(value: object) -> Booking:
    item = _object(value)
    for field in ("uid", "title", "status"):
        _string(item[field])
    _check_interval(item)
    if item.get("eventTypeId") is not None:
        _positive_int(item["eventTypeId"])
    for field in ("hosts", "attendees"):
        if field in item:
            for person in _array(item[field]):
                person = _object(person)
                if person.get("email") is not None:
                    _string(person["email"])
    for field in ("location", "meetingUrl"):
        if item.get(field) is not None and not isinstance(item[field], str):
            raise ValueError("Expected booking location text.")
    return cast(Booking, item)


def _parse_booking_page(value: object) -> BookingPage:
    envelope = _object(value)
    for item in _array(envelope["data"]):
        _parse_booking(item)
    pagination = _object(envelope["pagination"])
    has_more = pagination["hasMore"]
    if not isinstance(has_more, bool):
        raise ValueError("Expected a pagination flag.")
    cursor = pagination["nextCursor"]
    if has_more:
        _string(cursor)
    elif cursor is not None:
        raise ValueError("Expected a null cursor on the last page.")
    return cast(BookingPage, envelope)


def _decode_body(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except (UnicodeError, ValueError):
        return raw.decode("utf-8", errors="replace")


def _http_error(status: int, body: object) -> CalError:
    if status == 404:
        return CalError("not_found", "Cal.com could not find the requested item.", status=status, body=body)
    if status == 429:
        return CalError("rate_limited", "Cal.com is limiting requests.", status=status, body=body)
    if 400 <= status < 500:
        return CalError("invalid", "Cal.com rejected the request.", status=status, body=body)
    return CalError("upstream", "Cal.com returned an unexpected response.", status=status, body=body)


class CalClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.cal.com",
        *,
        observer: Callable[[CalObservation], None] | None = None,
        transport: Transport = _send,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key.strip():
            raise ValueError("A Cal.com API key is required.")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key.strip()
        self._observer = observer
        self._transport = transport
        self._clock = clock
        self._cache: dict[CacheKey, tuple[float, object]] = {}
        self._lock = Lock()
        self._slots_generation = 0

    def _observe(
        self, method: str, path: str, began: float, status: int | None, body: object, *, cache_hit: bool = False,
    ) -> None:
        if self._observer is not None:
            self._observer(CalObservation(method, path, status, body, (self._clock() - began) * 1000, cache_hit))

    def _request(
        self,
        method: str,
        path: str,
        version: str | None = None,
        *,
        query: Mapping[str, str | int | None] | None = None,
        payload: JsonObject | None = None,
        retry_read: bool = True,
    ) -> _Response:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if version is not None:
            headers["cal-api-version"] = version
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        suffix = urlencode({key: value for key, value in (query or {}).items() if value is not None})
        url = f"{self._base_url}{path}" + (f"?{suffix}" if suffix else "")
        request = Request(url, method=method, data=data, headers=headers)

        retry_read = method == "GET" and retry_read
        for attempt in range(2 if retry_read else 1):
            can_retry = retry_read and attempt == 0
            began = self._clock()
            try:
                status, raw = self._transport(request, TIMEOUT_SECONDS)
            except (URLError, OSError, HTTPException):
                self._observe(method, path, began, None, None)
                if can_retry:
                    continue
                raise CalError("unavailable", "Cal.com could not be reached.") from None

            body = _decode_body(raw)
            self._observe(method, path, began, status, body)
            if 500 <= status < 600 and can_retry:
                continue
            if not 200 <= status < 300:
                raise _http_error(status, body)
            if not isinstance(body, dict) or body.get("status") != "success" or "data" not in body:
                raise CalError(
                    "upstream", "Cal.com returned an unexpected response.", status=status, body=body
                )
            return _Response(status, body)
        raise AssertionError("No Cal.com request was attempted.")

    def _cached(
        self, key: CacheKey, ttl: float, fetch: Callable[[], T], *, path: str, fresh: bool = False,
    ) -> T:
        began = self._clock()
        with self._lock:
            generation = self._slots_generation
            entry = self._cache.get(key)
            hit = not fresh and entry is not None and entry[0] > began
        if hit and entry is not None:
            result = cast(T, deepcopy(entry[1]))
            self._observe("GET", path, began, 200, None, cache_hit=True)
            return result
        value = fetch()
        with self._lock:
            if key[0] != "slots" or generation == self._slots_generation:
                if len(self._cache) >= CACHE_CAPACITY and key not in self._cache:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[key] = (self._clock() + ttl, deepcopy(value))
        return value

    def _write(self, path: str, payload: JsonObject) -> Booking:
        try:
            return self._request("POST", path, BOOKINGS_VERSION, payload=payload).parse(_parse_booking)
        finally:
            # A host calendar change can affect multiple event types and date windows.
            with self._lock:
                self._slots_generation += 1
                self._cache = {key: value for key, value in self._cache.items() if key[0] != "slots"}

    def me(self, *, retry: bool = True) -> Profile:
        return self._cached(
            ("me",), PROFILE_TTL,
            lambda: self._request("GET", "/v2/me", retry_read=retry).parse(_parse_profile), path="/v2/me",
        )

    def event_types(self, username: str) -> list[EventType]:
        return self._cached(
            ("event_types", username),
            PROFILE_TTL,
            lambda: self._request(
                "GET", "/v2/event-types", EVENT_TYPES_VERSION, query={"username": username}
            ).parse(_parse_event_types),
            path="/v2/event-types",
        )

    def slots(
        self,
        event_type_id: int,
        start: SlotBound,
        end: SlotBound,
        time_zone: str = "UTC",
        *,
        booking_uid_to_reschedule: str | None = None,
        fresh: bool = False,
    ) -> dict[str, list[SlotRange]]:
        start_utc, end_utc = _slot_bound(start), _slot_bound(end)
        key = ("slots", event_type_id, start_utc, end_utc, time_zone, booking_uid_to_reschedule)
        query = {
            "eventTypeId": event_type_id,
            "start": start_utc,
            "end": end_utc,
            "timeZone": time_zone,
            "format": "range",
            "bookingUidToReschedule": booking_uid_to_reschedule,
        }
        return self._cached(
            key,
            SLOTS_TTL,
            lambda: self._request("GET", "/v2/slots", SLOTS_VERSION, query=query).parse(_parse_slots),
            path="/v2/slots",
            fresh=fresh,
        )

    def list_bookings(
        self,
        *,
        attendee_email: str | None = None,
        status: BookingFilter | None = "upcoming",
        after_start: Timestamp | None = None,
        before_end: Timestamp | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> BookingPage:
        """List bookings with optional inclusive start and end bounds."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CalError("invalid", "Booking page size must be between 1 and 100.")
        try:
            lower = _utc_iso(after_start) if after_start is not None else None
            upper = _utc_iso(before_end) if before_end is not None else None
        except (OverflowError, ValueError):
            raise CalError("invalid", "Booking date bounds must have an explicit UTC offset.") from None
        if lower is not None and upper is not None and _aware_datetime(lower) >= _aware_datetime(upper):
            raise CalError("invalid", "The booking start bound must be earlier than the end bound.")
        query = {
            "attendeeEmail": attendee_email,
            "status": status,
            "afterStart": lower,
            "beforeEnd": upper,
            "sortStart": "asc",
            "limit": limit,
            "cursor": cursor,
        }
        return self._request("GET", "/v2/bookings", BOOKING_LIST_VERSION, query=query).parse(
            _parse_booking_page, envelope=True
        )

    def booking(self, uid: str) -> Booking:
        return self._request("GET", _booking_path(uid), BOOKINGS_VERSION).parse(_parse_booking)

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
    ) -> Booking:
        start_utc = _utc_iso(start)
        payload = {
            "start": start_utc,
            "eventTypeId": event_type_id,
            "attendee": {"name": name, "email": email, "timeZone": time_zone, "language": "en"},
            "metadata": {"source": "chat-assistant"},
        }
        if notes and notes_field:
            payload["bookingFieldsResponses"] = {notes_field: notes}
        try:
            return self._write("/v2/bookings", payload)
        except CalError as error:
            # Only ambiguous validation/conflict responses warrant a fresh availability check.
            if error.status in {400, 409, 422} and self._slot_is_gone(event_type_id, start_utc):
                raise CalError(
                    "slot_taken", "That time is no longer available.", status=error.status, body=error.body
                ) from error
            raise

    def _slot_is_gone(self, event_type_id: int, start_utc: str) -> bool:
        instant = _aware_datetime(start_utc)
        day = instant.date()
        try:
            days = self.slots(event_type_id, day, day, fresh=True)
        except CalError:
            return False
        return all(_aware_datetime(slot["start"]) != instant for slots in days.values() for slot in slots)

    def cancel_booking(self, uid: str, reason: str = "User requested cancellation") -> Booking:
        return self._write(f"{_booking_path(uid)}/cancel", {"cancellationReason": reason})

    def reschedule_booking(
        self, uid: str, start: Timestamp, reason: str = "User requested reschedule"
    ) -> Booking:
        payload = {"start": _utc_iso(start), "reschedulingReason": reason}
        return self._write(f"{_booking_path(uid)}/reschedule", payload)
