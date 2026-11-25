"""Verify checked meeting ranges and explicit outside-hours choices end to end."""

from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from backend.demo_calendar import DemoCalendar
from backend.model import ModelCompletion, ToolCall
from backend.protocol import ChatRequest
from backend.state import verify_state
from backend.time import parse_instant
from backend.turn import TurnService


ZONE = "Asia/Singapore"
NOW = datetime(2026, 11, 25, 10, tzinfo=ZoneInfo(ZONE))
SECRET = "fixture-signing-key-for-closed-windows"


class NoModel:
    async def stream(self, **_):
        if False:
            yield
        raise AssertionError("This checked range should be handled without Gemini.")


class WindowModel:
    async def stream(self, **_):
        yield ModelCompletion("window", "requires_action", (ToolCall("check", "find_slots", {
            "from_date": "2026-12-12", "part_of_day": "any", "for_booking_ref": "",
            "start_time": "11:00", "end_time": "13:00", "duration_minutes": 120, "time_zone": ZONE,
        }),))


class ClosedWindowTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self, model=None):
        calendar = DemoCalendar(clock=lambda: NOW, time_zone=ZONE)
        service = TurnService(model=model or NoModel(), calendar=calendar, secret=SECRET, clock=lambda: NOW)
        self.addCleanup(service.close_prefetch)
        return service, calendar

    async def send(self, service, text=None, *, zone=ZONE, token=None, action=None, proposal_id=None):
        entry = {"kind": "ui", "action": action} if action else {"kind": "text", "text": text}
        if proposal_id:
            entry["proposalId"] = proposal_id
        request = ChatRequest.model_validate({"timeZone": zone, "stateToken": token, "input": entry})
        events = [event async for event in service.events(request)]
        state = verify_state(events[-1]["stateToken"], SECRET, NOW)
        return events, state

    def assert_lengths(self, state, refs):
        self.assertTrue(refs)
        for ref in refs:
            slot = state.slots[ref]
            self.assertEqual(parse_instant(slot.end_utc) - parse_instant(slot.start_utc), timedelta(hours=2))

    @staticmethod
    def reply(events):
        return " ".join(event["text"] for event in events if event["type"] == "assistant_text")

    async def test_new_meeting_phrasings_keep_the_full_span(self):
        service, _ = self.make_service()
        for text in (
            "Can I add to my schedule a meeting with Avery Example on 11 Dec from 1100 to 1300?",
            "Put a meeting with Avery Example on my calendar on 11 Dec 11:00-13:00",
            "I want to schedule a meeting with Avery Example on 11 Dec from 11 to 1",
            "Add a meeting with Avery Example on 11 Dec from 11 to 1",
            "Set up a meeting with Avery Example on 11 Dec from 11 to 1",
            "Pencil in a meeting with Avery Example on 11 Dec from 11 to 1",
            "Arrange a call with Avery Example on 11 Dec from 11 to 1",
        ):
            with self.subTest(text=text):
                events, state = await self.send(service, text)
                self.assertEqual(len(state.last_offered_slots), 1)
                self.assert_lengths(state, state.last_offered_slots)
                slot = state.slots[state.last_offered_slots[0]]
                self.assertEqual(parse_instant(slot.start_utc).astimezone(ZoneInfo(ZONE)).isoformat(), "2026-12-11T11:00:00+08:00")
                self.assertEqual(state.guest_draft.name, "Avery Example")
                self.assertFalse(any(event["type"] in {"error", "proposal", "booking_result"} for event in events))

    async def test_closed_saturday_offers_checked_two_hour_choices_in_both_zones(self):
        service, _ = self.make_service()
        for zone in (ZONE, "America/Los_Angeles"):
            with self.subTest(zone=zone):
                events, state = await self.send(
                    service, "Can I add to my schedule a meeting with Avery Example on 12 Dec from 1100 to 1300?", zone=zone,
                )
                self.assertIn("Sat 12 Dec has no bookable hours on your calendar.", self.reply(events))
                self.assert_lengths(state, state.last_offered_slots)
                starts = [parse_instant(state.slots[ref].start_utc).astimezone(ZoneInfo(zone)) for ref in state.last_offered_slots]
                self.assertTrue(any(start.day < 12 for start in starts))
                self.assertTrue(any(start.day > 12 for start in starts))
                if zone == ZONE:
                    self.assertTrue(all((start.hour, start.minute) == (11, 0) for start in starts))
                ref = state.outside_hours_offer
                self.assertNotIn(ref, state.last_offered_slots)
                self.assertTrue(state.slots[ref].outside_hours)
                self.assert_lengths(state, [ref])
                self.assertEqual(starts[0].tzinfo, ZoneInfo(zone))
                self.assertFalse(any(event["type"] == "proposal" for event in events))

    async def test_window_interpreted_by_model_uses_the_same_checked_alternatives(self):
        service, _ = self.make_service(WindowModel())
        events, state = await self.send(
            service, "Arrange a call with Avery Example on 12 Dec from 1100 to 1300 Singapore time.",
        )
        self.assertIn("Sat 12 Dec has no bookable hours", self.reply(events))
        self.assertEqual(len(events[-1]["metrics"]["modelCalls"]), 1)
        self.assert_lengths(state, state.last_offered_slots)
        self.assertTrue(state.slots[state.outside_hours_offer].outside_hours)

    async def test_busy_window_explains_overlap_and_offers_the_same_day(self):
        service, calendar = self.make_service()
        start = datetime(2026, 12, 11, 11, 30, tzinfo=ZoneInfo(ZONE))
        calendar.create_booking(event_type_id=9001, start=start, name="Jordan Example", email="jordan@example.com",
                                time_zone=ZONE)
        events, state = await self.send(service, "Add a meeting with Avery Example on 11 Dec from 11 to 1")
        self.assertIn("window on Fri 11 Dec is taken or outside your working hours", self.reply(events))
        self.assertIn("Jordan Example", self.reply(events))
        self.assert_lengths(state, state.last_offered_slots)
        self.assertTrue(all(parse_instant(state.slots[ref].start_utc).astimezone(ZoneInfo(ZONE)).day == 11
                            for ref in state.last_offered_slots))
        self.assertIsNone(state.outside_hours_offer)

    async def test_outside_hours_selection_requires_opt_in_contact_and_confirm(self):
        service, calendar = self.make_service()
        events, state = await self.send(service, "Add a meeting with Avery Example on 12 Dec from 11 to 1")
        offer = state.outside_hours_offer
        without_opt_in, _ = await self.send(service, token=events[-1]["stateToken"],
                                           action={"type": "pick_slot", "slotRef": offer})
        self.assertTrue(any(event.get("code") == "invalid_action" for event in without_opt_in))
        picked, _ = await self.send(service, "Book it anyway", token=events[-1]["stateToken"])
        self.assertIn("guest’s email address", self.reply(picked))
        self.assertFalse(any(event["type"] == "proposal" for event in picked))
        contact, _ = await self.send(service, "avery@example.com", token=picked[-1]["stateToken"])
        card = next(event["proposal"] for event in contact if event["type"] == "proposal")
        self.assertTrue(card["outsideHours"])
        self.assertEqual(card["durationMinutes"], 120)
        self.assertIn("Sat 12 Dec", card["whenLabel"])
        self.assertEqual(calendar.list_bookings(attendee_email="avery@example.com")["data"], [])
        confirmed, _ = await self.send(service, "yes", token=contact[-1]["stateToken"], proposal_id=card["id"])
        self.assertTrue(any(event["type"] == "booking_result" and event["kind"] == "booked" for event in confirmed))
