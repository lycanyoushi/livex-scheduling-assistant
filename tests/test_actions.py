from datetime import datetime
import json
import unittest

from backend.actions import ActionError, CalendarActions
from backend.cal_client import CalError
from backend.protocol import ConfirmProposal, DismissProposal, MoreSlots, PickSlot, RequestCancel, RequestReschedule
from backend.state import PendingCall, SlotEntry, add_booking, add_slot, new_state, sign_state, verify_state
from backend.tools import CalendarTools, booking_entry
from evals.fake_cal import DEFAULT_CLOCK, FakeCal


NOW = DEFAULT_CLOCK
ZONE = "Asia/Singapore"
FIRST = "2026-11-24T14:00:00+08:00"
SECOND = "2026-11-24T15:00:00+08:00"
KEY = "testing-signed-calendar-action-tokens"


class Calendar(FakeCal):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.reads = []
        self.writes = []

    def slots(self, event_type_id, start, end, time_zone="UTC", *, booking_uid_to_reschedule=None, fresh=False):
        self.reads.append(("slots", booking_uid_to_reschedule, fresh))
        return super().slots(
            event_type_id, start, end, time_zone, booking_uid_to_reschedule=booking_uid_to_reschedule, fresh=fresh,
        )

    def booking(self, uid):
        self.reads.append(("booking", uid))
        return super().booking(uid)

    def create_booking(self, **kwargs):
        self.writes.append(("create", kwargs))
        return super().create_booking(**kwargs)

    def cancel_booking(self, uid, reason="User requested cancellation"):
        self.writes.append(("cancel", uid, reason))
        return super().cancel_booking(uid, reason)

    def reschedule_booking(self, uid, start, reason="User requested reschedule"):
        self.writes.append(("reschedule", uid, start))
        return super().reschedule_booking(uid, start, reason)


def event(outcome, kind):
    return next(item for item in outcome.events if item["type"] == kind)


def ref_at(outcome, local_start):
    return next(
        ref for ref, view in event(outcome, "refs")["slots"].items()
        if view["startLocalIso"] == local_start
    )


def stored_slot(state, start="2026-11-24T06:00:00Z", end="2026-11-24T06:30:00Z"):
    return add_slot(state, SlotEntry(start_utc=start, end_utc=end, event_type_id=9001, fetched_at=NOW.timestamp()))


