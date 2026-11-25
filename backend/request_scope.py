"""Recognize requests that need calendar actions outside a single guest booking."""

import re

from backend.prescan import EMAIL


_BOOKING_ACTION = re.compile(
    r"\b(?:book|reserve|arrange|(?<!my )(?<!our )schedule|set up|organize|invite|reservar|agendar|programar)\b",
)
_CHANGE_ACTION = re.compile(r"\b(?:cancel|move|reschedule|push|cancelar|reprogramar)\b")
_SERIES_TARGET = re.compile(r"\b(?:series|all (?:occurrences|instances)|every (?:occurrence|instance))\b")
_CADENCE = r"(?:daily|weekly|biweekly|fortnightly|monthly|quarterly|yearly|annually)"
_RECURRENCE = re.compile(
    r"\b(?:recurring|recurrence|repeating|repeat|every (?:other )?(?:day|weekday|weekend|week|month|quarter|year|monday|tuesday|"
    r"wednesday|thursday|friday|saturday|sunday)s?|"
    r"cada (?:lunes|martes|miércoles|jueves|viernes|sábado|domingo|día|semana|mes|año))\b"
    r"|\bon (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)s\b"
    rf"|\b{_CADENCE}(?:\s+(?:appointment|call|catch[- ]?up|chat|check[- ]?in|intro|meeting|session|standup|sync)\b"
    rf"|\b(?=\s*(?:[,.!?]|$|at\b)))",
)
_CALENDAR_BLOCK = re.compile(
    r"\b(?:block (?:off|out)|mark (?:me|myself) busy)\b"
    r"|\bblock\s+[^.!?\n]{0,32}\b(?:minutes?|hours?|time|morning|afternoon|evening)\b"
    r"|\b(?:block|hold|reserve|set aside|mark)\b[^.!?\n]{0,70}\b(?:my|our) calendar\b"
    r"|\b(?:add|put|create|schedule|reserve|set|block)\b[^.!?\n]{0,50}"
    r"\b(?:personal (?:calendar )?block|focus time|out of office|busy block|(?:a )?calendar block|time[- ]block)\b",
)
_GUEST_COUNT = re.compile(
    r"\b(?:both|multiple|several|[2-9]|[1-9]\d+|two|three|four|five|six|seven|eight|nine|ten|more than one)"
    r"\s+(?:guests?|attendees?|people|participants?|invitees?|colleagues?|co-?workers?)\b",
)
_ADD_GUEST = re.compile(
    r"\b(?:add|include|invite|cc|copy)\b[^.!?\n]{0,60}\b"
    r"(?:(?:another|additional|extra|one more|second)\s+(?:guest|attendee|person)"
    r"|(?:to|on)\s+(?:my|the|that|this|our)\s+(?:meeting|booking|appointment|call))\b",
)
_ADD_TO_DRAFT = re.compile(
    r"\b(?:also|and)\s+(?:add|invite|include|cc|copy)\b"
    r"|\b(?:add|invite|include|cc|copy)\b[^.!?\n]{1,60}\b(?:too|as well)\b",
)
_GUEST_LIST = re.compile(
    r"\b(?:with|invite|for|con|invitar)\s+(?P<first>\w[\w'’ -]{0,59}?)"
    r"(?:\s*\(\s*\))?\s*,?\s+(?:and|plus|&|y)\s+(?:(?:a|an|the)\s+)?(?P<second>[\w'’-]+)\b",
)
_DIRECT_GUEST_LIST = re.compile(
    r"\b(?i:book|invite|schedule)\s+(?P<first>(?:[A-Z][\w'’-]*\s+){1,4})"
    r"(?i:and|plus|&)\s+(?P<second>[A-Z][\w'’-]+)\b",
)
_NON_GUEST_WORDS = frozenset({
    "a", "an", "at", "attach", "book", "but", "check", "choose", "decide", "for", "give", "leave", "let",
    "make", "move", "next", "notes", "on", "pick", "please", "schedule", "see", "send", "set", "tell", "then",
    "this", "times", "today", "tomorrow", "tonight", "us", "when", "you",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
})

_BLOCK_REPLY = "For a personal time block, add a busy event in your calendar app. I can help book a Cal.com meeting with a guest."
_SERIES_REPLY = "I can help book one meeting at a time. Would you like to book the first occurrence? If so, tell me its date and time."
_SINGLE_GUEST_REPLY = "I can book a meeting with one guest. Which guest should I use?"
SCOPE_REPLIES = {"multiple_guests": _SINGLE_GUEST_REPLY, "recurring": _SERIES_REPLY, "personal_block": _BLOCK_REPLY}


def _names_multiple_guests(text: str, lower: str) -> bool:
    for matches in (_GUEST_LIST.finditer(lower), _DIRECT_GUEST_LIST.finditer(text)):
        for match in matches:
            first = match["first"].casefold().split()
            second = match["second"].casefold()
            if (
                not _NON_GUEST_WORDS.intersection(first) and second not in _NON_GUEST_WORDS
                and not first[0][0].isdigit() and not second[0].isdigit()
            ):
                return True
    return False


def unsupported_request(text: str, *, collecting_guest: bool = False) -> str | None:
    """Return a reply before reading the calendar or changing a guest draft."""
    without_addresses = EMAIL.sub(" ", text)
    lower = without_addresses.casefold()
    if _CALENDAR_BLOCK.search(lower):
        return _BLOCK_REPLY
    booking = _BOOKING_ACTION.search(lower)
    if _CHANGE_ACTION.search(lower) and _SERIES_TARGET.search(lower):
        return "I can help change one occurrence at a time. Which meeting should I change?"
    if (booking or collecting_guest or re.search(r"\bat\s+\d", lower)) and (
        _RECURRENCE.search(lower) or _SERIES_TARGET.search(lower)
    ):
        return _SERIES_REPLY
    if _ADD_GUEST.search(lower) or (collecting_guest and _ADD_TO_DRAFT.search(lower)):
        return _SINGLE_GUEST_REPLY
    if booking or collecting_guest:
        addresses = {address.casefold() for address in EMAIL.findall(text)}
        if len(addresses) > 1 or _GUEST_COUNT.search(lower) or _names_multiple_guests(without_addresses, lower):
            return _SINGLE_GUEST_REPLY
    return None
