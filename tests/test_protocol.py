import unittest

from pydantic import ValidationError

from backend.protocol import (
    MAX_MESSAGE_LENGTH,
    MAX_STATE_TOKEN_LENGTH,
    ChatRequest,
    ConfirmProposal,
    PickSlot,
    TextInput,
    UiInput,
)


def chat_request(message_or_action: dict, **overrides: object) -> dict:
    return {
        "stateToken": None,
        "timeZone": "Asia/Singapore",
        "input": message_or_action,
        **overrides,
    }


class ProtocolTests(unittest.TestCase):
    def test_text_request_keeps_wire_names_and_snake_case_attributes(self):
        original = chat_request({"kind": "text", "text": "Tomorrow morning"})
        request = ChatRequest.model_validate(original)

        self.assertIsNone(request.state_token)
        self.assertEqual(request.time_zone, "Asia/Singapore")
        self.assertIsInstance(request.input, TextInput)
        self.assertEqual(request.input.text, "Tomorrow morning")
        self.assertEqual(request.model_dump(by_alias=True), original)

    def test_typed_confirmation_round_trips_the_bound_card_and_contact(self):
        original = chat_request({"kind": "text", "text": "yes", "proposalId": "P1",
                                 "contact": {"name": "Sam", "email": ""}})
        request = ChatRequest.model_validate(original)
        self.assertEqual(request.input.proposal_id, "P1")
        self.assertEqual(request.model_dump(by_alias=True), original)

    def test_every_supported_ui_action(self):
        actions = [
            {"type": "pick_slot", "slotRef": "S9"},
            {"type": "more_slots"},
            {"type": "more_slots", "fromDate": "2026-11-23"},
            {"type": "confirm_proposal", "proposalId": "P4"},
            {
                "type": "confirm_proposal",
                "proposalId": "P6",
                "name": "Sam Test",
                "email": "sam@example.com",
                "notes": "Test booking",
            },
            {"type": "dismiss_proposal", "proposalId": "P3"},
            {"type": "request_cancel", "bookingRef": "B1"},
            {"type": "request_reschedule", "bookingRef": "B2"},
        ]

        for action in actions:
            with self.subTest(action=action):
                original = chat_request({"kind": "ui", "action": action}, stateToken="abc.def")
                request = ChatRequest.model_validate(original)

                self.assertIsInstance(request.input, UiInput)
                self.assertEqual(request.input.action.type, action["type"])
                self.assertEqual(request.model_dump(by_alias=True, exclude_none=True), original)
                if isinstance(request.input.action, PickSlot):
                    self.assertEqual(request.input.action.slot_ref, "S9")
                if isinstance(request.input.action, ConfirmProposal):
                    self.assertEqual(request.input.action.proposal_id, action["proposalId"])

    def test_wrong_types_unknown_fields_and_unsupported_variants_are_rejected(self):
        invalid_inputs = [
            {"kind": "text", "text": 12},
            {"kind": "text", "text": "Hi", "action": {"type": "more_slots"}},
            {"kind": "action", "action": {"type": "more_slots"}},
            {"kind": "ui", "action": {"type": "unknown_action"}},
            {"kind": "ui", "action": {"type": "pick_slot", "slotRef": "B1"}},
            {"kind": "ui", "action": {"type": "pick_slot", "slotRef": "S0"}},
            {"kind": "ui", "action": {"type": "pick_slot", "slotRef": "S9", "startUtc": "2026-11-23T10:00:00Z"}},
            {"kind": "ui", "action": {"type": "request_cancel", "bookingUid": "1-example"}},
            {"kind": "ui", "action": {"type": "confirm_proposal", "proposalId": "P1", "email": None}},
            {"kind": "ui", "action": {"type": "more_slots", "fromDate": None}},
            {"kind": "ui", "action": {"type": "more_slots", "fromDate": "2026-2-03"}},
            {"kind": "ui", "action": {"type": "more_slots", "fromDate": "2026-02-29"}},
        ]

        for invalid_input in invalid_inputs:
            with self.subTest(input=invalid_input), self.assertRaises(ValidationError):
                ChatRequest.model_validate(chat_request(invalid_input))

        for changes in (
            {"stateToken": 1},
            {"stateToken": "a" * (MAX_STATE_TOKEN_LENGTH + 1)},
            {"timeZone": "America/Atlantis"},
            {"timeZone": "../Europe/London"},
            {"timeZone": []},
            {"history": []},
        ):
            with self.subTest(field=next(iter(changes))), self.assertRaises(ValidationError):
                ChatRequest.model_validate(chat_request({"kind": "text", "text": "Hi"}, **changes))

    def test_message_length_limit_and_blank_messages(self):
        text = "x" * MAX_MESSAGE_LENGTH
        validated = ChatRequest.model_validate(chat_request({"kind": "text", "text": text}))
        self.assertEqual(validated.input.text, text)

        for invalid_text in ("", " \n ", text + "x"):
            with self.subTest(length=len(invalid_text)), self.assertRaises(ValidationError):
                ChatRequest.model_validate(chat_request({"kind": "text", "text": invalid_text}))
