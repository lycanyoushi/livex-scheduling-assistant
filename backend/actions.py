"""Run confirmation and calendar controls without invoking the model."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from time import sleep
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from backend.cal_client import Booking, CalError, EventType, Timestamp
from backend.confirmations import ConfirmationConflict, ConfirmationLedger
from backend.prescan import EMAIL
from backend.protocol import (
    BookingResultEvent,
    BookingView,
    ConfirmProposal,
    DismissProposal,
    MoreSlots,
    PickSlot,
    RefsEvent,
    RequestCancel,
    RequestReschedule,
    ServerEvent,
    SlotChipsEvent,
    UiAction,
)
from backend.state import (
    MAX_COMPLETED,
    MAX_NOTES,
    BookingEntry,
    BookingTarget,
    ConfirmedOutcome,
    DismissedOutcome,
    GuestDraft,
    NoActionYetOutcome,
    PendingOutcome,
    Proposal,
    SessionState,
    SlotEntry,
    SupersededOutcome,
    add_booking,
    add_slot,
)
from backend.time import local_bounds, parse_instant
from backend.tools import CalendarReader, CalendarTools, ToolRejected, booking_entry, booking_view, last_bookable_day, slot_view


_UPCOMING_STATUS = {"accepted", "pending"}
_SLOT_TAKEN = "That time was just taken. Here are the closest open times:"
_MAX_VERIFY_PAGES = 3
_WRITE_READBACK_DELAY_SECONDS = 2.0


class CalendarWriter(CalendarReader, Protocol):
    def booking(self, uid: str) -> Booking: ...

    def create_booking(
        self,
        *,
        start: Timestamp,
        event_type_id: int,
        name: str,
        email: str,
        time_zone: str,
        notes: str | None = None,
        notes_field: str | None = None,
    ) -> Booking: ...

    def cancel_booking(self, uid: str, reason: str = "User requested cancellation") -> Booking: ...

    def reschedule_booking(
        self, uid: str, start: Timestamp, reason: str = "User requested reschedule"
    ) -> Booking: ...


class ActionError(ValueError):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class ActionOutcome:
    events: list[ServerEvent]
    error_code: str | None = None


def _has_attendee(booking: BookingEntry, email: str | None) -> bool:
    return bool(email and any(address.casefold() == email.casefold() for address in booking.attendee_emails))


def _same_attendees(first: BookingEntry, second: BookingEntry) -> bool:
    return {email.casefold() for email in first.attendee_emails} == {
        email.casefold() for email in second.attendee_emails
    }


class CalendarActions:
    def __init__(
        self,
        calendar: CalendarWriter,
        state: SessionState,
        now: datetime,
        *,
        allowed_slugs: Iterable[str] = (),
        default_slug: str | None = None,
        ledger: ConfirmationLedger | None = None,
        wait: Callable[[float], None] = sleep,
    ) -> None:
        self.calendar = calendar
        self.state = state
        self.now = parse_instant(now)
        self.zone = ZoneInfo(state.time_zone)
        self.tools = CalendarTools(calendar, state, self.now, allowed_slugs=allowed_slugs, default_slug=default_slug)
        self.ledger = ledger
        self.wait = wait

    def execute(self, action: UiAction, *, later: bool = False) -> ActionOutcome:
        try:
            if isinstance(action, PickSlot):
                return self._pick_slot(action)
            if isinstance(action, MoreSlots):
                return self._more_slots(action)
            if isinstance(action, RequestCancel):
                return self._request_cancel(action)
            if isinstance(action, RequestReschedule):
                return self._request_reschedule(action, later=later)
            if isinstance(action, ConfirmProposal):
                return self._confirm(action)
            if isinstance(action, DismissProposal):
                return self._dismiss(action)
        except ToolRejected as error:
            raise ActionError("invalid_action", str(error)) from None
        raise ActionError("invalid_action", "Choose a scheduling action from the chat.")

    def _live_booking(self, ref: str, *, target: BookingTarget | None = None) -> BookingEntry:
        saved = self.state.bookings.get(ref)
        if saved is None:
            raise ActionError("invalid_action", "Choose a booking from your calendar results.")
        live = self._booking_result(self.calendar.booking(saved.uid))
        if live is None or live.uid != saved.uid:
            raise ActionError("invalid_action", "I couldn't verify that booking. List your calendar again.")
        if live.status not in _UPCOMING_STATUS or parse_instant(live.start_utc) <= self.now:
            raise ActionError("invalid_action", "Only upcoming bookings can be changed.")
        if target is not None and (
            live.uid != target.uid
            or parse_instant(live.start_utc) != parse_instant(target.start_utc)
            or parse_instant(live.end_utc) != parse_instant(target.end_utc)
            or live.event_type_id != target.event_type_id
        ):
            raise ActionError("invalid_action", "That booking changed. Open it again to make a new confirmation.")
        self.state.bookings[ref] = live
        return live

    def _event(self, event_type_id: int | None) -> EventType:
        for event in self.tools.event_types():
            if event["id"] == event_type_id:
                return event
        raise ActionError("invalid_action", "This event type is no longer available.")

    def _pending(self, proposal: Proposal, outcome: PendingOutcome) -> None:
        pending = self.state.pending_call
        if pending is not None and pending.proposal_id == proposal.id:
            pending.outcome = outcome

    def _note(self, summary: str) -> None:
        self.state.notes = [*self.state.notes, summary][-MAX_NOTES:]

    def _supersede(self, previous: Proposal | None, proposal: Proposal | None) -> None:
        if previous is not None and proposal is not None:
            self._pending(previous, SupersededOutcome(outcome="superseded", by=proposal.id))

    def _pick_slot(self, action: PickSlot) -> ActionOutcome:
        slot = self.state.slots.get(action.slot_ref)
        if slot is None:
            raise ActionError("invalid_action", "Choose a time from the current slots.")
        previous = self.state.proposal
        ref = slot.for_booking_ref
        booking = self._live_booking(ref) if ref is not None else None
        if parse_instant(slot.start_utc) <= self.now:
            return self._recover_slot(slot, ref, booking)
        try:
            if booking is None:
                proposal = self.tools.execute("propose_booking", {
                    "slot_ref": action.slot_ref, "name": "", "notes": "",
                })
            else:
                proposal = self.tools.execute("propose_reschedule", {
                    "booking_ref": ref, "slot_ref": action.slot_ref, "reason": "",
                })
        except ToolRejected as error:
            if "nearest" not in error.result:
                raise
            return self._recover_slot(slot, ref, booking)
        if booking is None:
            if self.state.guest_draft is None:
                self.state.guest_draft = GuestDraft()
            self.state.guest_draft.slot_ref = action.slot_ref
        self._supersede(previous, proposal.proposal)
        self.state.rescheduling_booking_ref = ref
        return ActionOutcome(proposal.events)

    def _ask_for_slots(self, event: EventType, first: date, ref: str | None, note: str) -> ActionOutcome:
        horizon = last_bookable_day(self.now.astimezone(self.zone).date())
        if first > horizon:
            raise ActionError("invalid_action", f"Choose a date on or before {horizon.isoformat()}.")
        last = date.fromordinal(min(first.toordinal() + 6, horizon.toordinal()))
        args = {
            "from_date": first.isoformat(),
            "to_date": last.isoformat(),
            "part_of_day": "any",
            "for_booking_ref": ref or "",
        }
        if len(self.tools.event_types()) > 1:
            args["event_type"] = event["slug"]
        outcome = self.tools.execute("find_slots", args)
        refs = [row["ref"] for row in outcome.result["slots"]]
        if not refs:
            return ActionOutcome([{"type": "assistant_text", "text": "There are no open times in that window."}])
        chips: SlotChipsEvent = {"type": "slot_chips", "refs": refs, "note": note}
        return ActionOutcome([*outcome.events, chips])

    def _more_slots(self, action: MoreSlots) -> ActionOutcome:
        ref = self.state.rescheduling_booking_ref
        booking = self._live_booking(ref) if ref is not None else None
        event = self._event(booking.event_type_id) if booking is not None else self.tools.default_event()
        first = self.now.astimezone(self.zone).date()
        if action.from_date is not None:
            first = max(first, date.fromisoformat(action.from_date))
        else:
            shown = [
                parse_instant(slot.start_utc).astimezone(self.zone).date()
                for slot in self.state.slots.values()
                if slot.for_booking_ref == ref and slot.event_type_id == event["id"]
            ]
            if shown:
                try:
                    first = max(first, max(shown) + timedelta(days=1))
                except OverflowError:
                    raise ActionError("invalid_action", "Choose a valid date range.") from None
        return self._ask_for_slots(event, first, ref, "Pick a time." if ref is None else "Pick a new time.")

    def _request_cancel(self, action: RequestCancel) -> ActionOutcome:
        self._live_booking(action.booking_ref)
        previous = self.state.proposal
        outcome = self.tools.execute("propose_cancel", {"booking_ref": action.booking_ref, "reason": ""})
        self._supersede(previous, outcome.proposal)
        self.state.rescheduling_booking_ref = None
        return ActionOutcome(outcome.events)

    def _request_reschedule(self, action: RequestReschedule, *, later: bool = False) -> ActionOutcome:
        booking = self._live_booking(action.booking_ref)
        event = self._event(booking.event_type_id)
        first = self.now.astimezone(self.zone).date()
        if later:
            self.tools.slots_after = parse_instant(booking.start_utc)
            first = max(first, self.tools.slots_after.astimezone(self.zone).date())
        outcome = self._ask_for_slots(event, first, action.booking_ref, "Pick a new time.")
        if self.state.proposal is not None:
            self._pending(self.state.proposal, NoActionYetOutcome(
                outcome="no_action_yet", note="The user requested new reschedule options."
            ))
            self.state.proposal = None
        self.state.rescheduling_booking_ref = action.booking_ref
        return outcome

    def _active_proposal(self, proposal_id: str) -> Proposal:
        proposal = self.state.proposal
        if proposal is None or proposal.id != proposal_id:
            raise ActionError("invalid_action", "That confirmation is no longer active.")
        return proposal

    def _dismiss(self, action: DismissProposal) -> ActionOutcome:
        proposal = self._active_proposal(action.proposal_id)
        self._pending(proposal, DismissedOutcome(outcome="dismissed"))
        self.state.proposal = None
        if proposal.kind == "book":
            self.state.guest_draft = None
        self.state.rescheduling_booking_ref = None
        self._note(f"Dismissed {proposal.id}.")
        return ActionOutcome([{"type": "assistant_text", "text": "Okay. The schedule is unchanged."}])

    def _fresh_slots(
        self, event_type_id: int, first: date, last: date, booking: BookingEntry | None
    ) -> list[tuple[datetime, datetime]]:
        try:
            begin, end = local_bounds(first, last, self.state.time_zone)
        except (ValueError, OverflowError):
            raise ActionError("invalid_action", "Choose a valid date range.") from None
        data = self.calendar.slots(
            event_type_id, begin, end, self.state.time_zone,
            booking_uid_to_reschedule=booking.uid if booking is not None else None, fresh=True,
        )
        existing_start = parse_instant(booking.start_utc) if booking is not None else None
        options = set()
        for ranges in data.values():
            for item in ranges:
                start, finish = parse_instant(item["start"]), parse_instant(item["end"])
                if start > self.now and begin <= start < end and start != existing_start:
                    options.add((start, finish))
        return sorted(options)

    def _recover_slot(
        self,
        slot: SlotEntry,
        ref: str | None,
        booking: BookingEntry | None,
        *,
        proposal: Proposal | None = None,
        options: list[tuple[datetime, datetime]] | None = None,
    ) -> ActionOutcome:
        selected = parse_instant(slot.start_utc)
        if options is None:
            options = self._recovery_options(slot, booking)
        alternatives = sorted(
            (window for window in options if window[0] != selected),
            key=lambda window: (abs(window[0] - selected), window[0]),
        )[:3]
        views = {}
        for start, end in alternatives:
            entry = SlotEntry(
                start_utc=start.isoformat(), end_utc=end.isoformat(), event_type_id=slot.event_type_id,
                fetched_at=self.now.timestamp(), for_booking_ref=ref,
            )
            views[add_slot(self.state, entry)] = slot_view(entry, self.state.time_zone)
        if proposal is not None:
            self.state.proposal = None
            self._pending(proposal, NoActionYetOutcome(outcome="no_action_yet", note="That time is no longer available."))
            self._note(f"Could not confirm {proposal.id}. That time is no longer available.")
        self.state.rescheduling_booking_ref = ref
        message = _SLOT_TAKEN if views else "That time is no longer available. Choose another date."
        events: list[ServerEvent] = [{"type": "error", "code": "slot_taken", "message": message, "retryable": False}]
        if views:
            refs: RefsEvent = {"type": "refs", "slots": views}
            chips: SlotChipsEvent = {"type": "slot_chips", "refs": list(views)}
            events.extend([refs, chips])
        return ActionOutcome(events, error_code="slot_taken")

    def _recovery_options(self, slot: SlotEntry, booking: BookingEntry | None) -> list[tuple[datetime, datetime]]:
        day = parse_instant(slot.start_utc).astimezone(self.zone).date()
        today = self.now.astimezone(self.zone).date()
        first = max(day - timedelta(days=3), today)
        last = max(day, today) + timedelta(days=3)
        return self._fresh_slots(slot.event_type_id, first, last, booking)

    def _confirm(self, action: ConfirmProposal) -> ActionOutcome:
        if action.proposal_id in self.state.completed:
            return ActionOutcome([{"type": "assistant_text", "text": self.state.completed[action.proposal_id]}])
        proposal = self._active_proposal(action.proposal_id)
        if self.ledger is None:
            return self._confirm_once(proposal)
        try:
            return self.ledger.execute(
                self.state, action, self.now, lambda: self._confirm_once(proposal),
            )
        except ConfirmationConflict:
            raise ActionError("invalid_action", "This confirmation was already submitted with different details.") from None

    def _confirm_once(self, proposal: Proposal) -> ActionOutcome:
        ref = proposal.booking_ref
        booking = self._live_booking(ref, target=proposal.booking_target) if ref is not None else None
        if proposal.kind == "cancel" and booking is not None and ref is not None:
            return self._confirm_cancel(proposal, ref, booking)
        return self._confirm_slot(proposal, booking)

    def _confirm_cancel(self, proposal: Proposal, ref: str, booking: BookingEntry) -> ActionOutcome:
        try:
            data = self.calendar.cancel_booking(booking.uid, proposal.reason or "User requested cancellation")
        except CalError as error:
            if error.code == "unavailable":
                self.wait(_WRITE_READBACK_DELAY_SECONDS)
                data = self._recover_cancel(proposal, booking)
            elif error.code == "upstream":
                return self._unverified(proposal, "I couldn’t verify whether the booking was cancelled.")
            else:
                raise
        if isinstance(data, ActionOutcome):
            return data
        entry = self._booking_result(data)
        if entry is None or entry.uid != booking.uid or entry.status != "cancelled":
            return self._unverified(proposal, "I couldn’t verify whether the booking was cancelled.")
        self.state.bookings[ref] = entry
        return self._complete(proposal, "cancelled", booking_view(ref, entry, self.state.time_zone))

    @staticmethod
    def _booking_result(data: Booking) -> BookingEntry | None:
        try:
            return booking_entry(data)
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _interval(entry: BookingEntry) -> tuple[datetime, datetime]:
        return parse_instant(entry.start_utc), parse_instant(entry.end_utc)

    def _matches_target(
        self, entry: BookingEntry, event_type_id: int, requested: tuple[datetime, datetime],
        email: str | None, original: BookingEntry | None,
    ) -> bool:
        if (
            entry.status not in _UPCOMING_STATUS or entry.event_type_id != event_type_id
            or self._interval(entry) != requested
        ):
            return False
        if original is None:
            return _has_attendee(entry, email)
        return entry.uid != original.uid and _same_attendees(entry, original)

    def _matching_bookings(
        self,
        email: str | None,
        event_type_id: int,
        requested: tuple[datetime, datetime],
        *,
        original: BookingEntry | None = None,
    ) -> list[Booking] | None:
        address = next(iter(original.attendee_emails), None) if original is not None else email
        if not address:
            return None
        matches: dict[str, Booking] = {}
        cursor = None
        seen: set[str] = set()
        for _ in range(_MAX_VERIFY_PAGES):
            try:
                page = self.calendar.list_bookings(attendee_email=address, status=None, limit=100, cursor=cursor)
            except CalError:
                return None
            for data in page["data"]:
                entry = self._booking_result(data)
                if entry is None:
                    return None
                if self._matches_target(entry, event_type_id, requested, email, original):
                    matches[entry.uid] = data
                    if len(matches) > 1:
                        return list(matches.values())
            if not page["pagination"]["hasMore"]:
                return list(matches.values())
            cursor = page["pagination"]["nextCursor"]
            if not cursor or cursor in seen:
                return None
            seen.add(cursor)
        return None

    def _original_after_write(self, before: BookingEntry) -> tuple[Booking, BookingEntry] | None:
        try:
            data = self.calendar.booking(before.uid)
        except CalError:
            return None
        entry = self._booking_result(data)
        if (
            entry is None or entry.uid != before.uid or entry.event_type_id != before.event_type_id
            or self._interval(entry) != self._interval(before) or not _same_attendees(entry, before)
        ):
            return None
        return data, entry

    @staticmethod
    def _not_applied(message: str) -> ActionOutcome:
        return ActionOutcome([{
            "type": "error", "code": "write_not_applied", "message": message, "retryable": True,
        }], error_code="write_not_applied")

    def _unverified(
        self, proposal: Proposal, message: str, *, partial: bool = False,
        original: BookingEntry | None = None,
    ) -> ActionOutcome:
        code = "reschedule_partial" if partial else "write_unverified"
        events: list[ServerEvent] = []
        if original is not None and proposal.booking_ref is not None:
            ref = proposal.booking_ref
            self.state.bookings[ref] = original
            events.append({"type": "refs", "bookings": {ref: booking_view(ref, original, self.state.time_zone)}})
        self.state.proposal = None
        self.state.rescheduling_booking_ref = None
        self._pending(proposal, NoActionYetOutcome(outcome="no_action_yet", note=message))
        self._note(f"Could not verify {proposal.id}. {message}")
        events.append({
            "type": "error", "code": code,
            "message": f"{message} Check Cal.com before submitting another change.", "retryable": False,
        })
        return ActionOutcome(events, error_code=code)

    def _recover_create(
        self, proposal: Proposal, email: str, event_type_id: int, requested: tuple[datetime, datetime],
    ) -> Booking | ActionOutcome:
        matches = self._matching_bookings(email, event_type_id, requested)
        if matches is not None and len(matches) == 1:
            return matches[0]
        if matches == []:
            return self._not_applied("Cal.com shows no booking for this attendee at the requested time. You can retry.")
        return self._unverified(proposal, "I couldn’t verify whether the booking was created.")

    def _recover_cancel(self, proposal: Proposal, before: BookingEntry) -> Booking | ActionOutcome:
        original = self._original_after_write(before)
        if original is not None:
            data, current = original
            if current.status == "cancelled":
                return data
            if current.status == before.status:
                return self._not_applied("Cal.com still shows the original booking as active. You can retry.")
        return self._unverified(proposal, "I couldn’t verify whether the booking was cancelled.")

    @staticmethod
    def _linked_replacement(original: Booking, replacement: Booking) -> bool:
        return (
            original.get("rescheduledToUid") in (None, "", replacement["uid"])
            and replacement.get("rescheduledFromUid") in (None, "", original["uid"])
        )

    def _linked_guestless_replacement(
        self, original: Booking, before: BookingEntry, event_type_id: int, requested: tuple[datetime, datetime],
    ) -> Booking | None:
        uid = original.get("rescheduledToUid")
        if not isinstance(uid, str) or not uid or uid == before.uid:
            return None
        try:
            replacement = self.calendar.booking(uid)
        except CalError:
            return None
        entry = self._booking_result(replacement)
        if entry is None or entry.uid != uid or not self._matches_target(entry, event_type_id, requested, None, before):
            return None
        return replacement

    def _recover_reschedule(
        self, proposal: Proposal, before: BookingEntry, event_type_id: int, requested: tuple[datetime, datetime],
    ) -> Booking | ActionOutcome:
        original = self._original_after_write(before)
        matches = self._matching_bookings(None, event_type_id, requested, original=before)
        replacement = matches[0] if matches is not None and len(matches) == 1 else None
        if original is not None:
            data, current = original
            if current.status == "cancelled":
                if not before.attendee_emails:
                    replacement = self._linked_guestless_replacement(data, before, event_type_id, requested)
                if replacement is not None and self._linked_replacement(data, replacement):
                    return replacement
                return self._unverified(
                    proposal, "Cal.com shows the original booking was cancelled, but I couldn’t verify its replacement.",
                    partial=True, original=current,
                )
            if current.status == before.status and matches == []:
                return self._not_applied("Cal.com still shows the booking at its original time. You can retry.")
            if current.status in _UPCOMING_STATUS and replacement is not None:
                return self._unverified(
                    proposal, "Cal.com shows the original booking is still active and a booking exists at the requested time.",
                    partial=True, original=current,
                )
        if replacement is not None:
            return self._unverified(
                proposal, "Cal.com shows a booking at the requested time, but I couldn’t verify the original booking.",
                partial=True,
            )
        return self._unverified(proposal, "I couldn’t verify whether the booking was rescheduled.")

    def _confirm_taken_slot(
        self, proposal: Proposal, slot: SlotEntry, booking: BookingEntry | None,
        requested: tuple[datetime, datetime], email: str,
        *, options: list[tuple[datetime, datetime]] | None = None,
    ) -> ActionOutcome:
        matches = self._matching_bookings(email, slot.event_type_id, requested, original=booking)
        match = matches[0] if matches is not None and len(matches) == 1 else None
        if booking is None and match is not None:
            return self._complete_slot(proposal, slot, booking, requested, email, match)
        if booking is not None and (match is not None or not booking.attendee_emails):
            original = self._original_after_write(booking)
            if original is not None and original[1].status == "cancelled":
                if not booking.attendee_emails:
                    match = self._linked_guestless_replacement(original[0], booking, slot.event_type_id, requested)
                if match is not None and self._linked_replacement(original[0], match):
                    return self._complete_slot(proposal, slot, booking, requested, email, match)
        return self._recover_slot(slot, proposal.booking_ref, booking, proposal=proposal, options=options)

    def _confirm_slot(self, proposal: Proposal, booking: BookingEntry | None) -> ActionOutcome:
        ref = proposal.booking_ref
        slot = self.state.slots.get(proposal.slot_ref or "")
        if slot is None or slot.for_booking_ref != ref or (
            booking is not None and slot.event_type_id != booking.event_type_id
        ):
            raise ActionError("invalid_action", "Choose a time for this booking from the current slots.")
        event = self._event(slot.event_type_id)
        name, email = (proposal.name or "").strip(), (proposal.email or "").strip()
        if proposal.kind == "book" and (not name or not EMAIL.fullmatch(email)):
            raise ActionError("invalid_contact", "I need the guest's name and email before booking.")
        start = parse_instant(slot.start_utc)
        if booking is not None and start == parse_instant(booking.start_utc):
            raise ActionError("invalid_action", "Choose a different start time for this booking.")
        requested = (start, parse_instant(slot.end_utc))
        day = start.astimezone(self.zone).date()
        if start <= self.now:
            return self._recover_slot(slot, ref, booking, proposal=proposal)
        if requested not in self._fresh_slots(slot.event_type_id, day, day, booking):
            return self._confirm_taken_slot(proposal, slot, booking, requested, email)
        try:
            if booking is not None:
                data = self.calendar.reschedule_booking(
                    booking.uid, start, proposal.reason or "User requested reschedule"
                )
            else:
                notes_field = next((
                    item["slug"] for item in event.get("bookingFields") or ()
                    if item.get("field") == "notes" and item.get("slug") and not item.get("hidden")
                ), None)
                data = self.calendar.create_booking(
                    start=start, event_type_id=slot.event_type_id, name=name, email=email,
                    time_zone=self.state.time_zone, notes=proposal.notes, notes_field=notes_field,
                )
        except CalError as error:
            if error.code == "unavailable":
                self.wait(_WRITE_READBACK_DELAY_SECONDS)
                data = (
                    self._recover_reschedule(proposal, booking, slot.event_type_id, requested)
                    if booking is not None else self._recover_create(proposal, email, slot.event_type_id, requested)
                )
            elif error.code == "upstream":
                action_name = "rescheduled" if booking is not None else "created"
                return self._unverified(proposal, f"I couldn’t verify whether the booking was {action_name}.")
            elif error.code == "slot_taken":
                return self._confirm_taken_slot(proposal, slot, booking, requested, email)
            elif error.status not in {400, 409, 422}:
                raise
            else:
                options = self._recovery_options(slot, booking)
                if requested in options:
                    raise
                return self._confirm_taken_slot(proposal, slot, booking, requested, email, options=options)
        else:
            if booking is not None and not booking.attendee_emails and data.get("rescheduledFromUid") != booking.uid:
                original = self._original_after_write(booking)
                if (
                    original is None or original[1].status != "cancelled"
                    or original[0].get("rescheduledToUid") != data.get("uid")
                ):
                    return self._unverified(proposal, "I couldn’t verify whether the booking was rescheduled.")

        if isinstance(data, ActionOutcome):
            return data
        return self._complete_slot(proposal, slot, booking, requested, email, data)

    def _complete_slot(
        self, proposal: Proposal, slot: SlotEntry, booking: BookingEntry | None,
        requested: tuple[datetime, datetime], email: str, data: Booking,
    ) -> ActionOutcome:
        ref = proposal.booking_ref
        entry = self._booking_result(data)
        if (
            entry is None or not self._matches_target(entry, slot.event_type_id, requested, email, booking)
            or (booking is not None and data.get("rescheduledFromUid") not in (None, "", booking.uid))
        ):
            action_name = "rescheduled" if booking is not None else "created"
            return self._unverified(proposal, f"I couldn’t verify whether the booking was {action_name}.")
        if booking is not None and ref is not None:
            booking.status = "cancelled"
            self.state.bookings[ref] = booking
            old = booking_view(ref, booking, self.state.time_zone)
            replacement = booking_view(add_booking(self.state, entry), entry, self.state.time_zone)
            result = self._complete(proposal, "rescheduled", replacement)
            return ActionOutcome([{"type": "refs", "bookings": {ref: old}}, *result.events])
        created = booking_view(add_booking(self.state, entry), entry, self.state.time_zone)
        return self._complete(proposal, "booked", created)

    def _complete(
        self, proposal: Proposal, kind: Literal["booked", "cancelled", "rescheduled"], booking: BookingView,
    ) -> ActionOutcome:
        when, title = booking["whenLabel"], booking["title"]
        if kind == "booked":
            prefix = "Booking requested" if booking["status"] == "pending" else "You're booked"
            summary = f"{prefix}: {title}, {when} ({self.state.time_zone})."
        elif kind == "cancelled":
            summary = f"Done. {title} on {when} is cancelled."
        else:
            summary = f"Moved {title} to {when} ({self.state.time_zone})."
        self.state.proposal = None
        self.state.rescheduling_booking_ref = None
        self.state.pending_change = None
        self.state.clear_time_choices()
        self._note(f"Confirmed {proposal.id}. {summary}")
        self.state.completed[proposal.id] = summary
        if len(self.state.completed) > MAX_COMPLETED:
            del self.state.completed[next(iter(self.state.completed))]
        self._pending(proposal, ConfirmedOutcome(outcome="confirmed", result=summary))
        result: BookingResultEvent = {"type": "booking_result", "kind": kind, "booking": booking}
        return ActionOutcome([result, {"type": "assistant_text", "text": summary}])
