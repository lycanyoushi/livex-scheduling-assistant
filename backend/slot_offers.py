"""Keep streamed and explicit time choices within the reply's offer limit."""

from collections.abc import Callable, Iterator
import re

from backend.protocol import ServerEvent, SlotView

_MAX_OFFERS = 4
_SLOT_MARKER = re.compile(r"\[\[(S[1-9][0-9]*)\]\]")
_PARTIAL_MARKER = re.compile(r"\[\[(?:S(?:[1-9][0-9]*\]?)?)?\Z|\[\Z")


class SlotOfferLimit:
    def __init__(self, earlier_label: Callable[[str], str | None]) -> None:
        self.earlier_label = earlier_label
        self.slots: dict[str, SlotView] = {}
        self.offered: set[str] = set()
        self.partial = ""

    def _label(self, ref: str) -> str | None:
        slot = self.slots.get(ref)
        return f"{slot['dayLabel']} at {slot['timeLabel']}" if slot else self.earlier_label(ref)

    def _offer(self, ref: str) -> bool:
        if ref in self.offered or len(self.offered) >= _MAX_OFFERS:
            return False
        self.offered.add(ref)
        return True

    def _replace_marker(self, match: re.Match[str]) -> str:
        ref = match[1]
        label = self._label(ref)
        if label is None:
            return ref
        return match[0] if self._offer(ref) else label

    def finish(self) -> Iterator[ServerEvent]:
        if self.partial:
            yield {"type": "text_delta", "text": self.partial}
            self.partial = ""

    def apply(self, event: ServerEvent) -> Iterator[ServerEvent]:
        kind = event["type"]
        if kind == "refs":
            self.slots.update(event.get("slots", {}))
        elif kind == "text_delta":
            text = self.partial + event["text"]
            suffix = _PARTIAL_MARKER.search(text)
            self.partial = suffix[0] if suffix else ""
            if suffix:
                text = text[:suffix.start()]
            if text:
                yield {"type": "text_delta", "text": _SLOT_MARKER.sub(self._replace_marker, text)}
            return
        elif kind == "assistant_text":
            yield from self.finish()
            event = {"type": kind, "text": _SLOT_MARKER.sub(self._replace_marker, event["text"])}
        elif kind == "slot_chips":
            choices, plain = [], []
            for ref in dict.fromkeys(event["refs"]):
                if self._label(ref) is not None:
                    (choices if self._offer(ref) else plain).append(ref)
            event = {"type": kind, "refs": choices, **({"note": event["note"]} if "note" in event else {})}
            if plain:
                event["plainRefs"] = plain
        elif kind == "error":
            yield from self.finish()
        yield event
