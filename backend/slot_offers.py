"""Keep streamed and explicit time choices within the reply's offer limit."""

from collections.abc import Callable, Iterator

from backend.protocol import ServerEvent, SlotView
from backend.reference_text import ReferenceLabel, ReferenceText, repeats_time

_MAX_OFFERS = 4


class SlotOfferLimit:
    def __init__(
        self,
        earlier_label: Callable[[str], str | None],
        *,
        excluded_ref: Callable[[str], bool] | None = None,
    ) -> None:
        self.earlier_label = earlier_label
        self.excluded_ref = excluded_ref
        self.slots: dict[str, SlotView] = {}
        self.offered: set[str] = set()
        self.text = ReferenceText(self._replace_marker)

    def _label(self, ref: str) -> str | None:
        slot = self.slots.get(ref)
        return f"{slot['dayLabel']} at {slot['timeLabel']}" if slot else self.earlier_label(ref)

    def _offer(self, ref: str) -> bool:
        if ref in self.offered or len(self.offered) >= _MAX_OFFERS:
            return False
        self.offered.add(ref)
        return True

    def _replace_marker(self, ref: str, preceding: str) -> ReferenceLabel | None:
        if not ref.startswith("S"):
            return None
        if self.excluded_ref is not None and self.excluded_ref(ref):
            return ReferenceLabel("", compact=True)
        label = self._label(ref)
        if label is None:
            return ReferenceLabel(ref)
        if self._offer(ref):
            return None
        if repeats_time(preceding, label):
            return ReferenceLabel("", compact=True)
        return ReferenceLabel(label)

    def finish(self) -> Iterator[ServerEvent]:
        text = self.text.feed("", final=True)
        if text:
            yield {"type": "text_delta", "text": text}

    def apply(self, event: ServerEvent) -> Iterator[ServerEvent]:
        kind = event["type"]
        if kind == "refs":
            self.slots.update(event.get("slots", {}))
        elif kind == "text_delta":
            text = self.text.feed(event["text"])
            if text:
                yield {"type": "text_delta", "text": text}
            return
        elif kind == "assistant_text":
            yield from self.finish()
            event = {"type": kind, "text": self.text.feed(event["text"], final=True)}
        elif kind == "slot_chips":
            choices, plain = [], []
            for ref in dict.fromkeys(event["refs"]):
                if self.excluded_ref is not None and self.excluded_ref(ref):
                    continue
                if self._label(ref) is not None:
                    (choices if self._offer(ref) else plain).append(ref)
            event = {"type": kind, "refs": choices, **({"note": event["note"]} if "note" in event else {})}
            if plain:
                event["plainRefs"] = plain
        elif kind == "error":
            yield from self.finish()
        yield event
