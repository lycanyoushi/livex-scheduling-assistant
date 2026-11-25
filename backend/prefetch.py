"""Fetch optional calendar context without mutating a conversation in a worker."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import re
from threading import BoundedSemaphore
from time import perf_counter
from typing import cast
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from backend.cal_client import BookingFilter, CalError
from backend.prescan import PART_HOURS, DateHint, PartOfDay, PrescanResult, TimeWindow
from backend.protocol import CalCallMetrics
from backend.state import BookingEntry, SessionState, SlotEntry
from backend.time import local_bounds, parse_instant
from backend.tools import ClosedWindow, WARM_SLOT_DAYS, CalendarTools, ToolRejected, is_daytime, last_bookable_day, nearest_slots, preferred_daytime_slots


PREFETCH_BUDGET_SECONDS = 1.2
_MAX_HINT_DAYS = 7
_MAX_DATED_SLOTS = 16
_UNDATED_SLOTS = 6
_CONSTRAINED_WEEK_SLOTS = 4
_WEEK_SCOPE = re.compile(r"\b(this|next)\s+week\b", re.I)
_PAST_SCOPE = re.compile(r"\b(?:past|previous|earlier|history|last (?:week|month|year)|pasad[oa]s?|anteriores?)\b", re.I)
_CANCELLED_SCOPE = re.compile(r"\b(?:cancelled|canceled)\b", re.I)


def warm_slot_bounds(now: datetime, time_zone: str) -> tuple[datetime, datetime]:
    """Return the half-open UTC window for the next 14 local dates."""
    return local_bounds(*_warm_dates(now, ZoneInfo(time_zone)), time_zone)


def _warm_dates(now: datetime, zone: ZoneInfo) -> tuple[date, date]:
    today = parse_instant(now).astimezone(zone).date()
    return today, today + timedelta(days=WARM_SLOT_DAYS - 1)


@dataclass(frozen=True)
class RescheduleTarget:
    ref: str
    uid: str
    event_type_id: int
    start: datetime
    length_minutes: int | None = None


@dataclass(frozen=True)
class BookingLookup:
    status: BookingFilter | None
    first: date | None = None
    last: date | None = None
    at: time | None = None


@dataclass(frozen=True)
class PrefetchPlan:
    days: tuple[date, ...]
    times: tuple[DateHint, ...]
    part_of_day: PartOfDay
    spread_slots: bool
    booking_lookup: BookingLookup | None
    reschedule: RescheduleTarget | None
    show_openings: bool = False
    excluded_days: frozenset[date] = frozenset()
    excluded_weekdays: frozenset[int] = frozenset()
    exclusion_unresolved: bool = False
    prefer_daytime: bool = False
    move_requested: bool = False
    move_after: time | None = None
    move_later: bool = False
    time_window: TimeWindow | None = None

    def excludes(self, day: date) -> bool:
        return day in self.excluded_days or day.weekday() in self.excluded_weekdays

    def status(self) -> str:
        if self.booking_lookup is not None:
            return "Checking your calendar…"
        if self.days:
            if len(self.days) == 1:
                day = self.days[0]
                return f"Checking availability for {day:%A} {day.day} {day:%b}…"
            return "Checking availability…"
        return "Checking the schedule…"


def prefer_daytime_for_request(state: SessionState, hints: PrescanResult) -> bool:
    return (
        not hints.dates and not hints.exclusion_unresolved
        and not {"list", "cancel", "reschedule"}.intersection(hints.intents)
        and state.rescheduling_booking_ref is None
        and (state.proposal is None or state.proposal.kind == "book")
    )


def plan_prefetch(state: SessionState, now: datetime, hints: PrescanResult, *, text: str = "") -> PrefetchPlan:
    if hints.needs_interpretation:
        return PrefetchPlan(
            (), (), "any", False, None, None,
            excluded_days=hints.excluded_days,
            excluded_weekdays=hints.excluded_weekdays,
            exclusion_unresolved=hints.exclusion_unresolved,
            move_requested="reschedule" in hints.intents,
        )
    instant = parse_instant(now)
    today = instant.astimezone(ZoneInfo(state.time_zone)).date()
    intents = set(hints.intents)
    days = () if hints.exclusion_unresolved else tuple(sorted({
        hint.day for hint in hints.dates if today <= hint.day <= last_bookable_day(today) and not hints.excludes(hint.day)
    })[:_MAX_HINT_DAYS])
    spread = (not hints.exclusion_unresolved and not hints.dates and not hints.excluded_days
              and not hints.excluded_weekdays and hints.availability_request)
    if spread:
        days = tuple(today + timedelta(days=offset) for offset in range(_MAX_HINT_DAYS))
    lookup = None
    if intents & {"list", "cancel", "reschedule"}:
        change = bool(intents & {"cancel", "reschedule"})
        meeting = hints.existing_meeting
        scope = sorted({hint.day for hint in hints.dates}) if "reschedule" not in intents else []
        if meeting is not None:
            scope = [meeting.day]
        elif "reschedule" not in intents and (week := _WEEK_SCOPE.search(text)):
            first = today - timedelta(days=today.weekday()) + timedelta(days=7 if week[1].lower() == "next" else 0)
            scope = [first, first + timedelta(days=6)]
        status: BookingFilter | None = "upcoming"
        if not change:
            if _CANCELLED_SCOPE.search(text):
                status = "cancelled"
            elif _PAST_SCOPE.search(text):
                status = "past"
            elif scope:
                status = None
        lookup = BookingLookup(status, scope[0] if scope else None, scope[-1] if scope else None, meeting.at if meeting else None)
    if lookup is not None and "reschedule" not in intents and not hints.availability_request:
        days = ()
    has_pending_change = state.rescheduling_booking_ref is not None or (
        state.proposal is not None and state.proposal.kind in {"cancel", "reschedule"}
    )
    return PrefetchPlan(
        days, hints.dates, hints.part_of_day, spread, lookup, None,
        show_openings=bool(days and hints.availability_request and not has_pending_change),
        excluded_days=hints.excluded_days, excluded_weekdays=hints.excluded_weekdays,
        exclusion_unresolved=hints.exclusion_unresolved,
        prefer_daytime=prefer_daytime_for_request(state, hints),
        move_requested="reschedule" in intents, move_after=hints.move_after, move_later=hints.move_later,
        time_window=hints.time_window,
    )


def _date_ranges(days: tuple[date, ...]) -> list[tuple[date, date]]:
    """Read one range for each contiguous run of requested dates."""
    ranges: list[tuple[date, date]] = []
    for day in days:
        if ranges and (day - ranges[-1][1]).days == 1:
            ranges[-1] = (ranges[-1][0], day)
        else:
            ranges.append((day, day))
    return ranges


def _spread_slots(day_slots: dict[date, list[SlotEntry]]) -> list[SlotEntry]:
    ordered_days = list(day_slots.values())
    first_per_day = [entries[0] for entries in ordered_days[:3]]
    first_starts = {entry.start_utc for entry in first_per_day}
    remaining = [
        entry for entries in ordered_days for entry in entries if entry.start_utc not in first_starts
    ]
    return sorted(
        [*first_per_day, *remaining[:_UNDATED_SLOTS - len(first_per_day)]],
        key=lambda entry: entry.start_utc,
    )


@dataclass(frozen=True)
class _SlotResults:
    slots: list[SlotEntry] = field(default_factory=list)
    alternatives: list[SlotEntry] = field(default_factory=list)
    unavailable_event: str | None = None
    shorter_options: bool = False
    closed_window: ClosedWindow | None = None


def _prefetch_slots(tools: CalendarTools, plan: PrefetchPlan) -> _SlotResults:
    target = plan.reschedule
    window = plan.time_window
    shorter_options = False
    if target is not None:
        event_id = target.event_type_id
        event = next((item for item in tools.event_types() if item["id"] == event_id), None)
        if event is None:
            return _SlotResults()
        # A move keeps the meeting's own length.
        minutes = target.length_minutes
        duration = tools.query_duration(event_id, target.length_minutes)
    else:
        draft = tools.state.guest_draft
        minutes = draft.duration_minutes if draft else None
        try:
            event = tools.event_for_duration(minutes)
        except ToolRejected as error:
            return _SlotResults(unavailable_event=str(error))
        if window is not None and window.full_meeting:
            try:
                event = tools.event_for_duration(window.minutes)
                minutes = window.minutes
            except ToolRejected:
                shorter_options = True
        event_id = event["id"]
        duration = tools.query_duration(event_id, minutes)
    zone = ZoneInfo(tools.time_zone)
    first_warm_date, last_warm_date = _warm_dates(tools.now, zone)
    other_days = plan.days
    ranges = []
    if target is None:
        if any(first_warm_date <= day <= last_warm_date for day in plan.days):
            ranges.append((first_warm_date, last_warm_date))
        other_days = tuple(day for day in plan.days if not first_warm_date <= day <= last_warm_date)
    ranges.extend(_date_ranges(other_days))
    wanted_days = set(plan.days)
    day_slots: dict[date, list[SlotEntry]] = defaultdict(list)
    daytime_slots: dict[date, list[SlotEntry]] = defaultdict(list)
    warm_entries = None
    window_day_entries = []
    offer_alternatives = plan.show_openings and target is None and window is None
    hours = PART_HOURS.get(plan.part_of_day)
    for first, last in ranges:
        is_warm_range = (first, last) == (first_warm_date, last_warm_date)
        entries = tools.read_slot_entries(
            event_id, first, last, "any" if window is not None or (offer_alternatives and is_warm_range) else plan.part_of_day,
            target.ref if target is not None else None,
            target.uid if target is not None else None,
            duration=duration,
        )
        if offer_alternatives and is_warm_range:
            warm_entries = entries
        for entry in entries:
            instant = parse_instant(entry.start_utc)
            local = instant.astimezone(zone)
            if window is not None and local.date() in wanted_days:
                window_day_entries.append(entry)
            if window is not None:
                end = parse_instant(entry.end_utc)
                if (not window.contains(local, end.astimezone(zone))
                        or end - instant != timedelta(minutes=minutes or event["lengthInMinutes"])):
                    continue
            if local.date() in wanted_days and (hours is None or hours[0] <= local.hour < hours[1]):
                if target is not None and (
                    instant == target.start
                    or (plan.move_later and instant <= target.start)
                    or (plan.move_after is not None and local.time() <= plan.move_after)
                ):
                    continue
                day_slots[local.date()].append(entry)
                if plan.prefer_daytime and is_daytime(local):
                    daytime_slots[local.date()].append(entry)

    day_slots = daytime_slots or day_slots
    if plan.show_openings and window is not None and not day_slots and not shorter_options:
        closed = tools.read_closed_window(
            event_id, min(wanted_days), max(wanted_days), window.start, window.end, window_day_entries,
            minutes=minutes or event["lengthInMinutes"], duration=duration, zone=zone,
            booking_ref=target.ref if target is not None else None, booking_uid=target.uid if target is not None else None,
            plan_whole_meeting=window.full_meeting,
        )
        return _SlotResults(alternatives=closed.alternatives, closed_window=closed)
    if offer_alternatives:
        requested_times: dict[date, set[time | None]] = defaultdict(set)
        for hint in plan.times:
            requested_times[hint.day].add(hint.at)
        has_matching_slot = any(
            not requested_times.get(day) or None in requested_times[day]
            or any(parse_instant(entry.start_utc).astimezone(zone).time() in requested_times[day] for entry in entries)
            for day, entries in day_slots.items()
        )
        if not has_matching_slot:
            if warm_entries is None:
                try:
                    warm_entries = tools.read_slot_entries(
                        event_id, first_warm_date, last_warm_date, "any", None, None, duration=duration,
                    )
                except (CalError, ToolRejected, ValidationError):
                    warm_entries = []
            anchors = tuple(hint for hint in plan.times if hint.day in wanted_days)
            if not anchors:
                anchors = tuple(DateHint(day) for day in plan.days)
            if plan.excluded_days or plan.excluded_weekdays:
                warm_entries = [entry for entry in warm_entries
                                if not plan.excludes(parse_instant(entry.start_utc).astimezone(zone).date())]
            if plan.prefer_daytime:
                matching_part = warm_entries if hours is None else [
                    entry for entry in warm_entries
                    if hours[0] <= parse_instant(entry.start_utc).astimezone(zone).hour < hours[1]
                ]
                warm_entries = preferred_daytime_slots(matching_part or warm_entries, zone)
            return _SlotResults(alternatives=nearest_slots(warm_entries, anchors, plan.part_of_day, tools.time_zone))

    if target is not None and not day_slots and (plan.move_later or plan.move_after or plan.part_of_day != "any"):
        first = max(wanted_days) + timedelta(days=1)
        if first <= last_warm_date:
            later = tools.read_slot_entries(
                event_id, first, last_warm_date, "any", target.ref, target.uid, duration=duration,
            )
            return _SlotResults(alternatives=[
                entry for entry in later
                if not plan.excludes(parse_instant(entry.start_utc).astimezone(zone).date())
            ][:3])

    if plan.spread_slots:
        return _SlotResults(slots=_spread_slots(day_slots))

    requested_local_times = {(hint.day, hint.at) for hint in plan.times if hint.at is not None}
    def matches_time(entry: SlotEntry) -> bool:
        local = parse_instant(entry.start_utc).astimezone(zone)
        return (local.date(), local.time()) in requested_local_times

    days = [sorted(day_slots[day], key=lambda entry: not matches_time(entry)) for day in sorted(day_slots)]
    initial = [entries[0] for entries in days]
    remainder = sorted(
        (entry for entries in days for entry in entries[1:]), key=lambda entry: not matches_time(entry)
    )
    limit = _CONSTRAINED_WEEK_SLOTS if plan.excluded_days or plan.excluded_weekdays else _MAX_DATED_SLOTS
    return _SlotResults(slots=sorted([*initial, *remainder][:limit], key=lambda entry: entry.start_utc),
                        shorter_options=shorter_options)


@dataclass(frozen=True)
class PrefetchResult:
    slots: list[SlotEntry] = field(default_factory=list)
    bookings: list[BookingEntry] | None = None
    elapsed_ms: float = 0
    timed_out: bool = False
    cal_calls: list[CalCallMetrics] = field(default_factory=list)
    slots_checked: bool = False
    alternatives: list[SlotEntry] = field(default_factory=list)
    more_bookings: bool = False
    unavailable_event: str | None = None
    shorter_options: bool = False
    closed_window: ClosedWindow | None = None


_Entries = _SlotResults | tuple[list[BookingEntry], bool]


def _prefetch_bookings(tools: CalendarTools, lookup: BookingLookup) -> tuple[list[BookingEntry], bool]:
    return tools.read_booking_entries(lookup.status, from_date=lookup.first, to_date=lookup.last, at=lookup.at)


@dataclass(frozen=True)
class _Job:
    entries: _Entries | None
    finished_at: float
    calls: list[CalCallMetrics]


class Prefetcher:
    """Bound optional read work for a service, independent of request executors."""

    def __init__(
        self,
        *,
        budget_seconds: float = PREFETCH_BUDGET_SECONDS,
        metrics_context: ContextVar[list[CalCallMetrics] | None],
    ) -> None:
        self._budget = budget_seconds
        self._metrics_context = metrics_context
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="livex-prefetch")
        self._capacity = BoundedSemaphore(4)

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _read(self, call: Callable[[], _Entries]) -> _Job:
        records: list[CalCallMetrics] = []
        token = self._metrics_context.set(records)
        try:
            try:
                entries = call()
            except (CalError, ToolRejected, ValidationError):
                entries = None
            return _Job(entries, perf_counter(), records)
        finally:
            self._metrics_context.reset(token)

    def _start(self, call: Callable[[], _Entries]) -> asyncio.Future[_Job] | None:
        if not self._capacity.acquire(blocking=False):
            return None
        try:
            future = self._executor.submit(self._read, call)
        except RuntimeError:
            self._capacity.release()
            return None
        future.add_done_callback(lambda _: self._capacity.release())
        return asyncio.wrap_future(future)

    async def fetch(self, tools: CalendarTools, plan: PrefetchPlan) -> PrefetchResult:
        lookup = plan.booking_lookup
        fetch_slots = bool(plan.days and (not plan.move_requested or plan.reschedule is not None))
        if not fetch_slots and lookup is None:
            return PrefetchResult()
        began = perf_counter()
        deadline = began + self._budget
        jobs = {}
        missing_admission = False
        if fetch_slots:
            future = self._start(lambda: _prefetch_slots(tools, plan))
            if future is not None:
                jobs["slots"] = future
            else:
                missing_admission = True
        if lookup is not None:
            future = self._start(lambda: _prefetch_bookings(tools, lookup))
            if future is not None:
                jobs["bookings"] = future
            else:
                missing_admission = True
        if not jobs:
            return PrefetchResult(elapsed_ms=(perf_counter() - began) * 1000, timed_out=missing_admission)

        ready: dict[str, _Entries | None] = {}
        calls: list[CalCallMetrics] = []
        try:
            done, pending = await asyncio.wait(jobs.values(), timeout=max(0, deadline - perf_counter()))
            timed_out = bool(pending) or missing_admission
            for name, future in jobs.items():
                if future not in done or future.cancelled():
                    continue
                result = future.result()
                if result.finished_at > deadline:
                    timed_out = True
                    continue
                ready[name] = result.entries
                calls.extend(result.calls)
        finally:
            for future in jobs.values():
                if not future.done():
                    future.cancel()

        availability = cast(_SlotResults | None, ready.get("slots"))
        bookings = cast(tuple[list[BookingEntry], bool] | None, ready.get("bookings"))
        return PrefetchResult(
            slots=availability.slots if availability is not None else [],
            bookings=bookings[0] if bookings is not None else None,
            elapsed_ms=(perf_counter() - began) * 1000,
            timed_out=timed_out,
            cal_calls=calls,
            slots_checked=availability is not None,
            alternatives=availability.alternatives if availability is not None else [],
            more_bookings=bookings[1] if bookings is not None else False,
            unavailable_event=availability.unavailable_event if availability is not None else None,
            shorter_options=availability.shorter_options if availability is not None else False,
            closed_window=availability.closed_window if availability is not None else None,
        )
