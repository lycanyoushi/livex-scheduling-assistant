import base64
from datetime import datetime, timedelta, timezone
import hmac
import json
import unittest
from unittest.mock import patch

from backend.state import (
    MAX_BOOKINGS,
    MAX_COMPLETED,
    MAX_NOTES,
    MAX_SLOTS,
    SESSION_TTL,
    BookingEntry,
    BookingTarget,
    ConfirmedOutcome,
    GuestDraft,
    PendingCall,
    Proposal,
    SlotEntry,
    StateError,
    add_booking,
    add_slot,
    new_state,
    sign_state,
    verify_state,
)


NOW = datetime(2026, 11, 23, 10, tzinfo=timezone.utc)
SECRET = "local-test-secret-only-x0123456789abc"


def sample_slot(*, for_booking_ref: str | None = None) -> SlotEntry:
    return SlotEntry(
        start_utc="2026-11-24T10:00:00Z",
        end_utc="2026-11-24T10:30:00Z",
        event_type_id=10,
        fetched_at=NOW.timestamp(),
        for_booking_ref=for_booking_ref,
    )


def sample_booking() -> BookingEntry:
    return BookingEntry(
        uid="calendar-test-uid",
        title="Synthetic 30-minute meeting",
        start_utc="2026-11-24T09:00:00+00:00",
        end_utc="2026-11-24T09:30:00+00:00",
        status="accepted",
        attendee_emails=["sam@example.com"],
        event_type_id=10,
    )


def encode_signed_fixture(document: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(document).encode("utf-8")).rstrip(b"=")
    signature = base64.urlsafe_b64encode(hmac.digest(SECRET.encode(), payload, "sha256")).rstrip(b"=")
    return (payload + b"." + signature).decode("ascii")


class StateTests(unittest.TestCase):
    def test_signed_round_trip_includes_mutable_state(self):
        state = new_state("America/New_York", NOW)
        state.guest_draft = GuestDraft(name="Sam Test", email="sam@example.com")
        booking_ref = add_booking(state, sample_booking())
        slot_ref = add_slot(state, sample_slot(for_booking_ref=booking_ref))
        state.counters.p = 1
        booking = state.bookings[booking_ref]
        state.proposal = Proposal(
            id="P1", kind="reschedule", slot_ref=slot_ref, booking_ref=booking_ref,
            booking_target=BookingTarget(
                uid=booking.uid, start_utc=booking.start_utc, end_utc=booking.end_utc,
                event_type_id=booking.event_type_id,
            ),
            created_at=NOW.timestamp(),
        )
        state.interaction_id = "test-interaction"
        state.pending_call = PendingCall(
            id="test-tool-call", name="propose_reschedule", proposal_id="P1",
            outcome=ConfirmedOutcome(outcome="confirmed", result="Rescheduled B1"),
        )
        state.notes.append("Rescheduled this booking")
        state.completed["P1"] = "Rescheduled B1"

        token = sign_state(state, SECRET)
        restored = verify_state(token, SECRET.encode("utf-8"), NOW)

        self.assertEqual(restored, state)
        self.assertNotEqual(new_state(state.time_zone, NOW).session_id, state.session_id)
        self.assertRegex(token, r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}$")

    def test_token_tamper_is_rejected_before_decoding(self):
        token = sign_state(new_state("Asia/Singapore", NOW), SECRET)
        payload, signature = token.split(".")
        replacement = "A" if payload[0] != "A" else "B"

        with patch("backend.state.base64.b64decode") as decoder:
            with self.assertRaisesRegex(StateError, "signature"):
                verify_state(replacement + payload[1:] + "." + signature, SECRET, NOW)
            decoder.assert_not_called()

        with self.assertRaisesRegex(StateError, "signature"):
            verify_state(token, SECRET + "wrong-secret", NOW)

    def test_invalid_envelopes_and_verified_invalid_json_state_are_rejected(self):
        signed_state = new_state("UTC", NOW).model_dump(by_alias=True)
        for document in (
            {**signed_state, "v": 1},
            {**signed_state, "v": True},
            {**signed_state, "slots": []},
            {**signed_state, "unrecognizedField": "value"},
            {key: value for key, value in signed_state.items() if key != "v"},
        ):
            with self.assertRaisesRegex(StateError, "Invalid session state"):
                verify_state(encode_signed_fixture(document), SECRET, NOW)

        for invalid_token in ("", ".", "body.signature.extra", "x.x", "🙂.test"):
            with self.subTest(token=invalid_token), self.assertRaisesRegex(StateError, "Invalid session token"):
                verify_state(invalid_token, SECRET, NOW)

    def test_expiry_uses_the_signed_clock_value(self):
        state = new_state("Asia/Singapore", NOW)
        token = sign_state(state, SECRET)

        self.assertEqual(verify_state(token, SECRET, NOW + SESSION_TTL - timedelta(seconds=1)), state)
        with self.assertRaisesRegex(StateError, "expired"):
            verify_state(token, SECRET, NOW + SESSION_TTL)

    def test_slot_and_booking_references_keep_only_latest_and_never_reuse_ids(self):
        state = new_state("UTC", NOW)
        for index in range(1, MAX_SLOTS + 4):
            self.assertEqual(add_slot(state, sample_slot()), f"S{index}")
        for index in range(1, MAX_BOOKINGS + 4):
            self.assertEqual(add_booking(state, sample_booking()), f"B{index}")

        self.assertEqual(list(state.slots), [f"S{index}" for index in range(4, MAX_SLOTS + 4)])
        self.assertEqual(list(state.bookings), [f"B{index}" for index in range(4, MAX_BOOKINGS + 4)])

        restored = verify_state(sign_state(state, SECRET), SECRET, NOW)
        self.assertEqual(add_slot(restored, sample_slot()), f"S{MAX_SLOTS + 4}")
        self.assertEqual(add_booking(restored, sample_booking()), f"B{MAX_BOOKINGS + 4}")
        self.assertEqual(next(iter(restored.slots)), "S5")
        self.assertEqual(next(iter(restored.bookings)), "B5")

    def test_ref_counters_and_note_caps_are_enforced_before_signing(self):
        state = new_state("UTC", NOW)
        state.slots["S1"] = sample_slot()
        with self.assertRaisesRegex(StateError, "Invalid session state"):
            sign_state(state, SECRET)

        state.slots.clear()
        state.notes = ["Test note"] * (MAX_NOTES + 1)
        with self.assertRaisesRegex(StateError, "Invalid session state"):
            sign_state(state, SECRET)

        state.notes.clear()
        state.counters.p = MAX_COMPLETED + 1
        state.completed = {f"P{index}": "Test result" for index in range(1, MAX_COMPLETED + 2)}
        with self.assertRaisesRegex(StateError, "Invalid session state"):
            sign_state(state, SECRET)
