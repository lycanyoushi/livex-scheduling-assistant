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
    "list": re.compile(
        r"\b(?:my meetings|my bookings|upcoming|scheduled|what do i have|what meetings|"
        r"appointments? (?:are |is )?coming up|meetings? i(?:['’]m| am) booked for)\b", re.I,
    ),
    "cancel": re.compile(r"\b(?:cancel|call off|drop|cancelar)\b", re.I),
    "reschedule": re.compile(r"\b(?:reschedule|move|push|change the time|reprogramar)\b", re.I),
}
BOOKING_REQUEST = re.compile(r"\b(?:book|schedule|reserve|reservar)\b", re.I)
AVAILABILITY_REQUEST = re.compile(
    r"\b(?:availability|openings?|slots?|available|free times?|horarios?|disponibles?)\b"
    r"|\b(?:show|see|find|check|offer)\b[^.!?\n]{0,45}\b(?:times?|options?)\b"
    r"|\b(?:morning|afternoon|evening)\s+(?:options?|times?)\b",
    re.I,
)
TIME_REQUEST = re.compile(
    r"^\s*\d+\s*-?\s*(?:min(?:ute)?s?|hours?)\b"
    r"|\b(?:can|could|may)\s+(?:i|we)\s+(?:grab|get|have|take)\b[^.!?\n]{0,35}\b(?:mins?|minutes?|hours?)\b",
    re.I,
)
DATED_FOLLOWUP = re.compile(r"^\s*¿?\s*(?:what about|how about|qué tal)\b", re.I)
EXPLANATION = re.compile(
    r"\b(?:why|explain|how (?:to|does|do|did|would|will)|what can you help (?:me|us)|por qué|cómo funciona)\b",
    re.I,
)
EXISTING_BOOKING = re.compile(
    r"\b(?:my|our)\s+(?:(?:existing|current|upcoming|scheduled)\s+)?(?:bookings?|meetings?|appointments?|calendar)\b",
    re.I,
)
_WEEKDAY_TOKEN = re.compile(rf"\b(?P<weekday>{'|'.join(WEEKDAYS)})(?:s)?\b")
_EXCLUSION_PREFIX = re.compile(
    r"\b(?:(?P<short>no|not)|except(?:\s+for)?|excluding|other than|anything but|besides|"
    r"(?:can['’]t|cannot)\s+(?:do|make)|(?P<condition>busy|away|unavailable)|menos|excepto|salvo)"
    r"\s+(?:(?P<connector>on|for|el)\s+)?"
)
_EXCLUSION_SUFFIX = re.compile(
    r"\s+(?:are out|is out|(?:don['’]t|doesn['’]t|do not|does not|won['’]t) work|"
    r"(?:are|is) (?:off limits|unavailable)|(?:aren['’]t|isn['’]t) (?:good|available))\b"
)
_LIST_SEPARATOR = re.compile(r"\s*(?:,\s*(?:(?:and|or|y|o)\s+)?|\s+(?:and|or|y|o)\s+|\s*&\s*)")
_EXCLUSION_QUALIFIER = re.compile(
    r"\s+(?:(?:in the|during the|por la)\s+)?(?:mornings?|afternoons?|evenings?|nights?|ma[nñ]anas?|tardes?|noches?)\b"
    r"|\s+(?:between|before|after|at|a las|until|through|to)\b"
)
_POSITIVE_DATE_SUFFIX = re.compile(
    r"\s+(?:works?|(?:would|could) work|is (?:fine|okay|good|available|an option))\b"
)
_UNPARSED_EXCLUSION = re.compile(
    r"(?:before|after|between|until|during|too (?:early|late)|mornings?|afternoons?|evenings?|"
    r"weekdays?|weekends?|holidays?|the \d|\d|(?:jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\.)\b"
)


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
    availability_request: bool = False
    excluded_days: frozenset[date] = frozenset()
    excluded_weekdays: frozenset[int] = frozenset()
    exclusion_unresolved: bool = False

    def excludes(self, day: date) -> bool:
        return day in self.excluded_days or day.weekday() in self.excluded_weekdays


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


def _week_start(monday: date, week: str) -> date:
    return monday + timedelta(days=7) if week.startswith("next") or "próxima" in week else monday


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
            start = _week_start(monday, match["week"])
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


def _excluded_date(text: str, start: int, reference: datetime) -> tuple[int, int | date | None] | None:
    weekday = _WEEKDAY_TOKEN.match(text, start)
    if weekday:
        return weekday.end(), WEEKDAYS[weekday["weekday"]]
    match = DATES.match(text, start)
    if not match:
        return None
    if match["weekday"] and match["modifier"] == "this":
        offset = WEEKDAYS[match["weekday"]] - reference.weekday()
        return match.end(), reference.date() + timedelta(days=offset)
    days = _dates(match[0], reference)
    return match.end(), days[0].day if len(days) == 1 and not match["week"] else None


