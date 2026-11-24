"""Exercise scheduling recovery through signed turns and the Flask route."""

import json
import os
import unittest
from collections import deque
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from backend.cal_client import CalError
from backend.limits import RateLimits
from backend.model import ModelCompletion, ModelError, ModelText, ToolCall
from backend.protocol import ChatRequest
from backend.server import create_app
from backend.state import NoActionYetOutcome, PendingCall, new_state, sign_state, verify_state
from backend.turn import TurnService
from evals.fake_cal import DEFAULT_CLOCK, FakeCal


ZONE = "Asia/Singapore"
SECRET = "a-fictional-signing-secret-for-recovery-tests"


class ScriptedModel:
    def __init__(self, *turns):
        self.turns = deque(turns)
        self.requests = []

    async def stream(self, **request):
        self.requests.append(request)
        for event in self.turns.popleft():
            if isinstance(event, ModelError):
                raise event
            yield event


def reply(id="new-parent"):
    return ModelText("Here is the status."), ModelCompletion(id, "completed")


def text(value):
    return {"kind": "text", "text": value}


def wire(value, token=None):
    return {"stateToken": token, "timeZone": ZONE, "input": value}


def exception_event(events):
    return next(event for event in events if event["type"] == "error")


class ModelRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def service(self, model, **settings):
        calendar = settings.pop("calendar", FakeCal())
        service = TurnService(model=model, calendar=calendar, secret=SECRET, clock=lambda: DEFAULT_CLOCK, **settings)
        self.addCleanup(service.close_prefetch)
        return service

    async def turn(self, service, source, token=None):
        return [event async for event in service.events(ChatRequest.model_validate(wire(source, token)))]

    def state(self, events):
        return verify_state(events[-1]["stateToken"], SECRET, DEFAULT_CLOCK)

    async def test_pretext_503_timeout_and_429_each_get_one_backup_attempt(self):
        errors = (ModelError("model_down", 503), ModelError("timeout"), ModelError("rate_limited", 429))
        for error in errors:
            with self.subTest(code=error.code):
                primary, backup = ScriptedModel((error,)), ScriptedModel(reply("backup-parent"))
                events = await self.turn(self.service(primary, backup_model=backup), text("Hello"))
                metrics = events[-1]["metrics"]
                self.assertEqual(metrics["outcome"], "ok")
                self.assertEqual(metrics["fallback"], "backup_model")
                self.assertEqual([call["modelRole"] for call in metrics["modelCalls"]], ["primary", "backup"])
                self.assertEqual(primary.requests, backup.requests)
                self.assertEqual(len(primary.requests), 1)
                self.assertEqual(self.state(events).interaction_id, "backup-parent")

    async def test_case_23_keeps_real_chips_actionable_after_both_provider_failures(self):
        calendar = FakeCal()
        primary = ScriptedModel((ModelError("rate_limited", 429),))
        backup = ScriptedModel((ModelError("model_down", 503),))
        service = self.service(primary, backup_model=backup, calendar=calendar)
        events = await self.turn(service, text("30 min Thursday afternoon?"))
        metrics = events[-1]["metrics"]
        self.assertEqual(metrics["outcome"], "failed")
        self.assertEqual(metrics["fallback"], "code_only")
        self.assertEqual([call["modelRole"] for call in metrics["modelCalls"]], ["primary", "backup"])
        self.assertEqual(metrics["toolCalls"], [])
        self.assertIsNone(self.state(events).interaction_id)
        self.assertIn("here are open times for Thu 26 Nov", exception_event(events)["message"])
        self.assertFalse(any(event["type"] == "proposal" for event in events))
        chips = next(event for event in events if event["type"] == "slot_chips")
        self.assertLessEqual(1, len(chips["refs"]))
        self.assertLessEqual(len(chips["refs"]), 4)
        slots = self.state(events).slots
        self.assertTrue(set(chips["refs"]) <= slots.keys())
        starts = [datetime.fromisoformat(slots[ref].start_utc).astimezone(ZoneInfo(ZONE)) for ref in chips["refs"]]
        self.assertTrue(all(value.date().isoformat() == "2026-11-26" and 12 <= value.hour < 17 for value in starts))
        pick = {"kind": "ui", "action": {"type": "pick_slot", "slotRef": chips["refs"][0]}}
        selected = await self.turn(service, pick, events[-1]["stateToken"])
        self.assertEqual(selected[-1]["metrics"]["modelCalls"], [])
        self.assertEqual(next(event["proposal"]["kind"] for event in selected if event["type"] == "proposal"), "book")
        self.assertEqual(calendar.list_bookings()["data"], [])

    async def test_unverified_stored_backup_preserves_pending_history_and_lists_owned_bookings(self):
        primary, backup = ScriptedModel((ModelError("timeout"),)), ScriptedModel()
        calendar = FakeCal(fixture_overrides={"active_bookings": ["sam_intro", "other_intro"]})
        service = self.service(primary, backup_model=backup, backup_continuations=False, calendar=calendar)
        previous = new_state(ZONE, DEFAULT_CLOCK)
        previous.user.email = "sam@example.com"
        previous.counters.p = 1
        previous.interaction_id = "primary-parent"
        previous.pending_call = PendingCall(id="find-1", name="propose_cancel", proposal_id="P1",
                                            outcome=NoActionYetOutcome(outcome="no_action_yet", note="Waiting."))
        events = await self.turn(service, text("What meetings do I have?"), sign_state(previous, SECRET))
        metrics, state = events[-1]["metrics"], self.state(events)
        self.assertEqual(backup.requests, [])
        self.assertEqual(metrics["fallback"], "code_only")
        self.assertEqual(state.interaction_id, previous.interaction_id)
        self.assertEqual(state.pending_call, previous.pending_call)
        self.assertEqual(len(state.bookings), 1)
        self.assertIn("upcoming meetings", exception_event(events)["message"])

    async def test_verified_backup_can_close_the_same_pending_call(self):
        primary, backup = ScriptedModel((ModelError("timeout"),)), ScriptedModel(reply())
        service = self.service(primary, backup_model=backup)
        previous = new_state(ZONE, DEFAULT_CLOCK)
        previous.counters.p = 1
        previous.interaction_id = "primary-parent"
        previous.pending_call = PendingCall(id="pending", name="propose_cancel", proposal_id="P1")
        events = await self.turn(service, text("Hello"), sign_state(previous, SECRET))
        self.assertEqual(primary.requests, backup.requests)
        self.assertEqual([step["type"] for step in backup.requests[0]["input_steps"]], ["function_result", "user_input"])
        self.assertEqual(self.state(events).interaction_id, "new-parent")
        self.assertIsNone(self.state(events).pending_call)

    async def test_post_text_failure_refusal_and_expiration_do_not_retry(self):
        for script, code, booker in (
            ((ModelText("Partial text"), ModelError("model_down", 503)), "model_down", True),
            ((ModelError("refusal", 400),), "refusal", False),
            ((ModelError("interaction_expired", 404),), "interaction_expired", False),
        ):
            with self.subTest(code=code):
                primary, backup = ScriptedModel(script), ScriptedModel()
                service = self.service(primary, backup_model=backup)
                previous = new_state(ZONE, DEFAULT_CLOCK)
                previous.interaction_id = "old-parent"
                events = await self.turn(service, text("30 min Thursday afternoon?"), sign_state(previous, SECRET))
                error, metrics = exception_event(events), events[-1]["metrics"]
                self.assertEqual(error["code"], code)
                self.assertEqual(error.get("fallback") == "booker", booker)
                self.assertEqual(backup.requests, [])
                self.assertEqual(len(metrics["modelCalls"]), 1)
                self.assertNotIn("fallback", metrics)
                self.assertEqual(self.state(events).slots, {})
                self.assertEqual(self.state(events).interaction_id, None if code == "interaction_expired" else "old-parent")

    async def test_cal_errors_and_model_errors_without_usable_data_have_expected_fallbacks(self):
        class UnavailableCalendar(FakeCal):
            def __init__(self, code):
                super().__init__()
                self.code = code

            def me(self):
                raise CalError(self.code, "internal detail")

        for cal_code, expected, booker in (("unavailable", "cal_down", True), ("rate_limited", "rate_limited", False)):
            events = await self.turn(self.service(ScriptedModel(), calendar=UnavailableCalendar(cal_code)), text("Hello"))
            error = exception_event(events)
            self.assertEqual((error["code"], error.get("fallback") == "booker"), (expected, booker))
            self.assertEqual(events[-1]["metrics"]["modelCalls"], [])
        model = ScriptedModel((ModelError("model_down", 503),))
        events = await self.turn(self.service(model), text("Hello"))
        self.assertEqual(exception_event(events)["fallback"], "booker")
        self.assertEqual(len(model.requests), 1)

    async def test_invalid_tool_arguments_can_be_corrected_inside_the_turn(self):
        valid = {"from_date": "2026-11-26", "to_date": "2026-11-26", "part_of_day": "afternoon", "for_booking_ref": ""}
        model = ScriptedModel(
            (ModelCompletion("bad", "requires_action", (ToolCall("bad-call", "find_slots", {**valid, "from_date": 7}),)),),
            (ModelCompletion("good", "requires_action", (ToolCall("good-call", "find_slots", valid),)),),
            reply(),
        )
        events = await self.turn(self.service(model), text("Hello"))
        self.assertIn("error", model.requests[1]["input_steps"][0]["result"])
        self.assertEqual([call["ok"] for call in events[-1]["metrics"]["toolCalls"]], [False, True])
        self.assertEqual(events[-1]["metrics"]["outcome"], "ok")

    async def test_repeated_invalid_arguments_disable_only_that_tool(self):
        valid = {"from_date": "2026-11-26", "to_date": "2026-11-26", "part_of_day": "afternoon", "for_booking_ref": ""}
        model = ScriptedModel(
            (ModelCompletion("first", "requires_action", (ToolCall("first", "find_slots", {}),)),),
            (ModelCompletion("second", "requires_action", (ToolCall("second", "find_slots", {}),)),),
            (ModelCompletion("third", "requires_action", (ToolCall("third", "find_slots", valid),)),),
            reply(),
        )
        events = await self.turn(self.service(model), text("Hello"))
        available = {schema["name"] for schema in model.requests[2]["tools"]}
        self.assertNotIn("find_slots", available)
        self.assertIn("list_bookings", available)
        self.assertEqual([call["ok"] for call in events[-1]["metrics"]["toolCalls"]], [False, False, False])
        self.assertIn("correction limit", model.requests[-1]["input_steps"][0]["result"]["error"])

    async def test_daily_cap_counts_backup_and_still_allows_code_only_and_ui(self):
        primary, backup = ScriptedModel((ModelError("timeout"),)), ScriptedModel()
        service = self.service(primary, backup_model=backup, rate_limits=RateLimits(2, 1))
        events = await self.turn(service, text("30 min Thursday afternoon?"))
        self.assertEqual(backup.requests, [])
        self.assertEqual(events[-1]["metrics"]["fallback"], "code_only")
        self.assertEqual(exception_event(events)["code"], "rate_limited")
        slot = next(event["refs"][0] for event in events if event["type"] == "slot_chips")
        pick = {"kind": "ui", "action": {"type": "pick_slot", "slotRef": slot}}
        selection = await self.turn(service, pick, events[-1]["stateToken"])
        self.assertEqual(selection[-1]["metrics"]["outcome"], "ok")
        self.assertEqual(selection[-1]["metrics"]["modelCalls"], [])
        exhausted = await self.turn(service, text("Hello"), events[-1]["stateToken"])
        self.assertEqual(exhausted[-1]["metrics"]["modelCalls"], [])
        self.assertEqual(exception_event(exhausted)["code"], "rate_limited")


