from datetime import datetime
import unittest

from backend.demo_calendar import DemoCalendar
from backend.model import ModelCompletion, ModelText, ToolCall
from backend.protocol import ChatRequest
from backend.state import verify_state
from backend.turn import TurnService
from evals.fake_cal import DEFAULT_CLOCK, FakeCal


ZONE = "Asia/Singapore"
NOW = DEFAULT_CLOCK
SECRET = "fixture-signing-key-for-scheduling-followup"


class FakeModel:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.requests = []

    async def stream(self, **request):
        self.requests.append(request)
        for chunk in next(self.replies):
            yield chunk


class SchedulingFollowupTests(unittest.IsolatedAsyncioTestCase):
    def service(self, model, calendar=None):
        service = TurnService(model=model, calendar=calendar or DemoCalendar(clock=lambda: NOW, time_zone=ZONE),
                              secret=SECRET, clock=lambda: NOW)
        self.addCleanup(service.close_prefetch)
        return service

    async def turn(self, service, text, token=None):
        request = ChatRequest.model_validate({"timeZone": ZONE, "stateToken": token,
                                              "input": {"kind": "text", "text": text}})
        return [event async for event in service.events(request)]

    @staticmethod
    def chosen_starts(events):
        slots = {ref: value for event in events if event["type"] == "refs" for ref, value in event.get("slots", {}).items()}
        return [slots[ref]["startLocalIso"][:16] for event in events if event["type"] == "slot_chips" for ref in event["refs"]]

    @staticmethod
    def find(call_id, day, at, **extra):
        call = ToolCall(call_id, "find_slots", {"from_date": day, "start_time": at, "part_of_day": "any",
                                              "for_booking_ref": "", **extra})
        return ModelCompletion(call_id, "requires_action", (call,))

    async def test_a_lookup_checks_the_interpreted_clock_and_never_shows_unchecked_prose(self):
        model = FakeModel((ModelText("I’m sure 16:00 is unavailable."), self.find("read", "2026-11-24", "12:00")))
        events = await self.turn(self.service(model), "Tomorrow at noon")
        self.assertEqual(self.chosen_starts(events), ["2026-11-24T12:00"])
        self.assertFalse(any("unavailable" in event.get("text", "") for event in events))
        self.assertEqual(events[-1]["metrics"]["prefetch"]["shownSlots"], 0)
        self.assertEqual(len(model.requests), 1)

    async def test_a_missing_lookup_cannot_generate_a_claim(self):
        model = FakeModel((ModelText("Your entire afternoon is full."), ModelCompletion("done", "completed")))
        events = await self.turn(self.service(model), "Can you find time tomorrow at noon?")
        self.assertEqual(self.chosen_starts(events), [])
        self.assertEqual([event["text"] for event in events if event["type"] == "assistant_text"],
                         ["Which date and time should I check?"])

    async def test_named_timezone_is_converted_even_when_closed(self):
        model = FakeModel((self.find("read", "2026-11-27", "09:00", time_zone="Asia/Tokyo"),))
        events = await self.turn(self.service(model), "Check Friday at 9am Tokyo time")
        self.assertEqual(self.chosen_starts(events), [])
        reply = next(event["text"] for event in events if event["type"] == "assistant_text")
        self.assertIn("Fri 27 Nov 2026 at 08:00 (Asia/Singapore)", reply)

    async def test_cross_midnight_followup_keeps_the_requested_zone_and_date(self):
        model = FakeModel((self.find("late", "2026-11-27", "00:30", time_zone="Asia/Tokyo"),),
                          (self.find("morning", "2026-11-27", "10:00"),))
        service = self.service(model)
        events = await self.turn(service, "Check Friday at 12:30am Tokyo time")
        state = verify_state(events[-1]["stateToken"], SECRET, NOW)
        self.assertEqual((state.last_slot_search.day, state.last_slot_search.time_zone, state.last_slot_search.at),
                         ("2026-11-27", "Asia/Tokyo", "00:30"))
        next_events = await self.turn(service, "Can we check 10 instead?", events[-1]["stateToken"])
        self.assertEqual(self.chosen_starts(next_events), ["2026-11-27T09:00"])

    async def test_explicit_zone_in_followup_can_replace_previous_zone(self):
        model = FakeModel((self.find("friday", "2026-11-27", "12:00"),),
                          (self.find("tokyo", "2026-11-27", "12:00", time_zone="Asia/Tokyo"),))
        service = self.service(model)
        first = await self.turn(service, "Check Friday at noon")
        state = verify_state(first[-1]["stateToken"], SECRET, NOW)
        self.assertEqual(state.last_slot_search.time_zone, ZONE)

        revised = await self.turn(service, "How about noon Tokyo time?", first[-1]["stateToken"])
        self.assertEqual(self.chosen_starts(revised), ["2026-11-27T11:00"])
        self.assertEqual([call["ok"] for call in revised[-1]["metrics"]["toolCalls"]], [True])

    async def test_unsupported_request_retires_a_prior_confirmation(self):
        model = FakeModel()
        service = self.service(model)
        existing = await self.turn(service, "Book tomorrow at 11am with Avery Example, avery@example.com")
        card = next(event["proposal"] for event in existing if event["type"] == "proposal")
        events = await self.turn(service, "Invite Rowan Vale and Riley Frost to that meeting too", existing[-1]["stateToken"])
        self.assertEqual([event["text"] for event in events if event["type"] == "assistant_text"],
                         ["I can book a meeting with one guest. Which guest should I use?"])
        self.assertTrue(any(event.get("retiredProposal") == card["id"] for event in events))
        state = verify_state(events[-1]["stateToken"], SECRET, NOW)
        self.assertIsNone(state.proposal)
        self.assertFalse(model.requests)

    async def test_reschedule_draft_day_keeps_guest_and_uses_new_openings(self):
        model = FakeModel(*[(ModelText("Thanks."), ModelCompletion(str(index), "completed")) for index in range(2)])
        service = self.service(model)
        first = await self.turn(service, "Book a call with Avery Example on Friday")
        moved = await self.turn(service, "Actually make it Monday", first[-1]["stateToken"])
        starts = self.chosen_starts(moved)
        self.assertTrue(starts)
        self.assertTrue(all(datetime.fromisoformat(stamp).weekday() == 0 for stamp in starts))
        state = verify_state(moved[-1]["stateToken"], SECRET, NOW)
        self.assertEqual(state.guest_draft.name, "Avery Example")
        self.assertIsNone(state.proposal)

    async def test_time_only_revision_uses_busy_requests_date_and_verifies_new_slot(self):
        model = FakeModel((self.find("wrong-day", "2026-11-23", "16:00"),),
                          (self.find("right-day", "2026-11-24", "16:00"),))
        service = self.service(model, FakeCal(fixture_overrides={"active_bookings": ["owner_tomorrow_late"]}))
        first = await self.turn(service, "Book tomorrow at 3pm with Avery Example, avery@example.com")
        state = verify_state(first[-1]["stateToken"], SECRET, NOW)
        self.assertEqual((state.last_slot_search.day, state.last_slot_search.at), ("2026-11-24", "15:00"))
        revised = await self.turn(service, "Can we do 4 instead?", first[-1]["stateToken"])
        self.assertIn("2026-11-24T16:00", self.chosen_starts(revised))
        self.assertEqual([call["ok"] for call in revised[-1]["metrics"]["toolCalls"]], [False, True])
        self.assertTrue(any(event["type"] == "proposal" for event in revised))
        self.assertFalse(any(event["type"] == "booking_result" for event in revised))

    async def test_named_next_meeting_displays_one_card(self):
        model = FakeModel()
        events = await self.turn(self.service(model), "When is my next meeting with Riley?")
        bookings = next(event["bookings"] for event in events if "bookings" in event)
        cards = next(event["refs"] for event in events if event["type"] == "booking_cards")
        self.assertEqual(len(cards), 1)
        self.assertEqual(bookings[cards[0]]["guestName"], "Riley Brooks")
        self.assertFalse(model.requests)

    async def test_full_name_does_not_match_another_guest_with_the_same_first_name(self):
        events = await self.turn(self.service(FakeModel()), "When is my next meeting with Riley Frost?")
        self.assertFalse(any(event["type"] == "booking_cards" for event in events))
        self.assertTrue(any("couldn’t find" in event.get("text", "") for event in events))

    async def test_multiple_lookups_cannot_propose_an_unchecked_selection(self):
        first = self.find("one", "2026-11-24", "12:30")
        second = self.find("two", "2026-11-24", "08:00")
        model = FakeModel((ModelCompletion("two-checks", "requires_action", (*first.tool_calls, *second.tool_calls)),))
        events = await self.turn(self.service(model), "Book a meeting tomorrow around noon with Avery Example, avery@example.com")
        self.assertFalse(any(event["type"] == "proposal" for event in events))
        self.assertEqual(self.chosen_starts(events), ["2026-11-24T12:30"])
