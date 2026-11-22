"""Fetch optional calendar context without mutating a conversation in a worker."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import re
from threading import BoundedSemaphore
from time import perf_counter
from typing import cast
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from backend.cal_client import CalError
from backend.prescan import EMAIL, DateHint, PartOfDay, PrescanResult
from backend.protocol import CalCallMetrics
from backend.state import BookingEntry, SessionState, SlotEntry
from backend.time import local_bounds, parse_instant
from backend.tools import CalendarTools, ToolRejected


PREFETCH_BUDGET_SECONDS = 1.2
WARM_DAYS = 14
_MAX_HINT_DAYS = 7
_MAX_DATED_SLOTS = 16
_UNDATED_SLOTS = 6
_BOOKING_REFERENCE = re.compile(r"\bB[0-9]+\b", re.IGNORECASE)


def warm_slot_bounds(now: datetime, time_zone: str) -> tuple[datetime, datetime]:
    """Return the half-open UTC window for the next 14 local dates."""
    return local_bounds(*_warm_dates(now, ZoneInfo(time_zone)), time_zone)


def _warm_dates(now: datetime, zone: ZoneInfo) -> tuple[date, date]:
    today = parse_instant(now).astimezone(zone).date()
    return today, today + timedelta(days=WARM_DAYS - 1)


@dataclass(frozen=True)
class _Reschedule:
    ref: str
    uid: str
    event_type_id: int


@dataclass(frozen=True)
class PrefetchPlan:
    days: tuple[date, ...]
    times: tuple[DateHint, ...]
    part_of_day: PartOfDay
    spread_slots: bool
    attendee_email: str | None
    reschedule: _Reschedule | None
    show_openings: bool = False

    def status(self) -> str:
        if self.days and self.attendee_email is not None:
            return "Checking availability and your bookings…"
        if self.attendee_email is not None:
            return "Checking your bookings…"
        if self.days:
            if len(self.days) == 1:
                day = self.days[0]
                return f"Checking availability for {day:%A} {day.day} {day:%b}…"
            return "Checking availability…"
        return "Checking the schedule…"


def plan_prefetch(state: SessionState, now: datetime, hints: PrescanResult, *, text: str = "") -> PrefetchPlan:
    instant = parse_instant(now)
    today = instant.astimezone(ZoneInfo(state.time_zone)).date()
    intents = set(hints.intents)
    days = tuple(sorted({hint.day for hint in hints.dates if hint.day >= today})[:_MAX_HINT_DAYS])
    spread = not hints.dates and ("book" in intents or hints.availability_request)
    if spread:
        days = tuple(today + timedelta(days=offset) for offset in range(_MAX_HINT_DAYS))
    email = state.user.email
    known_email = email if email and EMAIL.fullmatch(email) else None
    lookup_email = known_email if intents & {"list", "cancel", "reschedule"} else None
    target = None
    if days and "reschedule" in intents and known_email is not None:
        owned: dict[str, _Reschedule] = {}
        for ref, booking in state.bookings.items():
            if (
                booking.event_type_id is not None
                and booking.status in {"accepted", "pending"}
                and parse_instant(booking.start_utc) > instant
                and any(address.casefold() == known_email.casefold() for address in booking.attendee_emails)
            ):
                owned[ref] = _Reschedule(ref, booking.uid, booking.event_type_id)
        requested_refs = {ref.upper() for ref in _BOOKING_REFERENCE.findall(text)}
        if requested_refs:
            if len(requested_refs) == 1:
                target = owned.get(next(iter(requested_refs)))
        else:
            target = owned.get(state.rescheduling_booking_ref or "")
            if target is None and len({booking.uid for booking in owned.values()}) == 1:
                target = next(reversed(owned.values()))
    has_pending_change = state.rescheduling_booking_ref is not None or (
        state.proposal is not None and state.proposal.kind in {"cancel", "reschedule"}
    )
    return PrefetchPlan(
        days, hints.dates, hints.part_of_day, spread, lookup_email, target,
        show_openings=bool(days and hints.availability_request and not has_pending_change),
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


def _prefetch_slots(tools: CalendarTools, plan: PrefetchPlan) -> list[SlotEntry]:
    target = plan.reschedule
    if target is not None:
        event_id = target.event_type_id
        if not any(event["id"] == event_id for event in tools.event_types()):
            return []
    else:
        event_id = tools.default_event()["id"]
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
    for first, last in ranges:
        entries = tools.read_slot_entries(
            event_id, first, last, plan.part_of_day,
            target.ref if target is not None else None,
            target.uid if target is not None else None,
        )
        for entry in entries:
            local_day = parse_instant(entry.start_utc).astimezone(zone).date()
            if local_day in wanted_days:
                day_slots[local_day].append(entry)
    if plan.spread_slots:
        return _spread_slots(day_slots)

    requested_local_times = {(hint.day, hint.at) for hint in plan.times if hint.at is not None}
    def matches_time(entry: SlotEntry) -> bool:
        local = parse_instant(entry.start_utc).astimezone(zone)
        return (local.date(), local.time()) in requested_local_times

    days = [sorted(day_slots[day], key=lambda entry: not matches_time(entry)) for day in sorted(day_slots)]
    initial = [entries[0] for entries in days]
    remainder = sorted(
        (entry for entries in days for entry in entries[1:]), key=lambda entry: not matches_time(entry)
    )
    return sorted([*initial, *remainder][:_MAX_DATED_SLOTS], key=lambda entry: entry.start_utc)


@dataclass(frozen=True)
class PrefetchResult:
    slots: list[SlotEntry] = field(default_factory=list)
    bookings: list[BookingEntry] | None = None
    elapsed_ms: float = 0
    timed_out: bool = False
    cal_calls: list[CalCallMetrics] = field(default_factory=list)
    slots_checked: bool = False


_Entries = list[SlotEntry] | list[BookingEntry]


def _prefetch_bookings(tools: CalendarTools, email: str) -> list[BookingEntry]:
    entries, _ = tools.read_booking_entries(email, "upcoming")
    return [
        entry for entry in entries
        if entry.status in {"accepted", "pending"} and parse_instant(entry.start_utc) > tools.now
    ]


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
        if not plan.days and plan.attendee_email is None:
            return PrefetchResult()
        began = perf_counter()
        deadline = began + self._budget
        jobs = {}
        missing_admission = False
        if plan.days:
            future = self._start(lambda: _prefetch_slots(tools, plan))
            if future is not None:
                jobs["slots"] = future
            else:
                missing_admission = True
        email = plan.attendee_email
        if email is not None:
            future = self._start(lambda: _prefetch_bookings(tools, email))
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

        return PrefetchResult(
            slots=cast(list[SlotEntry], ready.get("slots") or []),
            bookings=cast(list[BookingEntry] | None, ready.get("bookings")),
            elapsed_ms=(perf_counter() - began) * 1000,
            timed_out=timed_out,
            cal_calls=calls,
            slots_checked=ready.get("slots") is not None,
        )
