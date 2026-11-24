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
    def test_terminal_books_lists_moves_and_cancels_on_the_shared_service(self):
        calendar = FakeCal(fixture_overrides={"open_local_starts": ["2026-11-26T13:30", "2026-11-27T09:00"]})
        model = ScriptedModel(
            ("I can offer [", "[S1]", "].", "search"),
            ("Your meeting is [[B2]].", "listed"),
        )
        service = TurnService(model=model, calendar=calendar, clock=lambda: DEFAULT_CLOCK, secret=SECRET)
        answers = iter((
            "Can I grab a meeting Thursday afternoon?", "1", "Nova Fixture", "nova@example.com", "y",
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
        self.assertIn("Your meeting is 30 Minute Meeting · Fri 27 Nov", transcript)
        self.assertEqual(transcript.count("Cancel this meeting?"), 2)
        self.assertIn("Meeting cancelled", transcript)
        self.assertIn('"modelCalls": []', transcript)
        self.assertEqual(prompts.count("Name: "), 1)
        self.assertEqual(prompts.count("Email: "), 1)
        self.assertEqual(prompts.count("Confirm? [y/n] "), 4)
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
