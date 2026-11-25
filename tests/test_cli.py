import asyncio
from io import StringIO
import os
import unittest
from unittest.mock import patch

from backend.cli import TerminalChat, detect_time_zone, run_chat
from backend.model import ModelCompletion, ModelText
from backend.turn import TurnService
from evals.fake_cal import DEFAULT_CLOCK, FakeCal


ZONE = "Asia/Singapore"
SECRET = "synthetic-terminal-test-signing-key-2026"
SLOT = {"dayLabel": "Thu 26 Nov", "timeLabel": "13:30–14:00", "startLocalIso": "2026-11-26T13:30:00+08:00"}


class ScriptedModel:
    def __init__(self, *messages):
        self.messages = iter(messages)
        self.requests = []
        self.close_loop = None

    async def stream(self, **request):
        self.requests.append((asyncio.get_running_loop(), request))
        message = next(self.messages)
        for chunk in message[:-1]:
            yield ModelText(chunk)
        yield ModelCompletion(message[-1], "completed")

    async def aclose(self):
        self.close_loop = asyncio.get_running_loop()


class TurnFixture:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    async def events(self, request):
        self.requests.append(request)
        for event in next(self.responses):
            yield event


def terminal(token="synthetic.token", *, outcome="ok", fallback=None):
    metrics = {"outcome": outcome, "modelCalls": []}
    if fallback is not None:
        metrics["fallback"] = fallback
    return {"type": "turn_end", "stateToken": token, "metrics": metrics}


class TerminalIntegrationTests(unittest.TestCase):
    def test_terminal_binds_confirmation_text_and_preserves_shortcuts(self):
        chat = TerminalChat(TurnFixture(), ZONE, read=lambda _: "", output=StringIO())
        chat.proposal = {"id": "P1", "kind": "book"}
        self.assertEqual(chat._input("yes please"), {"kind": "text", "text": "yes please", "proposalId": "P1"})
        self.assertEqual(chat._input("Actually, a different day"), {
            "kind": "text", "text": "Actually, a different day", "proposalId": "P1",
        })
        self.assertEqual(chat._input("y")["action"], {"type": "confirm_proposal", "proposalId": "P1"})
        self.assertEqual(chat._input("/cancel")["action"], {"type": "dismiss_proposal", "proposalId": "P1"})

    def test_terminal_books_lists_moves_and_cancels_on_the_shared_service(self):
        calendar = FakeCal(fixture_overrides={"open_local_starts": ["2026-11-26T13:30", "2026-11-27T09:00"]})
        model = ScriptedModel(
            ("I can offer [", "[S1]", "].", "search"),
            ("Your meeting is [[B2]].", "listed"),
        )
        service = TurnService(model=model, calendar=calendar, clock=lambda: DEFAULT_CLOCK, secret=SECRET)
        answers = iter((
            "Can I grab a meeting Thursday afternoon?", "1", "Nova Fixture", "nova@example.com", "yes please",
            "r", "pick Friday", "y", "What meetings do I have coming up?", "c", "n", "c", "y", "/debug", "/quit",
        ))
        output = StringIO()
        prompts = []

        def read(prompt):
            prompts.append(prompt)
            answer = next(answers)
            if answer == "pick Friday":
                return next(line[1:line.index("]")] for line in output.getvalue().splitlines()
                            if line.startswith("[") and "Fri 27 Nov" in line)
            return answer

        self.assertEqual(run_chat(service, read=read, output=output, time_zone=ZONE), 0)
        transcript = output.getvalue()
        self.assertIn("Fixture Host · Asia/Singapore", transcript)
        self.assertIn("[1] Thu 26 Nov", transcript)
        self.assertIn("Book this meeting?", transcript)
        self.assertIn("Meeting booked", transcript)
        self.assertIn("Move this meeting?", transcript)
        self.assertIn("Meeting moved", transcript)
        self.assertIn("Your meeting is [B2].", transcript)
        self.assertEqual(transcript.count("[B2] 30 Minute Meeting · Fri 27 Nov 2026, 09:00–09:30 · accepted"), 1)
        self.assertEqual(transcript.count("Cancel this meeting?"), 2)
        self.assertIn("Meeting cancelled", transcript)
        self.assertIn('"modelCalls": []', transcript)
        self.assertEqual(set(prompts), {"You: "})
        self.assertEqual(len(model.requests), 2)
        self.assertIs(model.requests[0][0], model.close_loop)
        self.assertIs(model.requests[1][0], model.close_loop)
        self.assertEqual(calendar.list_bookings(attendee_email="nova@example.com")["data"], [])
        self.assertEqual(len(calendar.list_bookings(attendee_email="nova@example.com", status="cancelled")["data"]), 2)

    def test_commands_and_invalid_options_do_not_consume_a_model_turn(self):
        model = ScriptedModel()
        service = TurnService(model=model, calendar=FakeCal(), clock=lambda: DEFAULT_CLOCK, secret=SECRET)
        answers = iter(("999", "r", "/tz Bad/Zone", "/tz UTC", "/debug", "/new", "/quit"))
        output = StringIO()
        self.assertEqual(run_chat(service, read=lambda _: next(answers), output=output, time_zone=ZONE), 0)
        self.assertEqual(model.requests, [])
        self.assertIn("Choose a displayed time option.", output.getvalue())
        self.assertIn("Enter an IANA time zone", output.getvalue())
        self.assertIn("Time zone: UTC", output.getvalue())
        self.assertIn("No turn metrics yet.", output.getvalue())
        self.assertIn("New chat started.", output.getvalue())

    def test_explicit_iana_environment_zone_takes_precedence(self):
        with patch.dict(os.environ, {"TZ": ":Asia/Singapore"}):
            self.assertEqual(detect_time_zone("UTC"), ZONE)


class TerminalEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_outside_hours_offer_uses_explicit_text_and_warns_on_the_proposal(self):
        outside = {**SLOT, "dayLabel": "Sat 28 Nov", "startLocalIso": "2026-11-28T13:30:00+08:00", "outsideHours": True}
        proposal = {"id": "P1", "kind": "book", "title": "Fixture appointment", "slotRef": "S1",
                    "whenLabel": "Sat 28 Nov, 13:30–14:00", "durationMinutes": 30,
                    "timeZone": ZONE, "outsideHours": True}
        service = TurnFixture([
            {"type": "refs", "slots": {"S1": outside}},
            {"type": "slot_chips", "refs": ["S1"], "allowMore": False},
            {"type": "assistant_text", "text": "You asked for [[S1]]."},
            terminal(),
        ], [{"type": "proposal", "proposal": proposal}, terminal()])
        output = StringIO()
        chat = TerminalChat(service, ZONE, read=lambda _: "", output=output)

        await chat.send({"kind": "text", "text": "Book Saturday at 13:30"})
        self.assertEqual(chat.choices, {})
        self.assertIsNone(chat._input("1"))
        self.assertIsNone(chat.proposal)
        self.assertIn("Outside your working hours · Sat 28 Nov 13:30–14:00", output.getvalue())
        self.assertIn("Type Book it anyway", output.getvalue())
        self.assertNotIn("[1]", output.getvalue())

        choice = chat._input("Book it anyway")
        self.assertEqual(choice, {"kind": "text", "text": "Book it anyway"})
        await chat.send(choice)
        self.assertEqual(chat._input("Book it anyway"), choice)
        self.assertIn("Outside your working hours · Sat 28 Nov\nType yes or no", output.getvalue())
        self.assertEqual(chat._input("yes")["proposalId"], "P1")

    async def test_inline_number_reuses_immediately_preceding_time_range(self):
        second = {**SLOT, "timeLabel": "14:30–15:00"}
        service = TurnFixture([
            {"type": "refs", "slots": {"S1": SLOT, "S2": second}},
            {"type": "text_delta", "text": "Thu 26 Nov\n13:30—14:"},
            {"type": "text_delta", "text": "00 ("},
            {"type": "text_delta", "text": "[[S"},
            {"type": "text_delta", "text": "1]]"},
            {"type": "text_delta", "text": ")\n2:30 to 3:00 p.m. "},
            {"type": "text_delta", "text": "[[S2]]."},
            terminal(),
        ])
        output = StringIO()
        chat = TerminalChat(service, ZONE, read=lambda _: "", output=output)

        await chat.send({"kind": "text", "text": "What times are free?"})

        self.assertEqual(output.getvalue(), "Assistant: Thu 26 Nov\n13:30—14:00 [1]\n2:30 to 3:00 p.m. [2].\n")
        self.assertEqual(chat.choices, {1: "S1", 2: "S2"})
        self.assertEqual(chat._input("2"), {"kind": "ui", "action": {"type": "pick_slot", "slotRef": "S2"}})

    async def test_inline_booking_omits_only_the_immediately_repeated_range(self):
        booking = {"title": "Fixture appointment", "whenLabel": "Thu 26 Nov, 12:00–12:30"}
        service = TurnFixture([
            {"type": "refs", "bookings": {"B1": booking}},
            {"type": "text_delta", "text": "You booked from 12:00–12:30 ("},
            {"type": "text_delta", "text": "[[B1]])"},
            {"type": "text_delta", "text": ". A 12:00–13:00 range differs from [[B1]]."},
            terminal(),
        ])
        output = StringIO()
        chat = TerminalChat(service, ZONE, read=lambda _: "", output=output)

        await chat.send({"kind": "text", "text": "Which meeting did I book?"})

        self.assertEqual(output.getvalue(), "Assistant: You booked from 12:00–12:30 (Fixture appointment · Thu 26 Nov). "
                         "A 12:00–13:00 range differs from Fixture appointment · Thu 26 Nov, 12:00–12:30.\n")

    async def test_plain_times_do_not_receive_numbers_and_previous_choices_stay_available(self):
        overflow = {"dayLabel": "Fri 27 Nov", "timeLabel": "09:00", "startLocalIso": "2026-11-27T09:00:00+08:00"}
        service = TurnFixture([
            {"type": "refs", "slots": {"S2": SLOT, "S6": overflow}},
            {"type": "slot_chips", "refs": ["S2"], "plainRefs": ["S6"]},
            terminal(),
        ])
        output = StringIO()
        chat = TerminalChat(service, ZONE, read=lambda _: "", output=output)
        chat.slots = {"S1": SLOT}
        chat.choices = {1: "S1"}
        chat.next_choice = 2

        await chat.send({"kind": "text", "text": "Show more slots"})

        self.assertEqual(chat.choices, {1: "S1", 2: "S2"})
        self.assertIn("Fri 27 Nov 09:00", output.getvalue().splitlines())
        self.assertIn("S6", chat.slots)

    async def test_booking_refs_support_inline_text_and_explicit_cards_control_list_output(self):
        booking = {"title": "Inventory appointment", "whenLabel": "Thu 26 Nov, 13:30–14:00", "status": "accepted"}
        offered = {**booking, "title": "Listed appointment"}
        earlier = {**booking, "title": "Earlier listed appointment"}
        service = TurnFixture(
            [{"type": "refs", "bookings": {"B1": booking, "B2": offered, "B3": earlier}}, terminal()],
            [{"type": "booking_cards", "refs": ["B3", "B2"]},
             {"type": "text_delta", "text": "Check [[B2]] first."}, terminal()],
            [{"type": "assistant_text", "text": "You chose [[B1]]."}, terminal()],
        )
        output = StringIO()
        chat = TerminalChat(service, ZONE, read=lambda _: "", output=output)

        await chat.send({"kind": "text", "text": "Show context"})
        self.assertEqual(output.getvalue(), "")
        await chat.send({"kind": "text", "text": "List meetings"})
        self.assertEqual(output.getvalue().splitlines(), [
            "Assistant: Check [B2] first.",
            "[B3] Earlier listed appointment · Thu 26 Nov, 13:30–14:00 · accepted",
            "[B2] Listed appointment · Thu 26 Nov, 13:30–14:00 · accepted",
        ])
        self.assertNotIn("Inventory appointment", output.getvalue())
        await chat.send({"kind": "text", "text": "What did I choose?"})
        self.assertIn("You chose Inventory appointment · Thu 26 Nov, 13:30–14:00.", output.getvalue())

    async def test_retired_refs_drop_booking_lookup_even_when_the_turn_fails(self):
        old = {"title": "Retired appointment", "whenLabel": "Thu 26 Nov, 13:30–14:00", "status": "accepted"}
        current = {**old, "title": "Current appointment"}
        service = TurnFixture(
            [{"type": "refs", "bookings": {"B1": old, "B2": current}},
             {"type": "text_delta", "text": "Look at [[B1]]."}, terminal()],
            [{"type": "refs", "retiredBookings": ["B1"]},
             {"type": "text_delta", "text": "I can see [[B2]]."}, terminal(outcome="failed")],
            [{"type": "text_delta", "text": "You mentioned [[B1]]."}, terminal()],
        )
        output = StringIO()
        chat = TerminalChat(service, ZONE, read=lambda _: "", output=output)

        await chat.send({"kind": "text", "text": "What do I have?"})
        chat.latest_booking = "B1"
        await chat.send({"kind": "text", "text": "Cancel the old booking"})
        await chat.send({"kind": "text", "text": "Which booking?"})

        self.assertEqual(chat.bookings, {"B2": current})
        self.assertIsNone(chat.latest_booking)
        self.assertEqual(output.getvalue().count("Retired appointment"), 1)
        self.assertIn("I can see Current appointment", output.getvalue())

    async def test_failed_text_choices_are_not_selectable_and_split_refs_do_not_flash(self):
        service = TurnFixture([
            {"type": "refs", "slots": {"S1": SLOT}},
            {"type": "text_delta", "text": "Try [["},
            {"type": "text_delta", "text": "S1]]."},
            {"type": "error", "code": "model_down", "message": "Try again.", "retryable": True},
            terminal(outcome="failed"),
        ])
        output = StringIO()
        chat = TerminalChat(service, ZONE, read=lambda _: "", output=output)
        await chat.send({"kind": "text", "text": "Look Thursday"})
        self.assertIn("Try [1] Thu 26 Nov", output.getvalue())
        self.assertNotIn("[[", output.getvalue())
        self.assertEqual(chat.choices, {})
        self.assertIsNone(chat._input("1"))

    async def test_both_slot_actions_keep_only_the_signed_recovery_options(self):
        for action in ({"type": "confirm_proposal", "proposalId": "P1"}, {"type": "pick_slot", "slotRef": "S1"}):
            with self.subTest(action=action["type"]):
                service = TurnFixture([
                    {"type": "refs", "slots": {"S2": SLOT, "S3": SLOT}},
                    {"type": "slot_chips", "refs": ["S2"]},
                    {"type": "error", "code": "slot_taken", "message": "That time was just taken.", "retryable": False},
                    terminal("recovery.token", outcome="failed"),
                ])
                chat = TerminalChat(service, ZONE, read=lambda _: "", output=StringIO())
                chat.slots = {"S1": SLOT}
                chat.choices = {1: "S1"}
                chat.next_choice = 2
                await chat.send({"kind": "ui", "action": action})
                self.assertEqual(chat.token, "recovery.token")
                self.assertEqual(chat.choices, {2: "S2"})
                self.assertIsNone(chat._input("1"))
                self.assertEqual(chat._input("2"), {"kind": "ui", "action": {"type": "pick_slot", "slotRef": "S2"}})

    async def test_later_text_resolves_booking_refs_from_confirmation_results(self):
        booking = {"ref": "B1", "title": "Fixture appointment", "whenLabel": "Thu 26 Nov, 13:30–14:00",
                   "status": "accepted", "attendees": ["nova@example.com"], "startUtc": "2026-11-26T05:30:00Z",
                   "endUtc": "2026-11-26T06:00:00Z"}
        service = TurnFixture(
            [{"type": "booking_result", "kind": "booked", "booking": booking}, terminal()],
            [{"type": "text_delta", "text": "You chose [[B1]]."}, terminal()],
        )
        output = StringIO()
        chat = TerminalChat(service, ZONE, read=lambda _: "", output=output)
        await chat.send({"kind": "ui", "action": {"type": "confirm_proposal", "proposalId": "P1"}})
        await chat.send({"kind": "text", "text": "What did I choose?"})
        self.assertIn("You chose Fixture appointment · Thu 26 Nov, 13:30–14:00.", output.getvalue())

    async def test_can_retry_verified_noop_but_cannot_replay_an_uncertain_confirmation(self):
        proposal = {"id": "P1", "kind": "reschedule", "title": "Fixture meeting", "whenLabel": "Thu 26 Nov, 13:30",
                    "timeZone": ZONE, "durationMinutes": 30, "slotRef": "S1", "bookingRef": "B1"}
        source = {"kind": "ui", "action": {"type": "confirm_proposal", "proposalId": "P1"}}
        error = {"type": "error", "message": "Fixture write error"}
        service = TurnFixture(
            [{**error, "code": "write_not_applied", "retryable": True}, terminal(outcome="failed")],
            [{**error, "code": "write_unverified", "retryable": False}, terminal(outcome="failed")],
        )
        chat = TerminalChat(service, ZONE, read=lambda _: "", output=StringIO())
        chat.proposal = proposal
        chat.latest_booking = "B1"
        chat.slots = {"S1": SLOT}
        chat.choices = {1: "S1"}

        await chat.send(source)
        self.assertEqual(chat._input("Retry"), source)
        self.assertEqual(chat.proposal, proposal)
        await chat.send(source)
        self.assertIsNone(chat.retry_input)
        self.assertIsNone(chat.proposal)
        self.assertIsNone(chat.latest_booking)
        self.assertEqual(chat.choices, {})

    async def test_code_only_fallback_keeps_signed_choices(self):
        source = {"kind": "text", "text": "Look Thursday"}
        service = TurnFixture([
            {"type": "refs", "slots": {"S1": SLOT}},
            {"type": "slot_chips", "refs": ["S1"]},
            {"type": "error", "code": "model_down", "message": "Use these times.", "retryable": True},
            terminal(outcome="failed", fallback="code_only"),
        ])
        chat = TerminalChat(service, ZONE, read=lambda _: "", output=StringIO())
        await chat.send(source)
        self.assertEqual(chat._input("Retry"), source)
        self.assertEqual(chat._input("1"), {"kind": "ui", "action": {"type": "pick_slot", "slotRef": "S1"}})