class CalendarActionsTests(unittest.TestCase):
    def setUp(self):
        self.state = new_state(ZONE, NOW)
        self.state.user.name = "Sam"
        self.state.user.email = "sam@example.com"

    def actions(self, calendar):
        return CalendarActions(calendar, self.state, NOW)

    def record(self, calendar, uid="fixture-sam-intro"):
        return add_booking(self.state, booking_entry(calendar.booking(uid)))

    def test_button_only_create_reschedule_and_cancel_with_signed_context(self):
        calendar = Calendar(fixture_overrides={"open_local_starts": ["2026-11-24T14:00", "2026-11-24T15:00"]})
        actions = self.actions(calendar)
        available = actions.execute(MoreSlots(type="more_slots", fromDate="2026-11-24"))
        first = ref_at(available, FIRST)
        self.assertIn(first, event(available, "slot_chips")["refs"])
        card = event(actions.execute(PickSlot(type="pick_slot", slotRef=first)), "proposal")["proposal"]
        self.assertEqual((card["kind"], card["name"], card["email"]), ("book", "Sam", "sam@example.com"))
        self.assertEqual(calendar.writes, [])

        completed = actions.execute(ConfirmProposal(type="confirm_proposal", proposalId=card["id"], notes="Test note"))
        created = event(completed, "booking_result")
        self.assertEqual(created["kind"], "booked")
        self.assertEqual(created["booking"]["startUtc"], "2026-11-24T06:00:00Z")
        original_ref = created["booking"]["ref"]
        original_uid = self.state.bookings[original_ref].uid
        self.assertNotIn(original_uid, json.dumps(completed.events))
        self.assertEqual(calendar.writes[0][1]["notes_field"], "notes")
        self.assertEqual(calendar.writes[0][1]["notes"], "Test note")
        self.assertEqual(calendar.reads[-1], ("slots", None, True))
        duplicate = actions.execute(ConfirmProposal(type="confirm_proposal", proposalId=card["id"]))
        self.assertEqual(duplicate.events, [event(completed, "assistant_text")])
        self.assertEqual(len(calendar.writes), 1)

        move = actions.execute(RequestReschedule(type="request_reschedule", bookingRef=original_ref))
        self.assertEqual(calendar.reads[-2:], [("booking", original_uid), ("slots", original_uid, False)])
        self.assertEqual(self.state.rescheduling_booking_ref, original_ref)
        second = ref_at(move, SECOND)
        self.assertEqual(self.state.slots[second].for_booking_ref, original_ref)
        self.state = verify_state(sign_state(self.state, KEY), KEY, NOW)
        self.assertEqual(self.state.rescheduling_booking_ref, original_ref)
        actions = self.actions(calendar)
        move_card = event(actions.execute(PickSlot(type="pick_slot", slotRef=second)), "proposal")["proposal"]
        self.assertEqual((move_card["kind"], move_card["bookingRef"]), ("reschedule", original_ref))
        self.assertEqual(len(calendar.writes), 1)

        moved = actions.execute(ConfirmProposal(type="confirm_proposal", proposalId=move_card["id"]))
        replacement = event(moved, "booking_result")
        self.assertEqual(replacement["kind"], "rescheduled")
        self.assertEqual(replacement["booking"]["startUtc"], "2026-11-24T07:00:00Z")
        self.assertEqual(calendar.reads[-2:], [("booking", original_uid), ("slots", original_uid, True)])
        self.assertEqual(calendar.writes[-1], ("reschedule", original_uid, datetime.fromisoformat(SECOND)))
        self.assertEqual(self.state.bookings[original_ref].status, "cancelled")
        self.assertEqual(event(moved, "refs")["bookings"][original_ref]["status"], "cancelled")
        replacement_ref = replacement["booking"]["ref"]
        replacement_uid = self.state.bookings[replacement_ref].uid
        self.assertNotEqual(replacement_uid, original_uid)
        self.assertNotIn(replacement_uid, json.dumps(moved.events))
        self.assertIsNone(self.state.rescheduling_booking_ref)

        cancel = actions.execute(RequestCancel(type="request_cancel", bookingRef=replacement_ref))
        cancel_card = event(cancel, "proposal")["proposal"]
        self.assertEqual(cancel_card["bookingRef"], replacement_ref)
        self.assertEqual(len(calendar.writes), 2)
        cancelled = actions.execute(ConfirmProposal(type="confirm_proposal", proposalId=cancel_card["id"]))
        self.assertEqual(event(cancelled, "booking_result")["kind"], "cancelled")
        self.assertEqual(calendar.writes[-1][0:2], ("cancel", replacement_uid))
        self.assertEqual(self.state.bookings[replacement_ref].status, "cancelled")
        self.assertEqual(calendar.list_bookings()["data"], [])

    def test_more_slots_advances_window_and_keeps_reschedule_binding(self):
        calendar = Calendar(fixture_overrides={"active_bookings": ["sam_intro"]})
        ref = self.record(calendar)
        actions = self.actions(calendar)
        first = actions.execute(RequestReschedule(type="request_reschedule", bookingRef=ref))
        later = actions.execute(MoreSlots(type="more_slots"))
        first_starts = [value["startLocalIso"] for value in event(first, "refs")["slots"].values()]
        later_slots = event(later, "refs")["slots"]
        self.assertTrue(all(value["startLocalIso"] > max(first_starts) for value in later_slots.values()))
        self.assertTrue(all(self.state.slots[slot_ref].for_booking_ref == ref for slot_ref in later_slots))
        self.assertEqual(calendar.reads[-1], ("slots", "fixture-sam-intro", False))
        self.assertEqual(calendar.writes, [])

    def test_cancel_requires_live_owner_and_unchanged_booking_on_confirm(self):
        class OwnerChanges(Calendar):
            def booking(self, uid):
                data = super().booking(uid)
                if self.changed:
                    data["attendees"] = [{"email": "other@example.com"}]
                return data

        cal = OwnerChanges(fixture_overrides={"active_bookings": ["sam_intro"]})
        cal.changed = False
        ref = self.record(cal)
        actions = self.actions(cal)
        card = event(actions.execute(RequestCancel(type="request_cancel", bookingRef=ref)), "proposal")["proposal"]
        cal.changed = True
        with self.assertRaises(ActionError) as caught:
            actions.execute(ConfirmProposal(type="confirm_proposal", proposalId=card["id"]))
        self.assertEqual(caught.exception.code, "refusal")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(cal.writes, [])
        self.assertEqual(self.state.proposal.id, card["id"])

    def test_signed_foreign_booking_fails_before_calendar_read(self):
        calendar = Calendar(fixture_overrides={"active_bookings": ["other_intro"]})
        ref = self.record(calendar, "fixture-other-intro")
        calendar.reads.clear()
        for action in (
            RequestCancel(type="request_cancel", bookingRef=ref),
            RequestReschedule(type="request_reschedule", bookingRef=ref),
        ):
            with self.subTest(action=action), self.assertRaises(ActionError) as caught:
                self.actions(calendar).execute(action)
            self.assertEqual(caught.exception.code, "refusal")
        self.assertEqual(calendar.reads, [])
        self.assertEqual(calendar.writes, [])

    def test_create_conflict_emits_three_nearest_refs_and_keeps_state(self):
        calendar = Calendar(fixture_overrides={
            "calendar_faults": [{"name": "occupied_on_create", "local_start": "2026-11-24T14:00"}],
        })
        actions = self.actions(calendar)
        slot_ref = stored_slot(self.state)
        card = event(actions.execute(PickSlot(type="pick_slot", slotRef=slot_ref)), "proposal")["proposal"]
        self.state.pending_call = PendingCall(id="pending-1", name="propose_booking", proposal_id=card["id"])
        failed = actions.execute(ConfirmProposal(type="confirm_proposal", proposalId=card["id"]))
        self.assertEqual(failed.error_code, "slot_taken")
        self.assertEqual(event(failed, "error")["code"], "slot_taken")
        chips = event(failed, "slot_chips")["refs"]
        self.assertEqual(
            [self.state.slots[ref].start_utc for ref in chips],
            ["2026-11-24T05:15:00+00:00", "2026-11-24T07:00:00+00:00", "2026-11-24T04:30:00+00:00"],
        )
        self.assertEqual(set(event(failed, "refs")["slots"]), set(chips))
        self.assertEqual(len(calendar.writes), 1)
        self.assertIsNone(self.state.proposal)
        self.assertEqual(self.state.pending_call.outcome.outcome, "no_action_yet")
        reopened = actions.execute(PickSlot(type="pick_slot", slotRef=chips[0]))
        self.assertEqual(event(reopened, "proposal")["proposal"]["kind"], "book")
        self.assertEqual(len(calendar.writes), 1)

    def test_stale_slot_returns_fresh_alternatives_before_attempting_a_write(self):
        calendar = Calendar()
        slot_ref = stored_slot(self.state)
        action = self.actions(calendar)
        card = event(action.execute(PickSlot(type="pick_slot", slotRef=slot_ref)), "proposal")["proposal"]
        FakeCal.create_booking(
            calendar, start=FIRST, event_type_id=9001, name="Busy",
            email="fixture-busy@example.com", time_zone=ZONE,
        )
        failed = action.execute(ConfirmProposal(type="confirm_proposal", proposalId=card["id"]))
        self.assertEqual(failed.error_code, "slot_taken")
        self.assertEqual(len(event(failed, "slot_chips")["refs"]), 3)
        self.assertTrue(all(fresh for read, _, fresh in calendar.reads if read == "slots"))
        self.assertEqual(calendar.writes, [])

    def test_reschedule_race_rechecks_the_latest_booking_uid_and_tags_recovery_slots(self):
        class RacingCalendar(Calendar):
            def reschedule_booking(self, uid, start, reason="User requested reschedule"):
                self.writes.append(("reschedule", uid, start))
                FakeCal.create_booking(
                    self, start=start, event_type_id=9001, name="Busy",
                    email="fixture-busy@example.com", time_zone=ZONE,
                )
                raise CalError("invalid", "Cal.com rejected the request.", status=409)

        calendar = RacingCalendar(fixture_overrides={"active_bookings": ["sam_intro"]})
        ref = self.record(calendar)
        actions = self.actions(calendar)
        options = actions.execute(RequestReschedule(type="request_reschedule", bookingRef=ref))
        candidates = event(options, "slot_chips")["refs"]
        slot = next(slot_ref for slot_ref in candidates if self.state.slots[slot_ref].start_utc != "2026-11-26T02:00:00Z")
        card = event(actions.execute(PickSlot(type="pick_slot", slotRef=slot)), "proposal")["proposal"]
        failed = actions.execute(ConfirmProposal(type="confirm_proposal", proposalId=card["id"]))
        self.assertEqual(failed.error_code, "slot_taken")
        self.assertEqual(calendar.reads[-1], ("slots", "fixture-sam-intro", True))
        self.assertEqual(calendar.writes[0][0:2], ("reschedule", "fixture-sam-intro"))
        self.assertEqual(self.state.bookings[ref].status, "accepted")
        self.assertEqual(self.state.rescheduling_booking_ref, ref)
        chips = event(failed, "slot_chips")["refs"]
        self.assertEqual(len(chips), 3)
        self.assertTrue(all(self.state.slots[item].for_booking_ref == ref for item in chips))

    def test_reschedule_cannot_confirm_with_an_ordinary_slot_ref(self):
        calendar = Calendar(fixture_overrides={"active_bookings": ["sam_intro"]})
        booking_ref = self.record(calendar)
        actions = self.actions(calendar)
        opening = actions.execute(RequestReschedule(type="request_reschedule", bookingRef=booking_ref))
        held = event(opening, "slot_chips")["refs"][0]
        card = event(actions.execute(PickSlot(type="pick_slot", slotRef=held)), "proposal")["proposal"]
        self.state.proposal.slot_ref = stored_slot(self.state)
        with self.assertRaises(ActionError) as rejected:
            actions.execute(ConfirmProposal(type="confirm_proposal", proposalId=card["id"]))
        self.assertEqual(rejected.exception.code, "invalid_action")
        self.assertEqual(calendar.writes, [])

    def test_dismiss_and_ui_replacement_set_pending_tool_outcomes(self):
        calendar = Calendar()
        slot = stored_slot(self.state)
        tools = CalendarTools(calendar, self.state, NOW)
        initial = tools.execute("propose_booking", {"slot_ref": slot, "name": "", "email": "", "notes": ""})
        self.state.pending_call = PendingCall(id="call-x", name="propose_booking", proposal_id=initial.proposal.id)
        actions = self.actions(calendar)
        new_card = event(actions.execute(PickSlot(type="pick_slot", slotRef=slot)), "proposal")["proposal"]
        self.assertEqual(self.state.pending_call.outcome.outcome, "superseded")
        self.assertEqual(self.state.pending_call.outcome.by, new_card["id"])
        self.state.pending_call = PendingCall(id="call-y", name="propose_booking", proposal_id=new_card["id"])
        dismissed = actions.execute(DismissProposal(type="dismiss_proposal", proposalId=new_card["id"]))
        self.assertEqual(event(dismissed, "assistant_text")["text"], "Okay. The schedule is unchanged.")
        self.assertEqual(self.state.pending_call.outcome.outcome, "dismissed")
        self.assertIsNone(self.state.proposal)
        self.assertEqual(calendar.writes, [])


if __name__ == "__main__":
    unittest.main()
