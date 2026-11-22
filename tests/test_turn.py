import asyncio
from datetime import datetime
import json
from threading import Event, current_thread
import unittest

from backend.model import ModelCompletion, ModelError, ModelText, ToolCall
from backend.protocol import ChatRequest
from backend.server import create_app
from backend.state import verify_state
from backend.turn import TurnService
from evals.fake_cal import FakeCal


NOW = datetime.fromisoformat("2026-11-23T02:00:00Z")
ZONE = "Asia/Singapore"
SECRET = "a-fictitious-signing-key-for-tests-only"
FIND = {"from_date": "2026-11-24", "to_date": "2026-11-24", "part_of_day": "any", "for_booking_ref": ""}


class ScriptedModel:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    async def stream(self, **request):
        self.requests.append(request)
        for step in next(self.responses):
            if isinstance(step, ModelError):
                raise step
            yield step


def service(model, calendar=None):
    return TurnService(model=model, calendar=calendar or FakeCal(clock=lambda: NOW), secret=SECRET, clock=lambda: NOW)


def request(input_value, token=None):
    return ChatRequest.model_validate({"stateToken": token, "timeZone": ZONE, "input": input_value})


async def collect(turn_service, value, token=None):
    return [event async for event in turn_service.events(request(value, token))]


def text(value):
    return {"kind": "text", "text": value}


def turn_state(reply):
    return verify_state(reply[-1]["stateToken"], SECRET, NOW)


class TurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_proposal_only_writes_after_confirm_and_retry_uses_last_good_context(self):
        find = ModelCompletion("read-parent", "requires_action", (ToolCall("call-read", "find_slots", FIND),))
        proposal = ModelCompletion("proposal-parent", "requires_action", (
            ToolCall("call-propose", "propose_booking", {
                "slot_ref": "S1", "name": "", "email": "", "notes": "",
            }),
        ))
        model = ScriptedModel(
            (find,), (ModelText("I found [[S1]]."), proposal),
            (ModelError("timeout"),), (ModelText("It's on your schedule."), ModelCompletion("new-parent", "completed")),
        )
        calendar = FakeCal(clock=lambda: NOW)
        turn_service = service(model, calendar)
        reply = await collect(turn_service, text("Book tomorrow at 10am"))
        card = next(item["proposal"] for item in reply if item["type"] == "proposal")
        self.assertIn("24 Nov 2026", card["whenLabel"])
        self.assertEqual(calendar.list_bookings()["data"], [])
        self.assertEqual(turn_state(reply).pending_call.id, "call-propose")
        self.assertEqual(model.requests[1]["input_steps"][0]["call_id"], "call-read")

        confirmed = await collect(turn_service, {"kind": "ui", "action": {
            "type": "confirm_proposal", "proposalId": card["id"], "name": "Fixture Guest", "email": "guest@example.com",
        }}, reply[-1]["stateToken"])
        self.assertEqual([item["kind"] for item in confirmed if item["type"] == "booking_result"], ["booked"])
        self.assertEqual(confirmed[-1]["metrics"]["modelCalls"], [])
        self.assertEqual(len(calendar.list_bookings()["data"]), 1)
        self.assertEqual(turn_state(confirmed).pending_call.outcome.outcome, "confirmed")

        followup = text("Can you confirm the booking is on the calendar?")
        failed = await collect(turn_service, followup, confirmed[-1]["stateToken"])
        self.assertEqual(failed[-2]["code"], "timeout")
        self.assertEqual(turn_state(failed), turn_state(confirmed))
        retried = await collect(turn_service, followup, failed[-1]["stateToken"])
        self.assertEqual(turn_state(retried).interaction_id, "new-parent")
        self.assertIsNone(turn_state(retried).pending_call)
        self.assertEqual(turn_state(retried).notes, [])
        for sent in model.requests[-2:]:
            self.assertEqual(sent["previous_interaction_id"], "proposal-parent")
            self.assertEqual([step["type"] for step in sent["input_steps"]], ["function_result", "user_input"])
            self.assertEqual(sent["input_steps"][0]["result"]["outcome"], "confirmed")

    async def test_parallel_reads_are_returned_in_one_request(self):
        model = ScriptedModel((ModelCompletion("parallel", "requires_action", (
            ToolCall("find", "find_slots", FIND),
            ToolCall("list", "list_bookings", {"email": "sam@example.com", "which": "upcoming"}),
        )),), (ModelText("Here are your options."), ModelCompletion("done", "completed")))
        reply = await collect(service(model), text("Find tomorrow's availability and bookings for sam@example.com"))
        self.assertEqual(reply[-1]["metrics"]["outcome"], "ok")
        self.assertEqual([item["call_id"] for item in model.requests[1]["input_steps"]], ["find", "list"])
        self.assertEqual(reply[-1]["metrics"]["toolCalls"][0]["ok"], True)

    async def test_loop_stops_after_four_model_calls(self):
        repeated = (ModelCompletion("next", "requires_action", (ToolCall("find", "find_slots", FIND),)),)
        model = ScriptedModel(*(repeated for _ in range(4)))
        reply = await collect(service(model), text("Find open slots tomorrow"))
        self.assertEqual(reply[-2]["code"], "loop_limit")
        self.assertIsNone(turn_state(reply).interaction_id)
        self.assertEqual(len(model.requests), 4)

    async def test_ambiguous_proposal_is_rejected_when_grouped_with_a_read(self):
        model = ScriptedModel((ModelCompletion("pending", "requires_action", (
            ToolCall("find", "find_slots", FIND), ToolCall("propose", "propose_booking", {}),
        )),), (ModelText("An opening is available."), ModelCompletion("done", "completed")))
        reply = await collect(service(model), text("Check tomorrow"))
        self.assertFalse(any(event["type"] == "proposal" for event in reply))
        self.assertIn("error", model.requests[1]["input_steps"][1]["result"])


class StreamTests(unittest.TestCase):
    def test_client_disconnect_cancels_model_and_joins_worker(self):
        class WaitingModel:
            started, cancelled = Event(), Event()
            task = thread = None

            async def stream(self, **_request):
                self.task, self.thread = asyncio.current_task(), current_thread()
                self.started.set()
                try:
                    await asyncio.Event().wait()
                    yield ModelText("Unreachable")
                finally:
                    self.cancelled.set()

        model = WaitingModel()
        client = create_app(service(model)).test_client()
        response = client.post("/api/chat", json={"stateToken": None, "timeZone": ZONE, "input": text("Hi")},
                               buffered=False)
        try:
            self.assertTrue(response.content_type.startswith("application/x-ndjson"))
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(json.loads(next(response.response))["type"], "status")
            self.assertTrue(model.started.wait(2))
        finally:
            response.close()
        self.assertTrue(model.cancelled.is_set())
        self.assertIsNotNone(model.thread)
        self.assertFalse(model.thread.is_alive())
        self.assertTrue(model.task.done())
        self.assertTrue(model.task.cancelled())

    def test_invalid_request_returns_400_without_model(self):
        model = ScriptedModel()
        response = create_app(service(model)).test_client().post(
            "/api/chat", json={"stateToken": None, "timeZone": "Bad/Zone", "input": text("Hi")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["code"], "invalid_request")
        self.assertEqual(model.requests, [])
