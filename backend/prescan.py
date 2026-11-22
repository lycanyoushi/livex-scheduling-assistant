"""Extract scheduling hints before any model calls."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import re
from typing import Literal
from zoneinfo import ZoneInfo

import dateparser


PartOfDay = Literal["any", "morning", "afternoon", "evening"]
PART_HOURS = {"morning": (8, 12), "afternoon": (12, 17), "evening": (17, 21)}
EMAIL = re.compile(r"[\w.!#$%&'*+/=?^{}|~-]+@[\w.-]+\.[A-Za-z]{2,}")
WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    "lunes": 0, "martes": 1, "miércoles": 2, "jueves": 3, "viernes": 4, "sábado": 5, "domingo": 6,
}
RELATIVE = {"today": 0, "tomorrow": 1, "yesterday": -1, "hoy": 0, "mañana": 1, "ayer": -1}
MONTH = (
    r"january|february|march|april|may|june|july|august|september|october|november|december|"
    r"enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre"
)
DATES = re.compile(
    rf"\b(?:(?P<week>(?:next|this)\s+week|esta\s+semana|(?:la\s+)?próxima\s+semana)"
    rf"|(?P<calendar>\d{{4}}-\d{{2}}-\d{{2}}|(?:{MONTH})\s+\d{{1,2}}(?:,\s*\d{{4}})?"
    rf"|\d{{1,2}}\s+(?:de\s+)?(?:{MONTH})(?:\s+(?:de\s+)?\d{{4}})?)"
    rf"|(?P<relative>{'|'.join(RELATIVE)})"
    rf"|(?:(?P<modifier>next|this)\s+)?(?P<weekday>{'|'.join(WEEKDAYS)})"
    rf"|the\s+(?P<ordinal>[1-9]|[12]\d|3[01])(?:st|nd|rd|th))\b",
    re.IGNORECASE,
)
CLOCK = (r"(?:(?P<marker>at|a\s+las)\s+)?(?P<hour>[0-2]?\d)"
         r"(?::(?P<minute>[0-5]\d))?\s*(?P<period>am|pm)?\b(?!:)")
ATTACHED_TIME = re.compile(r"^[,\s]*" + CLOCK, re.IGNORECASE)
LEADING_TIME = re.compile(r"(?<![\w:])" + CLOCK + r"[,\s]*$", re.IGNORECASE)
PARTS: dict[PartOfDay, re.Pattern[str]] = {
    "morning": re.compile(r"\b(?:morning|por la mañana)\b", re.IGNORECASE),
    "afternoon": re.compile(r"\b(?:afternoon|por la tarde)\b", re.IGNORECASE),
    "evening": re.compile(r"\b(?:evening|por la noche)\b", re.IGNORECASE),
}
INTENTS = {
    "book": re.compile(r"\b(?:book|schedule|meet|meeting|call|slots?|available|free|time|reservar)\b", re.I),
    "list": re.compile(r"\b(?:my meetings|my bookings|upcoming|scheduled|what do i have|what meetings)\b", re.I),
    "cancel": re.compile(r"\b(?:cancel|call off|drop|cancelar)\b", re.I),
    "reschedule": re.compile(r"\b(?:reschedule|move|push|change the time|reprogramar)\b", re.I),
}


@dataclass(frozen=True)
class DateHint:
    day: date
    at: time | None = None


@dataclass(frozen=True)
class PrescanResult:
    email: str | None
    dates: tuple[DateHint, ...]
    part_of_day: PartOfDay
    intents: tuple[str, ...]


def _requested_time(match: re.Match[str] | None) -> time | None:
    if not match or not any(match[name] for name in ("marker", "minute", "period")):
        return None
    hour, minute = int(match["hour"]), int(match["minute"] or 0)
    period = match["period"]
    if period:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if period.lower() == "pm" else 0)
    elif match["minute"] is None and 1 <= hour <= 7:
        hour += 12
    return time(hour, minute) if hour < 24 else None


def _ordinal_day(today: date, number: int) -> date | None:
    for offset in range(3):
        year, month = divmod(today.year * 12 + today.month - 1 + offset, 12)
        try:
            candidate = date(year, month + 1, number)
        except ValueError:
            continue
        if candidate >= today:
            return candidate
    return None


def _calendar_date(span: str, reference: datetime) -> date | None:
    parsed = dateparser.parse(
        span,
        languages=["en", "es"],
        settings={"RELATIVE_BASE": reference.replace(tzinfo=None), "PREFER_DATES_FROM": "future"},
    )
    return parsed.date() if parsed else None


def _dates(text: str, reference: datetime) -> tuple[DateHint, ...]:
    today = reference.date()
    monday = today - timedelta(days=today.weekday())
    results = []
    for position, match in enumerate(DATES.finditer(text)):
        at = _requested_time(ATTACHED_TIME.match(text[match.end():]))
        if at is None and position == 0:
            at = _requested_time(LEADING_TIME.search(text[:match.start()]))
        day = None
        if match["week"]:
            next_week = match["week"].startswith("next") or "próxima" in match["week"]
            start = monday + timedelta(days=7) if next_week else monday
            results.extend(DateHint(start + timedelta(days=index))
                           for index in range(5) if start + timedelta(days=index) >= today)
        elif match["relative"]:
            if match["relative"] != "mañana" or not text[:match.start()].endswith("por la "):
                day = today + timedelta(days=RELATIVE[match["relative"]])
        elif match["weekday"]:
            weekday = WEEKDAYS[match["weekday"]]
            if match["modifier"] == "next":
                day = monday + timedelta(days=7 + weekday)
            else:
                days = (weekday - today.weekday()) % 7
                if days == 0 and at is not None and at <= reference.time():
                    days = 7
                day = today + timedelta(days=days)
        elif match["ordinal"]:
            day = _ordinal_day(today, int(match["ordinal"]))
        elif match["calendar"]:
            day = _calendar_date(match["calendar"], reference)
        if day is not None:
            results.append(DateHint(day, at))
    return tuple(dict.fromkeys(results))


def prescan(text: str, *, now: datetime, time_zone: str) -> PrescanResult:
    if now.utcoffset() is None:
        raise ValueError("Prescan requires a timezone-aware clock.")
    reference = now.astimezone(ZoneInfo(time_zone))
    match = EMAIL.search(text)
    text_without_email = EMAIL.sub(" ", text.casefold())
    part: PartOfDay = next((name for name, pattern in PARTS.items() if pattern.search(text_without_email)), "any")
    return PrescanResult(
        match.group().casefold() if match else None,
        _dates(text_without_email, reference),
        part,
        tuple(name for name, pattern in INTENTS.items() if pattern.search(text_without_email)),
    )


def warm_prescan() -> None:
    """Initialize dateparser's English and Spanish paths once per app instance."""
    reference = datetime(2026, 11, 23)
    _calendar_date("November 26", reference)
    _calendar_date("26 de noviembre", reference)
