"""Replace streamed references while keeping adjacent prose readable."""

from collections.abc import Callable
from dataclasses import dataclass
import re

_REF = r"[SB][1-9][0-9]*"
_DISPLAY_REF = re.compile(
    rf"(?P<space>[ \t]*)(?:\([ \t]*\[\[(?P<wrapped>{_REF})\]\][ \t]*\)|\[\[(?P<bare>{_REF})\]\])"
)
_INCOMPLETE_REF = r"\[(?:\[(?:[SB](?:[1-9][0-9]*\]?)?)?)?"
_PENDING = re.compile(rf"[ \t]*(?:\([ \t]*(?:{_INCOMPLETE_REF}|\[\[{_REF}\]\])?|{_INCOMPLETE_REF})?[ \t]*\Z")
_CLOCK = r"(?:[01]?\d|2[0-3]):[0-5]\d(?:[ \t]*[ap]\.?m\.?)?"
_TIME_SUFFIX = re.compile(
    rf"(?<![\w:+–—−-])(?P<start>{_CLOCK})(?:[ \t]*(?:[-–—−]|to)[ \t]*(?P<end>{_CLOCK}))?\Z",
    re.IGNORECASE,
)
_RECENT_CHARS = 128


@dataclass(frozen=True)
class ReferenceLabel:
    text: str
    compact: bool = False


class ReferenceText:
    def __init__(self, replace: Callable[[str, str], ReferenceLabel | None]) -> None:
        self.replace = replace
        self.pending = ""
        self.recent = ""

    def feed(self, chunk: str, *, final: bool = False) -> str:
        text = self.pending + chunk
        self.pending = ""
        if not final:
            suffix = _PENDING.search(text)
            if suffix and suffix[0]:
                self.pending = suffix[0]
                text = text[:suffix.start()]

        def render(match: re.Match[str]) -> str:
            ref = match["wrapped"] or match["bare"]
            preceding = (self.recent + text[:match.start()])[-_RECENT_CHARS:]
            label = self.replace(ref, preceding)
            if label is None:
                return match[0]
            if label.compact:
                return (match["space"] or " ") + label.text if label.text else ""
            return match[0].replace(f"[[{ref}]]", label.text, 1)

        rendered = _DISPLAY_REF.sub(render, text)
        self.recent = "" if final else (self.recent + text)[-_RECENT_CHARS:]
        return rendered


def _clock_parts(value: str) -> tuple[int, int, str]:
    compact = re.sub(r"[. \t]", "", value.lower())
    period = compact[-2:] if compact.endswith(("am", "pm")) else ""
    clock = compact.removesuffix(period)
    hour, minute = clock.split(":", maxsplit=1)
    return int(hour), int(minute), period


def _minutes(hour: int, minute: int, period: str) -> int:
    return ((hour % 12 + (12 if period == "pm" else 0)) if period else hour) * 60 + minute


def _time_range(value: str) -> tuple[int, int | None] | None:
    match = _TIME_SUFFIX.search(value.rstrip(" \t*_`"))
    if not match:
        return None
    hour, minute, period = _clock_parts(match["start"])
    if not match["end"]:
        return _minutes(hour, minute, period), None
    end_hour, end_minute, end_period = _clock_parts(match["end"])
    if not period and end_period and 1 <= hour <= 12:
        period = end_period
    return _minutes(hour, minute, period), _minutes(end_hour, end_minute, end_period)


def repeats_time(preceding: str, label: str) -> bool:
    """Check only a clock or range immediately before the reference."""
    expected = _time_range(label)
    return expected is not None and _time_range(preceding) == expected