def _exclusion_list(text: str, start: int, reference: datetime) -> tuple[int, list[int | date], bool]:
    end = start
    targets: list[int | date] = []
    unresolved = False
    next_start = start
    while match := _excluded_date(text, next_start, reference):
        if _POSITIVE_DATE_SUFFIX.match(text, match[0]):
            return end, targets, not targets
        end, target = match
        if _EXCLUSION_QUALIFIER.match(text, end) or _requested_time(ATTACHED_TIME.match(text[end:])) is not None:
            return end, [], True
        if target is None:
            unresolved = True
        else:
            targets.append(target)
        separator = _LIST_SEPARATOR.match(text, end)
        if not separator:
            break
        if _excluded_date(text, separator.end(), reference) is None:
            unresolved |= bool(re.search(r"\b(?:and|or|y|o)\b|&", separator[0]))
            break
        next_start = separator.end()
    return end, targets, unresolved


def _exclusions(text: str, reference: datetime) -> tuple[str, frozenset[date], frozenset[int], bool]:
    days: set[date] = set()
    weekdays: set[int] = set()
    unresolved = False
    removed: list[tuple[int, int]] = []

    def collect(start: int, end: int, targets: list[int | date]) -> None:
        removed.append((start, end))
        days.update(target for target in targets if isinstance(target, date))
        weekdays.update(target for target in targets if isinstance(target, int))

    for cue in _EXCLUSION_PREFIX.finditer(text):
        end, targets, partial = _exclusion_list(text, cue.end(), reference)
        if end == cue.end():
            ambiguous = not (cue["short"] or cue["condition"]) or bool(cue["condition"] and cue["connector"])
            unresolved |= ambiguous or bool(_UNPARSED_EXCLUSION.match(text, end))
            continue
        unresolved |= partial
        collect(cue.start(), end, targets)
    for cue in _EXCLUSION_SUFFIX.finditer(text):
        for start in sorted({match.start() for pattern in (DATES, _WEEKDAY_TOKEN)
                             for match in pattern.finditer(text, 0, cue.start())}):
            end, targets, partial = _exclusion_list(text, start, reference)
            if end == cue.start() and targets:
                unresolved |= partial
                collect(start, cue.end(), targets)
                break
        else:
            unresolved = True

    cleaned = list(text)
    for first, last in removed:
        cleaned[first:last] = " " * (last - first)
    positive = "".join(cleaned)
    monday = reference.date() - timedelta(days=reference.weekday())
    for scope in DATES.finditer(positive):
        if scope["week"]:
            start = _week_start(monday, scope["week"])
            days.update(start + timedelta(days=weekday) for weekday in weekdays)
    return positive, frozenset(days), frozenset(weekdays), unresolved


def _availability_request(
    text: str, dates: tuple[DateHint, ...], part: PartOfDay, intents: tuple[str, ...],
) -> bool:
    if any(intent in intents for intent in ("list", "cancel", "reschedule")):
        return False
    if EXPLANATION.search(text) or EXISTING_BOOKING.search(text):
        return False
    if BOOKING_REQUEST.search(text) or AVAILABILITY_REQUEST.search(text) or TIME_REQUEST.search(text):
        return True
    if dates and DATED_FOLLOWUP.match(text):
        return True
    if len(dates) == 1 and dates[0].at is not None:
        phrase = text.strip(" \t\r\n¿?¡!,.")
        day = DATES.match(phrase)
        if day and ATTACHED_TIME.fullmatch(phrase[day.end():]):
            return True
    if dates and part != "any":
        remaining = PARTS[part].sub("", DATES.sub("", text))
        return not remaining.strip(" \t\r\n¿?¡!,.")
    return False


def prescan(text: str, *, now: datetime, time_zone: str) -> PrescanResult:
    if now.utcoffset() is None:
        raise ValueError("Prescan requires a timezone-aware clock.")
    reference = now.astimezone(ZoneInfo(time_zone))
    match = EMAIL.search(text)
    text_without_email = EMAIL.sub(" ", text.casefold())
    positive, excluded, weekdays, unresolved = _exclusions(text_without_email, reference)
    part: PartOfDay = "any"
    dates: tuple[DateHint, ...] = ()
    if not unresolved:
        part = next((name for name, pattern in PARTS.items() if pattern.search(positive)), "any")
        dates = tuple(hint for hint in _dates(positive, reference)
                      if hint.day not in excluded and hint.day.weekday() not in weekdays)
    intents = tuple(name for name, pattern in INTENTS.items() if pattern.search(text_without_email))
    relative_week = bool(excluded or weekdays) and any(match["week"] for match in DATES.finditer(positive))
    availability = not unresolved and _availability_request(positive, dates, part, intents)
    if relative_week and not unresolved and not set(intents) & {"list", "cancel", "reschedule"}:
        availability |= not EXPLANATION.search(text_without_email) and not EXISTING_BOOKING.search(text_without_email)
    return PrescanResult(
        match.group().casefold() if match else None,
        dates,
        part,
        intents,
        availability,
        excluded,
        weekdays,
        unresolved,
    )


def warm_prescan() -> None:
    """Initialize dateparser's English and Spanish paths once per app instance."""
    reference = datetime(2026, 11, 23)
    _calendar_date("November 26", reference)
    _calendar_date("26 de noviembre", reference)
