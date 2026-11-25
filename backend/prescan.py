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
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
    r"enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre"
)
DATES = re.compile(
    rf"\b(?:(?P<week>(?:next|this)\s+week|esta\s+semana|(?:la\s+)?próxima\s+semana)"
    rf"|(?P<calendar>\d{{4}}-\d{{2}}-\d{{2}}"
    rf"|(?:(?:{MONTH})\s+\d{{1,2}}|\d{{1,2}}\s+(?:de\s+)?(?:{MONTH}))"
    rf"(?:(?:,\s*|\s+(?:de\s+)?)\d{{4}})?)"
    rf"|(?P<relative>{'|'.join(RELATIVE)})"
    rf"|(?:(?P<modifier>next|this)\s+)?(?P<weekday>{'|'.join(WEEKDAYS)})"
    rf"|the\s+(?P<ordinal>[1-9]|[12]\d|3[01])(?:st|nd|rd|th))\b",
    re.IGNORECASE,
)
CLOCK = (r"(?:(?P<marker>at|a\s+las)\s+)?(?P<hour>[0-2]?\d)"
         r"(?::(?P<minute>[0-5]\d))?\s*(?P<period>am|pm)?\b(?!:)")
ATTACHED_TIME = re.compile(r"^[,\s]*" + CLOCK, re.IGNORECASE)
LEADING_TIME = re.compile(r"(?<![\w:])" + CLOCK + r"[,\s]*$", re.IGNORECASE)
_WINDOW_CLOCK = r"(?:\d{3,4}|\d{1,2}(?::\d{2})?(?:\s*(?:am|pm))?)\b(?!:)"
_TIME_WINDOW = re.compile(
    rf"\b(?:(?P<from>from)|between)\s+(?P<start>{_WINDOW_CLOCK})\s+"
    rf"(?(from)to|and)\s+(?P<end>{_WINDOW_CLOCK})",
)
_UNREAD_DATE_OR_TIME = re.compile(
    rf"[\d–—-]|\b(?:{MONTH}|{'|'.join(WEEKDAYS)}|{'|'.join(RELATIVE)}|"
    r"mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun|weeks?|weekends?|months?|years?|"
    r"morning|afternoon|evening|tonight|noon|midnight|mediodía|medianoche|am|pm|hours?|minutes?|"
    r"from|to|until|between|through|before|after)\b"
)
_SHORT_WEEKDAY = "mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun"
_PRIOR_WEEKDAY = re.compile(rf"\b(?:last|previous|past)\s+(?:{'|'.join(WEEKDAYS)}|{_SHORT_WEEKDAY})\b")
_TIME_UNITS = "days?|weeks?|weekends?|months?|years?"
_RELATIVE_PERIOD = re.compile(
    rf"\b(?:in|after|before|within|next|last|previous|this)\s+(?:[\w-]+\s+){{0,3}}(?:{_TIME_UNITS})\b"
    rf"|\b(?:{_TIME_UNITS})\s+(?:ago|later|earlier|out|from\s+now|(?:after|before)\s+(?:today|tomorrow|next|last))\b"
)
_BARE_TIMING_CUE = re.compile(
    r"\b(?:noon|midnight|tonight|mediodía|medianoche)\b"
    r"|\b\d{1,2}[:.]\d{2}(?!\d)|\b\d{1,2}\s*[ap]\.?\s*m\.?(?!\w)"
    r"|\b(?:at|before|after|by|a\s+las)\s+\d{1,4}\b|\b\d{1,2}\s+(?:instead|rather)\b"
    r"|\b[\w-]+\s+(?:hours?|minutes?)\b",
)
_BARE_DATE_CUE = re.compile(
    r"\b\d{4}[-./]\d{1,2}[-./]\d{1,2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b\d{1,2}\.\d{1,2}\.\d{4}\b"
    rf"|\b(?:in|on|by|next|last|this)\s+(?:{MONTH})\b|\b(?:{_SHORT_WEEKDAY})\b",
)
_STANDALONE_COMPACT_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3])[0-5][0-9]")
_ZONE_HINT = re.compile(
    r"\b[a-z][a-z_.+-]*(?:/[a-z0-9_.+-]+)+\b|\b(?:utc|gmt|time\s*zone)\b",
    re.I,
)
_CLOCK_ZONE_SUFFIX = re.compile(
    r"\b(?:\d{1,2}(?::\d{2})?\s*[ap]m|\d{1,2}:\d{2}|\d{1,2})\s+"
    r"(?:in\s+)?(?:[a-z][a-z.'-]*\s+){1,3}time\b",
    re.I,
)
_CLOCK_LOCATION = re.compile(r"\b(?:\d{1,2}:\d{2}|\d{1,2}(?::\d{2})?\s*(?i:[ap]m))\s+in\s+[A-Z][a-z]+\b")
_ZONE_ABBREVIATION = re.compile(r"\b[0-9](?:\s*(?i:[ap]m))?\s+(?:[A-Z]{2,5}T|GMT|UTC)\b")
_PROPER_ZONE_LABEL = re.compile(
    r"\b(?!(?:Book|Schedule|Reserve|Show|Find|Check|See|What|Which|Pick|Choose|Local|Free|Available|More|Meeting)\b)"
    r"[A-Z][a-z]+(?:[ -][A-Z][a-z]+)*\s+time\b",
)
PARTS: dict[PartOfDay, re.Pattern[str]] = {
    "morning": re.compile(r"\b(?:morning|por la mañana)\b", re.IGNORECASE),
    "afternoon": re.compile(r"\b(?:afternoon|por la tarde)\b", re.IGNORECASE),
    "evening": re.compile(r"\b(?:evening|por la noche)\b", re.IGNORECASE),
}
_PERSONAL_SCHEDULE = re.compile(r"\bmy\s+schedule\b")
INTENTS = {
    "book": re.compile(r"\b(?:book|schedule|meet|meeting|call|slots?|available|free|time|reservar)\b", re.I),
    "list": re.compile(
        r"\b(?:my meetings|my bookings|upcoming|scheduled|what do i have|what meetings|"
        r"appointments? (?:are |is )?coming up|meetings? i(?:['’]m| am) booked for)\b"
        r"|(?:^\s*(?:on\s+)?|\b(?:show(?:\s+me)?|see|check|list)\s+|\bwhat(?:['’]s|\s+is)(?:\s+on)?\s+)"
        r"my\s+(?:schedule|calendar)\b", re.I,
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
    r"|\b(?:can|could|may)\s+(?:i|we)\s+(?:grab|get|have|take)\b[^.!?\n]{0,35}"
    r"\b(?:mins?|minutes?|hours?|meetings?|calls?|appointments?|slots?)\b",
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
_MOVE_DESTINATION = re.compile(r"\b(?:reschedule|move|push|change the time)\b[^.!?\n]*?\bto\s+", re.I)
_MEETING_TIME = re.compile(r"\b(?:my|the|our)\s+" + CLOCK, re.I)
_AFTER_TIME = re.compile(r"\bafter\s+" + CLOCK, re.I)
_MOVE_DELAY = re.compile(r"\b(an?|one|[1-9]\d{0,2})\s*-?\s*(hours?|minutes?|mins?)\s+later\b", re.I)
_DURATION = re.compile(r"\b(\d{1,4})\s*[- ]?\s*(min(?:ute)?s?|h(?:ou)?rs?)\b", re.I)
_THIS_PART = re.compile(r"\bthis\s+(?:morning|afternoon|evening)\b", re.I)
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
class TimeWindow:
    start: time
    end: time
    full_meeting: bool = False

    @property
    def minutes(self) -> int:
        return (self.end.hour - self.start.hour) * 60 + self.end.minute - self.start.minute

    def contains(self, start: datetime, end: datetime) -> bool:
        return start.date() == end.date() and self.start <= start.time() < end.time() <= self.end


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
    existing_meeting: DateHint | None = None
    move_after: time | None = None
    move_later: bool = False
    duration_minutes: int | None = None
    move_delay_minutes: int | None = None
    time_window: TimeWindow | None = None
    invalid_time_window: bool = False
    date_time_complete: bool = True
    past_date_requested: bool = False
    timing_mentioned: bool = False
    date_mentioned: bool = False

    @property
    def needs_interpretation(self) -> bool:
        return not self.date_time_complete

    def excludes(self, day: date) -> bool:
        return day in self.excluded_days or day.weekday() in self.excluded_weekdays


def _requested_time(match: re.Match[str] | None, *, bare: bool = False) -> time | None:
    if not match or (not bare and not any(match[name] for name in ("marker", "minute", "period"))):
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


def _window_time(value: str) -> time | None:
    if value.isdigit() and len(value) >= 3:
        hour, minute = int(value[:-2]), int(value[-2:])
        return time(hour, minute) if hour < 24 and minute < 60 else None
    return _requested_time(re.fullmatch(CLOCK, value), bare=True)


_TYPED_TIME = re.compile(
    r"\s*(?:the\s+)?" + CLOCK + r"(?:\s+(?:one|works|is good|please))?[.!]?\s*", re.I,
)
_TYPED_ORDINAL = re.compile(
    r"(?:the\s+)?(first|second|third|fourth|1st|2nd|3rd|4th)(?:\s+(?:one|option|works|please))?[.!]?", re.I,
)
_ORDINAL_OPTIONS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "4th": 3}


def typed_choice(text: str) -> time | int | None:
    if ordinal := _TYPED_ORDINAL.fullmatch(text.strip()):
        return _ORDINAL_OPTIONS[ordinal[1].lower()]
    return _requested_time(_TYPED_TIME.fullmatch(text))


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


def _dates(
    text: str, reference: datetime, *, read: list[tuple[int, int]] | None = None,
) -> tuple[DateHint, ...]:
    today = reference.date()
    monday = today - timedelta(days=today.weekday())
    results = []
    for position, match in enumerate(DATES.finditer(text)):
        trailing = ATTACHED_TIME.match(text[match.end():])
        at = _requested_time(trailing)
        time_span = (match.end(), match.end() + trailing.end()) if at is not None and trailing else None
        if at is None and position == 0:
            leading = LEADING_TIME.search(text[:match.start()])
            at = _requested_time(leading)
            time_span = leading.span() if at is not None and leading else None
        day = None
        if match["week"]:
            start = _week_start(monday, match["week"])
            results.extend(DateHint(start + timedelta(days=index))
                           for index in range(5) if start + timedelta(days=index) >= today)
            if read is not None:
                read.append(match.span())
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
            if read is not None:
                read.append(match.span())
                if time_span is not None:
                    read.append(time_span)
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
    if any(intent in intents for intent in ("cancel", "reschedule")):
        return False
    if "list" in intents:
        clauses = re.split(r"\band\b", text, maxsplit=1)
        return len(clauses) == 2 and any(
            not INTENTS["list"].search(clause) and _availability_request(clause, dates, part, ("book",))
            for clause in clauses
        )
    if EXPLANATION.search(text) or EXISTING_BOOKING.search(text):
        return False
    if (BOOKING_REQUEST.search(_PERSONAL_SCHEDULE.sub(" ", text)) or AVAILABILITY_REQUEST.search(text)
            or TIME_REQUEST.search(text)):
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
    without_email = EMAIL.sub(" ", text)
    text_without_email = without_email.casefold()
    positive, excluded, weekdays, unresolved = _exclusions(text_without_email, reference)
    booking_text = _PERSONAL_SCHEDULE.sub(" ", text_without_email)
    intents = tuple(name for name, pattern in INTENTS.items()
                    if pattern.search(booking_text if name == "book" else text_without_email))
    is_move = "reschedule" in intents
    destination = _MOVE_DESTINATION.search(positive) if is_move else None
    source = positive[:destination.end()] if destination else positive
    window = positive[destination.end():] if destination else positive
    offset = len(positive) - len(window)
    read: list[tuple[int, int]] = []
    existing = None
    if is_move or "cancel" in intents:
        meeting = _MEETING_TIME.search(source)
        match_time = _requested_time(meeting, bare=True)
        if match_time is not None:
            source_dates = _dates(source, reference.replace(hour=0, minute=0, second=0, microsecond=0), read=read)
            existing = DateHint(source_dates[0].day if source_dates else reference.date(), match_time)
            read.append(meeting.span())
    after = _AFTER_TIME.search(window) if is_move else None
    move_after = _requested_time(after, bare=True)
    move_later = is_move and bool(re.search(r"\blater\b", window))
    part: PartOfDay = "any"
    dates: tuple[DateHint, ...] = ()
    read_window: list[tuple[int, int]] = []
    if destination:
        read.append((source.rstrip().rfind("to"), offset))
    if move_after is not None and after:
        read_window.append(after.span())
    bounds = None if is_move or "cancel" in intents else _TIME_WINDOW.search(window)
    if bounds is not None:
        # A clock endpoint before a date must not be treated as an exact start.
        date_text = window[:bounds.start()] + " " * (bounds.end() - bounds.start()) + window[bounds.end():]
    else:
        date_text = window
    if not unresolved:
        part = next((name for name, pattern in PARTS.items() if pattern.search(window)), "any")
        if part != "any":
            read_window.append(PARTS[part].search(window).span())
        dates = tuple(hint for hint in _dates(date_text, reference, read=read_window)
                      if hint.day not in excluded and hint.day.weekday() not in weekdays)
        if is_move and existing and not destination:
            dates = ()
        this_part = _THIS_PART.search(window)
        if not dates and (move_after or move_later or this_part):
            day = reference.date() if this_part or existing is None else existing.day
            if day not in excluded and day.weekday() not in weekdays:
                dates = (DateHint(day),)
        if destination and dates and move_after:
            dates = tuple(DateHint(hint.day) for hint in dates)
        if destination and not dates and not move_after:
            exact_move = ATTACHED_TIME.match(window)
            if new_time := _requested_time(exact_move):
                day = existing.day if existing else reference.date()
                if day not in excluded and day.weekday() not in weekdays:
                    dates = (DateHint(day, new_time),)
                    read_window.append(exact_move.span())
    relative_week = bool(excluded or weekdays) and any(match["week"] for match in DATES.finditer(positive))
    availability = not unresolved and _availability_request(positive, dates, part, intents)
    if relative_week and not unresolved and not set(intents) & {"list", "cancel", "reschedule"}:
        availability |= not EXPLANATION.search(text_without_email) and not EXISTING_BOOKING.search(text_without_email)
    duration = _DURATION.search(positive)
    duration_minutes = int(duration[1]) * (60 if duration[2].startswith("h") else 1) if duration else None
    if duration is not None and duration_minutes is not None and 0 < duration_minutes <= 1440:
        read.append(duration.span())
    time_window = None
    if bounds is not None and availability:
        start, end = _window_time(bounds["start"]), _window_time(bounds["end"])
        if start is not None and end is not None and start < end:
            full_meeting = bool(bounds["from"] and len(dates) == 1 and duration_minutes is None
                                and BOOKING_REQUEST.search(positive) and not AVAILABILITY_REQUEST.search(positive))
            time_window = TimeWindow(start, end, full_meeting)
        if dates or time_window is None:
            read_window.append(bounds.span())
    move_delay = _MOVE_DELAY.search(window) if is_move else None
    move_delay_minutes = None
    if move_delay:
        amount = int(move_delay[1]) if move_delay[1].isdigit() else 1
        move_delay_minutes = amount * (60 if move_delay[2].startswith("hour") else 1)
        read_window.append(move_delay.span())
    unread = list(positive)
    read.extend((offset + first, offset + last) for first, last in read_window)
    for first, last in read:
        unread[first:last] = " " * (last - first)
    prior = _PRIOR_WEEKDAY.search(positive)
    past_date_requested = bool(
        not unresolved and prior
        and not re.search(r"\bthe\s*$", positive[:prior.start()])
        and not re.match(r"\s+(?:of|in)\b", positive[prior.end():])
        and not DATES.search(_PRIOR_WEEKDAY.sub(" ", positive))
    )
    named_zone = (
        _ZONE_HINT.search(positive) or _CLOCK_ZONE_SUFFIX.search(positive)
        or _ZONE_ABBREVIATION.search(without_email) or _PROPER_ZONE_LABEL.search(without_email)
        or _CLOCK_LOCATION.search(without_email)
    )
    unread_text = "".join(unread)
    explicit_date = any(
        match["relative"] != "mañana" or not positive[:match.start()].endswith("por la ")
        for match in DATES.finditer(positive)
    )
    date_mentioned = bool(
        explicit_date or prior or _BARE_DATE_CUE.search(positive) or _RELATIVE_PERIOD.search(positive)
        or _THIS_PART.search(positive)
    )
    timing_mentioned = bool(
        date_mentioned or bounds or part != "any" or duration or named_zone or _BARE_TIMING_CUE.search(positive)
        or _STANDALONE_COMPACT_CLOCK.fullmatch(positive.strip())
    )
    complete = not (
        unresolved or prior or named_zone or _RELATIVE_PERIOD.search(unread_text)
        or _UNREAD_DATE_OR_TIME.search(unread_text)
    )
    invalid_window = complete and bounds is not None and availability and time_window is None
    return PrescanResult(
        match.group().casefold() if match else None,
        dates if complete else (),
        part if complete else "any",
        intents,
        availability,
        excluded,
        weekdays,
        unresolved,
        existing if complete else None,
        move_after if complete else None,
        move_later if complete else False,
        duration_minutes if complete else None,
        move_delay_minutes if complete else None,
        time_window if complete else None,
        invalid_window,
        complete,
        past_date_requested,
        timing_mentioned,
        date_mentioned,
    )


def warm_prescan() -> None:
    """Initialize dateparser's English and Spanish paths once per app instance."""
    reference = datetime(2026, 11, 23)
    _calendar_date("November 26", reference)
    _calendar_date("26 de noviembre", reference)
