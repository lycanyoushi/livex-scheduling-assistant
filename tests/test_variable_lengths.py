"""Meetings of any length the event type offers, from search through confirmation."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
import json
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from backend.actions import ActionError, CalendarActions
from backend.cal_client import CalClient, CalError, event_lengths
from backend.demo_calendar import DEMO_LENGTHS, DemoCalendar
from backend.protocol import ChatRequest, ConfirmProposal
from backend.state import BookingTarget, GuestDraft, Proposal, add_booking, new_state, sign_state, verify_state
from backend.time import parse_instant
from backend.tools import CalendarTools, ToolRejected, booking_entry, describe_lengths, meeting_title
from backend.turn import TurnService
from evals.fake_cal import FakeCal
from scripts import set_lengths


ZONE = "Asia/Singapore"
# Wednesday morning, so the demo seeds today, Thursday and Friday.
NOW = datetime(2026, 11, 25, 10, 0, tzinfo=ZoneInfo(ZONE))


def find(day, **extra):
    return {"from_date": day, "part_of_day": "any", "for_booking_ref": "", **extra}


def local_range(row):
    return row["startLocalIso"][11:16], row["endLocalIso"][11:16]


class Transport:
    def __init__(self, *bodies):
        self.bodies = iter(bodies)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        return 200, json.dumps(next(self.bodies)).encode()


class CalClientLengthTests(unittest.TestCase):
    def test_slots_send_duration_and_cache_each_length_separately(self):
        transport = Transport({"status": "success", "data": {}}, {"status": "success", "data": {}})
        client = CalClient("key", transport=transport)
        client.slots(9001, "2026-12-09", "2026-12-09", ZONE, duration=60)
        client.slots(9001, "2026-12-09", "2026-12-09", ZONE)
        self.assertEqual(len(transport.requests), 2)
        self.assertIn("duration=60", transport.requests[0].full_url)
        self.assertNotIn("duration", transport.requests[1].full_url)

    def test_create_booking_sends_length_only_when_given(self):
        booking = {"uid": "u1", "title": "Meeting", "status": "accepted",
                   "start": "2026-12-09T03:00:00Z", "end": "2026-12-09T05:00:00Z"}
        transport = Transport(*({"status": "success", "data": booking} for _ in range(2)))
        client = CalClient("key", transport=transport)
        common = {"start": "2026-12-09T03:00:00Z", "event_type_id": 9001, "name": "Avery Fixture",
                  "email": "avery@example.com", "time_zone": ZONE}
        client.create_booking(**common, length_in_minutes=120)
        client.create_booking(**common)
        self.assertEqual(json.loads(transport.requests[0].data)["lengthInMinutes"], 120)
        self.assertNotIn("lengthInMinutes", json.loads(transport.requests[1].data))

    def test_update_event_type_patches_lengths_with_the_event_type_version(self):
        event = {"id": 9001, "slug": "meeting", "title": "Meeting", "lengthInMinutes": 30,
                 "lengthInMinutesOptions": [15, 30, 60]}
        transport = Transport({"status": "success", "data": event})
        client = CalClient("key", transport=transport)
        updated = client.update_event_type(9001, {"lengthInMinutesOptions": [15, 30, 60]})
        request = transport.requests[0]
        self.assertEqual((request.get_method(), request.full_url), ("PATCH", "https://api.cal.com/v2/event-types/9001"))
        self.assertEqual(request.headers["Cal-api-version"], "2026-06-12")
        self.assertEqual(event_lengths(updated), (15, 30, 60))


class LengthLabelTests(unittest.TestCase):
    def test_titles_and_length_lists_read_naturally(self):
        self.assertEqual(meeting_title("30 Minute Meeting", 120), "2 Hour Meeting")
        self.assertEqual(meeting_title("30 Minute Meeting", 90), "1.5 Hour Meeting")
        self.assertEqual(meeting_title("30 Minute Meeting", 45), "45 Minute Meeting")
        self.assertEqual(meeting_title("30 Minute Meeting between A and B", 60), "1 Hour Meeting between A and B")
        self.assertEqual(meeting_title("Investor sync", 60), "Investor sync")
        self.assertEqual(describe_lengths(DEMO_LENGTHS), "5 to 480 minutes in 5-minute steps")
        self.assertEqual(describe_lengths([30]), "30 minutes")
        self.assertEqual(describe_lengths([60, 30]), "30 or 60 minutes")


class DemoLengthTests(unittest.TestCase):
    def setUp(self):
        self.calendar = DemoCalendar(clock=lambda: NOW, time_zone=ZONE)
        self.state = new_state(ZONE, NOW)
        self.tools = CalendarTools(self.calendar, self.state, NOW)

    def confirm(self, proposal):
        actions = CalendarActions(self.calendar, self.state, NOW, wait=lambda seconds: None)
        actions.execute(ConfirmProposal.model_validate({"type": "confirm_proposal", "proposalId": proposal.id}))

    def bookings_on(self, day):
        return [
            booking for booking in self.calendar.list_bookings(limit=100)["data"]
            if parse_instant(booking["start"]).astimezone(ZoneInfo(ZONE)).date().isoformat() == day
        ]

    def test_a_two_hour_window_offers_exactly_that_meeting(self):
        result = self.tools.execute("find_slots", find(
            "2026-12-09", start_time="11:00", end_time="13:00", duration_minutes=120,
        )).result
        self.assertEqual([local_range(row) for row in result["slots"]], [("11:00", "13:00")])
        self.assertEqual(result["durationMinutes"], 120)

    def test_long_meetings_respect_busy_time_and_working_hours(self):
        # Jordan's 10:00–10:30 meeting blocks any hour that overlaps it.
        morning = self.tools.execute("find_slots", find(
            "2026-11-26", start_time="09:00", end_time="11:00", duration_minutes=60,
        )).result
        self.assertEqual([local_range(row) for row in morning["slots"]], [("09:00", "10:00")])
        late = self.tools.execute("find_slots", find("2026-12-09", start_time="16:30", duration_minutes=60)).result
        self.assertEqual(late["slots"], [])
        last = self.tools.execute("find_slots", find("2026-12-09", start_time="16:00", duration_minutes=60)).result
        self.assertEqual([local_range(row) for row in last["slots"]], [("16:00", "17:00")])

    def test_confirmed_booking_keeps_the_chosen_length(self):
        self.state.guest_draft = GuestDraft(name="Avery Fixture", email="avery@example.com", duration_minutes=120)
        slots = self.tools.execute("find_slots", find(
            "2026-12-09", start_time="11:00", end_time="13:00", duration_minutes=120,
        )).result["slots"]
        outcome = self.tools.execute("propose_booking", {"slot_ref": slots[0]["ref"], "name": "", "notes": ""})
        view = outcome.events[0]["proposal"]
        self.assertEqual((view["title"], view["durationMinutes"]), ("2 Hour Meeting", 120))
        self.assertEqual(self.bookings_on("2026-12-09"), [])
        self.confirm(outcome.proposal)
        [booking] = self.bookings_on("2026-12-09")
        self.assertEqual(parse_instant(booking["end"]) - parse_instant(booking["start"]), timedelta(hours=2))

    def test_a_move_keeps_a_long_meeting_long(self):
        created = self.calendar.create_booking(
            start="2026-12-09T03:00:00Z", event_type_id=9001, name="Avery Fixture",
            email="avery@example.com", time_zone=ZONE, length_in_minutes=60,
        )
        ref = add_booking(self.state, booking_entry(created))
        with self.assertRaisesRegex(ToolRejected, "1 hour long. A move keeps its length"):
            self.tools.execute("find_slots", find("2026-12-10", for_booking_ref=ref, duration_minutes=30))
        slots = self.tools.execute("find_slots", find("2026-12-10", for_booking_ref=ref, start_time="16:00")).result
        self.assertEqual([local_range(row) for row in slots["slots"]], [("16:00", "17:00")])
        outcome = self.tools.execute("propose_reschedule", {
            "booking_ref": ref, "slot_ref": slots["slots"][0]["ref"], "reason": "",
        })
        self.confirm(outcome.proposal)
        [moved] = self.bookings_on("2026-12-10")
        self.assertEqual(parse_instant(moved["end"]) - parse_instant(moved["start"]), timedelta(hours=1))

    def test_short_signed_move_slot_cannot_propose_or_confirm_a_longer_booking(self):
        created = self.calendar.create_booking(
            start="2026-12-09T03:00:00Z", event_type_id=9001, name="Avery Fixture",
            email="avery@example.com", time_zone=ZONE, length_in_minutes=60,
        )
        booking = booking_entry(created)
        ref = add_booking(self.state, booking)
        [row] = self.tools.execute("find_slots", find("2026-12-10", start_time="16:00")).result["slots"]
        slot_ref = row["ref"]
        self.state.slots[slot_ref] = self.state.slots[slot_ref].model_copy(update={"for_booking_ref": ref})
        with self.assertRaisesRegex(ToolRejected, "original length"):
            self.tools.execute("propose_reschedule", {"booking_ref": ref, "slot_ref": slot_ref, "reason": ""})
        self.assertIsNone(self.state.proposal)

        # Simulate an earlier signed proposal whose opening was only 30 minutes.
        self.state.counters.p = 1
        self.state.proposal = Proposal(
            id="P1", kind="reschedule", slot_ref=slot_ref, booking_ref=ref,
            booking_target=BookingTarget(
                uid=booking.uid, event_type_id=booking.event_type_id,
                start_utc=booking.start_utc, end_utc=booking.end_utc,
            ),
            created_at=NOW.timestamp(),
        )
        secret = "fixture-signing-key-for-legacy-moves"
        state = verify_state(sign_state(self.state, secret), secret, NOW)
        action = ConfirmProposal.model_validate({"type": "confirm_proposal", "proposalId": "P1"})
        with (
            mock.patch.object(self.calendar, "slots", wraps=self.calendar.slots) as read_slots,
            mock.patch.object(self.calendar, "reschedule_booking", wraps=self.calendar.reschedule_booking) as move,
        ):
            with self.assertRaisesRegex(ActionError, "original length"):
                CalendarActions(self.calendar, state, NOW).execute(action)
            read_slots.assert_not_called()
            move.assert_not_called()

        self.assertEqual(self.calendar.booking(booking.uid)["status"], "accepted")
        self.assertEqual(self.bookings_on("2026-12-10"), [])


class NoModel:
    """Fails the test if a turn that code can answer reaches the model."""

    async def stream(self, **request):
        raise AssertionError("This turn should not call the model.")
        yield


class ReversedRangeTests(unittest.IsolatedAsyncioTestCase):
    async def turn(self, text):
        service = TurnService(model=NoModel(), calendar=DemoCalendar(clock=lambda: NOW, time_zone=ZONE),
                              secret="fixture-signing-key-for-variable-lengths", clock=lambda: NOW)
        self.addCleanup(service.close_prefetch)
        request = ChatRequest.model_validate({"timeZone": ZONE, "stateToken": None,
                                              "input": {"kind": "text", "text": text}})
        events = [event async for event in service.events(request)]
        slots = {ref: view for event in events if event["type"] == "refs" for ref, view in event.get("slots", {}).items()}
        chips = [slots[ref]["timeLabel"] for event in events if event["type"] == "slot_chips" for ref in event["refs"]]
        return [event["text"] for event in events if event["type"] == "assistant_text"], chips

    async def test_a_backwards_range_reads_as_the_morning_and_keeps_the_length(self):
        texts, chips = await self.turn("can I book a 1 hour meeting on 9 Dec from 11pm to 12pm")
        self.assertIn("so I read it as 11:00–12:00", texts[0])
        self.assertEqual(chips, ["11:00–12:00"])

    async def test_a_backwards_range_without_a_sensible_reading_asks(self):
        texts, chips = await self.turn("book a meeting with Avery Fixture on 9 Dec from 3pm to 1pm")
        self.assertEqual(texts, ["That time range ends before it starts. What start and end time should I use?"])
        self.assertEqual(chips, [])


class SetLengthsScriptTests(unittest.TestCase):
    EVENT = {"id": 9001, "slug": "30min", "title": "30 Minute Meeting", "lengthInMinutes": 30,
             "hidden": False, "ownerId": 7}

    def run_script(self, settings, **kwargs):
        client = mock.Mock()
        client.me.return_value = {"username": "host", "id": 7, "timeZone": ZONE}
        client.event_types.return_value = [dict(self.EVENT)]
        client.update_event_type.return_value = {**self.EVENT, "lengthInMinutesOptions": list(range(5, 481, 5))}
        client.slots.return_value = {"2026-11-26": [{"start": "2026-11-26T01:00:00Z", "end": "2026-11-26T03:00:00Z"}]}
        with mock.patch("scripts.set_lengths.load_local_environment", return_value=settings), \
                mock.patch("scripts.set_lengths.CalClient", return_value=client), redirect_stdout(StringIO()):
            set_lengths.run(**kwargs)
        return client

    def test_the_default_run_only_reads(self):
        client = self.run_script({"CAL_API_KEY": "key"})
        client.update_event_type.assert_not_called()
        client.slots.assert_not_called()

    def test_apply_saves_every_length_and_checks_a_long_opening(self):
        client = self.run_script({"CAL_API_KEY": "key", "CAL_DEFAULT_EVENT_TYPE_SLUG": "30min"}, apply=True)
        event_id, changes = client.update_event_type.call_args.args
        self.assertEqual(event_id, 9001)
        self.assertEqual(changes["lengthInMinutesOptions"], list(range(5, 481, 5)))
        self.assertEqual(client.slots.call_args.kwargs["duration"], 120)


class FixedLengthTests(unittest.TestCase):
    def test_an_event_type_without_options_names_what_it_offers(self):
        tools = CalendarTools(FakeCal(), new_state(ZONE, NOW), NOW)
        with self.assertRaisesRegex(ToolRejected, "can’t book 45-minute meetings. It offers 30 minutes."):
            tools.execute("find_slots", find("2026-11-26", duration_minutes=45))

    def test_the_fake_calendar_refuses_lengths_the_event_type_lacks(self):
        with self.assertRaises(CalError):
            FakeCal().slots(9001, "2026-11-26", "2026-11-26", ZONE, duration=60)


if __name__ == "__main__":
    unittest.main()
