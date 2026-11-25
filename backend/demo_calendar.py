"""In-memory demo calendar and the shared owner demo booking pattern."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.cal_client import EventType, Profile
from backend.time import local_datetime, parse_instant, utc_now
from evals.fake_cal import FakeCal


DEFAULT_DEMO_TIME_ZONE = "America/Los_Angeles"
_SEED_CUTOFF = time(14)
_DEMO_DURATION = timedelta(minutes=30)
_DEMO_GUESTS = (
    (0, time(15), "Avery Chen"),
    (1, time(10), "Jordan Patel"),
    (1, time(14), "Morgan Rivera"),
    (2, time(15), "Jules Bennett"),
    (2, time(15, 30), "Riley Brooks"),
)


@dataclass(frozen=True)
class DemoBooking:
    start: datetime
    name: str
    email: str

    @property
    def end(self) -> datetime:
        return self.start + _DEMO_DURATION


def _business_day(day: date) -> date:
    return day + timedelta(days=7 - day.weekday()) if day.weekday() >= 5 else day


def demo_bookings(first_day: date, time_zone: str) -> tuple[DemoBooking, ...]:
    """Place the five 30-minute meetings in the calendar owner's local time."""
    days = [_business_day(first_day)]
    for _ in range(2):
        days.append(_business_day(days[-1] + timedelta(days=1)))
    return tuple(
        DemoBooking(
            start=parse_instant(local_datetime(days[offset], at, time_zone)),
            name=name,
            email=f"{name.lower().replace(' ', '.')}@example.com",
        )
        for offset, at, name in _DEMO_GUESTS
    )


class DemoCalendar(FakeCal):
    """Rolling demo availability and account-wide booking reads based on FakeCal."""

    def __init__(
        self, *, clock: Callable[[], datetime] = utc_now, time_zone: str = DEFAULT_DEMO_TIME_ZONE,
    ) -> None:
        try:
            self._host_zone = ZoneInfo(time_zone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("DEMO_TIME_ZONE must be a valid IANA time zone.") from None
        super().__init__(clock=clock)
        local_now = parse_instant(clock()).astimezone(self._host_zone)
        first_day = local_now.date() + timedelta(days=local_now.time() >= _SEED_CUTOFF)
        event = self._event_type(self._event_type_id)
        for index, booking in enumerate(demo_bookings(first_day, self._host_zone.key), start=1):
            uid = f"demo-seed-{index}"
            self._bookings[uid] = {
                "uid": uid,
                "title": event["title"],
                "eventTypeId": event["id"],
                "start": booking.start.isoformat(),
                "end": booking.end.isoformat(),
                "status": "accepted",
                "attendees": [{"name": booking.name, "email": booking.email, "timeZone": self._host_zone.key}],
            }

    def me(self) -> Profile:
        return {**super().me(), "name": "Demo Calendar", "timeZone": self._host_zone.key}

    def event_types(self, username: str) -> list[EventType]:
        events = super().event_types(username)
        for event in events:
            event.pop("bookingUrl", None)
        return events

    def _starts_on(self, day: date) -> Iterator[datetime]:
        if day.weekday() >= 5:
            return
        start = datetime.combine(day, time(9), self._host_zone).astimezone(timezone.utc)
        close = datetime.combine(day, time(17), self._host_zone).astimezone(timezone.utc)
        while start + self._duration <= close:
            yield start
            start += self._duration

    def _scheduled_starts(self, begin: datetime, until: datetime) -> Iterator[datetime]:
        first = begin.astimezone(self._host_zone).date()
        last = until.astimezone(self._host_zone).date()
        for offset in range((last - first).days + 1):
            for start in self._starts_on(first + timedelta(days=offset)):
                if begin <= start < until:
                    yield start

    def _is_scheduled_start(self, start: datetime) -> bool:
        return start in self._starts_on(start.astimezone(self._host_zone).date())
