import unittest

from backend.slot_offers import SlotOfferLimit


def slot(hour):
    return {
        "dayLabel": "Thu 26 Nov",
        "timeLabel": f"{hour}:00",
        "startLocalIso": f"2026-11-26T{hour}:00:00+08:00",
    }


class SlotOfferLimitTests(unittest.TestCase):
    def test_explicit_and_split_inline_offers_share_first_four_slots(self):
        limit = SlotOfferLimit(lambda ref: "Wed 25 Nov at 9:30" if ref == "S7" else None)
        views = {f"S{index}": slot(index + 10) for index in range(1, 7)}
        refs = {"type": "refs", "slots": views}
        proposal = {"type": "proposal", "proposal": {"slotRef": "S6"}}
        events = [
            refs,
            {"type": "text_delta", "text": "An earlier choice: [[S7]]. Others: [[S"},
            {"type": "slot_chips", "refs": ["S3", "S3", "S4"], "note": "Available times"},
            {"type": "text_delta", "text": "1]] or [[S2]]; again [[S3]]; missing [[S99]]."},
            {"type": "slot_chips", "refs": ["S5", "S6"]},
            proposal,
        ]
        result = [visible for event in events for visible in limit.apply(event)]
        self.assertEqual(result, [
            refs,
            {"type": "text_delta", "text": "An earlier choice: [[S7]]. Others:"},
            {"type": "slot_chips", "refs": ["S3", "S4"], "note": "Available times"},
            {"type": "text_delta", "text": " [[S1]] or Thu 26 Nov at 12:00; again Thu 26 Nov at 13:00; missing S99."},
            {"type": "slot_chips", "refs": [], "plainRefs": ["S5", "S6"]},
            proposal,
        ])

    def test_final_paragraph_does_not_complete_an_unfinished_marker(self):
        limit = SlotOfferLimit(lambda _: None)
        self.assertEqual(list(limit.apply({"type": "text_delta", "text": "Text [[S1]"})), [
            {"type": "text_delta", "text": "Text"},
        ])
        self.assertEqual(list(limit.apply({"type": "assistant_text", "text": "] Later"})), [
            {"type": "text_delta", "text": " [[S1]"},
            {"type": "assistant_text", "text": "] Later"},
        ])
        self.assertEqual(list(limit.finish()), [])

    def test_excluded_refs_do_not_consume_a_choice_and_overflow_does_not_repeat_ranges(self):
        limit = SlotOfferLimit(lambda _: None, excluded_ref=lambda ref: ref == "S6")
        views = {f"S{index}": slot(index + 10) for index in range(1, 7)}
        views["S5"] = {**views["S5"], "timeLabel": "15:00–15:30"}
        events = [
            {"type": "refs", "slots": views},
            {"type": "slot_chips", "refs": ["S6", "S1", "S2", "S3", "S4", "S5"]},
            {"type": "text_delta", "text": "15:00–15:30 ("},
            {"type": "text_delta", "text": "[[S5]]"},
            {"type": "text_delta", "text": ") ([[S6]])."},
        ]
        result = [visible for event in events for visible in limit.apply(event)]
        self.assertEqual(result[1], {"type": "slot_chips", "refs": ["S1", "S2", "S3", "S4"], "plainRefs": ["S5"]})
        self.assertEqual("".join(event["text"] for event in result if event["type"] == "text_delta"), "15:00–15:30.")