class RateLimitTests(unittest.TestCase):
    def test_sliding_limit_and_day_reset_in_pacific_time(self):
        marker = 0.0
        limits = RateLimits(2, 1, clock=lambda: marker)
        self.assertEqual(limits.request_retry_after("first"), 0)
        marker = 20
        self.assertEqual(limits.request_retry_after("first"), 0)
        self.assertEqual(limits.request_retry_after("first"), 580)
        self.assertEqual(limits.request_retry_after("second"), 0)
        marker = 600
        self.assertEqual(limits.request_retry_after("first"), 0)
        before = datetime.fromisoformat("2026-11-23T07:59:59+00:00")
        after = datetime.fromisoformat("2026-11-23T08:00:00+00:00")
        self.assertTrue(limits.claim_model_call(before))
        self.assertFalse(limits.claim_model_call(before))
        self.assertTrue(limits.claim_model_call(after))

    def test_flask_admission_ignores_client_forwarding_outside_vercel(self):
        model = ScriptedModel(reply("first"), reply("second"))
        service = TurnService(model=model, calendar=FakeCal(), secret=SECRET, clock=lambda: DEFAULT_CLOCK,
                              rate_limits=RateLimits(1, 500))
        self.addCleanup(service.close_prefetch)
        client = create_app(service).test_client()
        with patch.dict(os.environ, {"VERCEL": "0"}):
            first = client.post("/api/chat", json=wire(text("Hello")), buffered=True)
            blocked = client.post("/api/chat", json=wire(text("Again")),
                                  headers={"x-vercel-forwarded-for": "192.0.2.2"})
            accepted = client.post("/api/chat", json=wire(text("Hello")), buffered=True,
                                   environ_overrides={"REMOTE_ADDR": "192.0.2.3"})
        self.assertEqual((first.status_code, blocked.status_code, accepted.status_code), (200, 429, 200))
        self.assertEqual(blocked.json["code"], "rate_limited")
        self.assertEqual(int(blocked.headers["Retry-After"]), 600)
        self.assertEqual(json.loads(accepted.data.splitlines()[-1])["metrics"]["outcome"], "ok")

    def test_vercel_proxy_address_has_independent_admission(self):
        model = ScriptedModel(reply("first"), reply("second"))
        service = TurnService(model=model, calendar=FakeCal(), secret=SECRET, clock=lambda: DEFAULT_CLOCK,
                              rate_limits=RateLimits(1, 500))
        self.addCleanup(service.close_prefetch)
        client = create_app(service).test_client()
        with patch.dict(os.environ, {"VERCEL": "1"}):
            statuses = [client.post("/api/chat", json=wire(text("Hello")), buffered=True,
                                    headers={"x-vercel-forwarded-for": address}).status_code
                        for address in ("192.0.2.5", "192.0.2.6", "::ffff:192.0.2.5")]
        self.assertEqual(statuses, [200, 200, 429])
        self.assertEqual(len(model.requests), 2)

    def test_bad_token_resets_before_any_model_call(self):
        service = TurnService(model=ScriptedModel(), calendar=FakeCal(), secret=SECRET, clock=lambda: DEFAULT_CLOCK)
        self.addCleanup(service.close_prefetch)
        client = create_app(service).test_client()
        response = client.post("/api/chat", json=wire(text("Hello"), "not-a-valid-token"), buffered=True)
        events = [json.loads(line) for line in response.data.splitlines()]
        self.assertEqual(exception_event(events)["code"], "interaction_expired")
        self.assertEqual(events[-1]["metrics"]["modelCalls"], [])
        state = verify_state(events[-1]["stateToken"], SECRET, DEFAULT_CLOCK)
        self.assertIsNone(state.interaction_id)
        self.assertEqual(state.slots, {})

    def test_http_replays_confirm_with_the_same_signed_token_without_a_second_write(self):
        class CountingCalendar(FakeCal):
            writes = 0

            def create_booking(self, **kwargs):
                self.writes += 1
                return super().create_booking(**kwargs)

        calendar = CountingCalendar()
        service = TurnService(model=ScriptedModel(), calendar=calendar, secret=SECRET, clock=lambda: DEFAULT_CLOCK)
        self.addCleanup(service.close_prefetch)
        client = create_app(service).test_client()

        def send(action, token=None):
            response = client.post("/api/chat", json=wire({"kind": "ui", "action": action}, token), buffered=True)
            return [json.loads(line) for line in response.data.splitlines()]

        offered = send({"type": "more_slots", "fromDate": "2026-11-24"})
        slot = next(event["refs"][0] for event in offered if event["type"] == "slot_chips")
        selection = send({"type": "pick_slot", "slotRef": slot}, offered[-1]["stateToken"])
        proposed = next(event["proposal"]["id"] for event in selection if event["type"] == "proposal")
        action = {"type": "confirm_proposal", "proposalId": proposed, "name": "Avery Fixture", "email": "avery@example.com"}
        confirms = [send(action, selection[-1]["stateToken"]) for _ in range(2)]
        results = [next(event for event in events if event["type"] == "booking_result") for events in confirms]
        self.assertEqual(results[0], results[1])
        self.assertEqual(calendar.writes, 1)
        self.assertEqual(confirms[0][-1]["stateToken"], confirms[1][-1]["stateToken"])
