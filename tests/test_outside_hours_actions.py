"""Outside-hours approval, atomic overlap checks, and exact write verification."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
from threading import Barrier
from unittest import TestCase, mock
from zoneinfo import ZoneInfo

from backend.actions import ActionError, CalendarActions
from backend.cal_client import CalClient, CalError, outside_hours_conflicts
from backend.demo_calendar import DemoCalendar
from backend.protocol import ConfirmProposal, PickSlot
from backend.state import GuestDraft, SlotEntry, add_slot, new_state, sign_state, verify_state
from backend.time import parse_instant


ZONE = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 11, 25, 10, tzinfo=ZONE)
START = datetime(2026, 11, 25, 18, 30, tzinfo=ZONE).astimezone(UTC)
END = START + timedelta(minutes=30)
CONTACT = {"event_type_id": 9001, "name": "Fixture Guest", "email": "guest@example.com", "time_zone": ZONE.key}


class OutsideHoursActionsTests(TestCase):
    def setUp(self):
        self.calendar = DemoCalendar(clock=lambda: NOW, time_zone=ZONE.key)

    def offer(self, *, start=START, outside=True, published=True, duration=30):
        state = new_state(ZONE.key, NOW)
        state.guest_draft = GuestDraft(name=CONTACT["name"], email=CONTACT["email"], duration_minutes=duration)
        ref = add_slot(state, SlotEntry(
            start_utc=start.isoformat(), end_utc=(start + timedelta(minutes=duration)).isoformat(),
            event_type_id=9001, fetched_at=NOW.timestamp(), outside_hours=outside,
        ))
        if outside and published:
            state.outside_hours_offer = ref
        return state, ref

    def actions(self, state):
        return CalendarActions(self.calendar, state, NOW, wait=lambda seconds: None)

    def proposal(self, state, ref):
        picked = self.actions(state).execute(PickSlot(type="pick_slot", slotRef=ref, outsideHours=True))
        self.assertEqual(picked.events[0]["type"], "proposal")
        self.assertIsNotNone(state.proposal)
        return ConfirmProposal(type="confirm_proposal", proposalId=state.proposal.id)

    def test_pick_requires_matching_exact_outside_hours_offer(self):
        for outside, flag, published in ((True, False, True), (False, True, False), (True, True, False)):
            with self.subTest(outside=outside, flag=flag, published=published):
                state, ref = self.offer(outside=outside, published=published)
                with self.assertRaises(ActionError):
                    self.actions(state).execute(PickSlot(type="pick_slot", slotRef=ref, outsideHours=flag))
                self.assertIsNone(state.proposal)

    def test_confirm_passes_override_only_after_explicit_pick_and_reads_exact_persisted_interval(self):
        state, ref = self.offer(duration=90)
        with self.assertRaises(CalError):
            self.calendar.create_booking(start=START, **CONTACT)
        with mock.patch.object(self.calendar, "create_booking", wraps=self.calendar.create_booking) as write:
            confirm = self.proposal(state, ref)
            write.assert_not_called()
            key = "fixture-signing-key-for-outside-hours"
            signed_state = verify_state(sign_state(state, key), key, NOW)
            with mock.patch.object(self.calendar, "booking", wraps=self.calendar.booking) as read:
                result = self.actions(signed_state).execute(confirm)
            read.assert_called_once()
        write.assert_called_once()
        self.assertIs(write.call_args.kwargs["outside_hours"], True)
        self.assertEqual(write.call_args.kwargs["start"], START)
        self.assertEqual(write.call_args.kwargs["length_in_minutes"], 90)
        uid, = read.call_args.args
        persisted = self.calendar.booking(uid)
        self.assertEqual(
            (parse_instant(persisted["start"]), parse_instant(persisted["end"])),
            (START, START + timedelta(minutes=90)),
        )
        booking_result, = [event for event in result.events if event["type"] == "booking_result"]
        self.assertEqual(booking_result["kind"], "booked")
        self.assertNotIn(ref, signed_state.slots)
        self.assertIsNone(signed_state.outside_hours_offer)

    def test_confirm_reports_overlapping_title_and_stops_before_create(self):
        state, ref = self.offer()
        confirm = self.proposal(state, ref)
        existing = self.calendar.create_booking(start=START + timedelta(minutes=15), outside_hours=True, **CONTACT)
        self.assertEqual(outside_hours_conflicts(self.calendar, START, END), (f"{existing['title']} with {CONTACT['name']}",))
        self.assertEqual(outside_hours_conflicts(self.calendar, START, START + timedelta(minutes=15)), ())
        with mock.patch.object(self.calendar, "create_booking") as write:
            result = self.actions(state).execute(confirm)
        write.assert_not_called()
        self.assertEqual(result.error_code, "slot_taken")
        self.assertIn(existing["title"], result.events[0]["message"])
        self.assertIsNone(state.proposal)

    def test_two_concurrent_confirmations_cannot_double_book(self):
        requests = []
        for _ in range(2):
            state, ref = self.offer()
            requests.append((self.actions(state), self.proposal(state, ref)))
        original_check = self.calendar.outside_hours_conflicts
        both_checked = Barrier(2, timeout=2)

        def check(start, end):
            conflicts = original_check(start, end)
            if not conflicts:
                both_checked.wait()
            return conflicts

        with mock.patch.object(self.calendar, "outside_hours_conflicts", side_effect=check):
            with ThreadPoolExecutor(max_workers=2) as workers:
                outcomes = list(workers.map(lambda request: request[0].execute(request[1]), requests))
        self.assertCountEqual([outcome.error_code for outcome in outcomes], [None, "slot_taken"])
        self.assertEqual(len(outside_hours_conflicts(self.calendar, START, END)), 1)

    def test_provider_refusal_and_readback_of_changed_start_or_end_are_visible(self):
        state, ref = self.offer()
        confirm = self.proposal(state, ref)
        with mock.patch.object(self.calendar, "create_booking", side_effect=CalError("invalid", "Provider refused")):
            refused = self.actions(state).execute(confirm)
        self.assertIn("refused", refused.events[0]["message"])
        self.assertIsNone(state.proposal)

        for field in ("start", "end"):
            with self.subTest(field=field):
                self.calendar = DemoCalendar(clock=lambda: NOW, time_zone=ZONE.key)
                state, ref = self.offer()
                confirm = self.proposal(state, ref)
                normal_read = self.calendar.booking

                def moved(uid):
                    booking = normal_read(uid)
                    return {**booking, field: (parse_instant(booking[field]) + timedelta(minutes=5)).isoformat()}

                with mock.patch.object(self.calendar, "booking", side_effect=moved):
                    outcome = self.actions(state).execute(confirm)
                self.assertEqual(outcome.error_code, "write_unverified")
                self.assertTrue(any("different start or end" in event.get("message", "") for event in outcome.events))
                self.assertFalse(any(event["type"] == "booking_result" for event in outcome.events))
                self.assertIsNone(state.outside_hours_offer)

    def test_calcom_adapter_gates_all_override_write_payloads(self):
        data = {
            "uid": "fixture-booked", "title": "Fixture Meeting", "status": "accepted",
            "start": START.isoformat(), "end": END.isoformat(), "eventTypeId": 9001,
        }
        transport = mock.Mock(return_value=(201, json.dumps({"status": "success", "data": data}).encode()))
        client = CalClient("fixture-key", transport=transport)
        with self.assertRaisesRegex(CalError, "Outside-hours booking is unavailable"):
            client.create_booking(start=START, outside_hours=True, **CONTACT)
        transport.assert_not_called()
        client.create_booking(start=START, **CONTACT)
        transport.assert_called_once()
        request, _ = transport.call_args.args
        payload = json.loads(request.data)
        self.assertNotIn("allowBookingOutOfBounds", payload)
        self.assertNotIn("allowConflicts", payload)
