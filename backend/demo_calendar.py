"""In-memory demo calendar with weekday openings relative to the live clock."""

from collections.abc import Callable, Iterator
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from backend.cal_client import EventType, Profile
from backend.time import utc_now
from evals.fake_cal import FakeCal


_HOST_ZONE = ZoneInfo("America/Los_Angeles")


class DemoCalendar(FakeCal):
    """Rolling demo availability and account-wide booking reads based on FakeCal."""

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        super().__init__(clock=clock)

    def me(self) -> Profile:
        return {**super().me(), "name": "Demo Calendar", "timeZone": _HOST_ZONE.key}

    def event_types(self, username: str) -> list[EventType]:
        events = super().event_types(username)
        for event in events:
            event.pop("bookingUrl", None)
        return events

    def _starts_on(self, day: date) -> Iterator[datetime]:
        if day.weekday() >= 5:
            return
        start = datetime.combine(day, time(9), _HOST_ZONE).astimezone(timezone.utc)
        close = datetime.combine(day, time(17), _HOST_ZONE).astimezone(timezone.utc)
        while start + self._duration <= close:
            yield start
            start += self._duration

    def _scheduled_starts(self, begin: datetime, until: datetime) -> Iterator[datetime]:
        first, last = begin.astimezone(_HOST_ZONE).date(), until.astimezone(_HOST_ZONE).date()
        for offset in range((last - first).days + 1):
            for start in self._starts_on(first + timedelta(days=offset)):
                if begin <= start < until:
                    yield start

    def _is_scheduled_start(self, start: datetime) -> bool:
        return start in self._starts_on(start.astimezone(_HOST_ZONE).date())
