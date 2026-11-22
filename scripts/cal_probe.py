"""Exercise Cal.com's live API on the configured test account."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import re
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from backend.cal_client import CalClient, CalError, CalObservation, SLOTS_VERSION, USER_AGENT
from scripts.local_environment import load_local_environment


CAL_BASE_URL = "https://api.cal.com"
PROBE_NOTE = "Synthetic scheduling assistant API probe."
EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


class ProbeError(RuntimeError):
    """Report a probe failure without exposing upstream values."""


def report_response(observation: CalObservation) -> None:
    path = observation.path
    if path.startswith("/v2/bookings/"):
        path = "/v2/bookings/{uid}" + ("/cancel" if path.endswith("/cancel") else "")
    status = "cache hit" if observation.cache_hit else observation.status or "no response"
    print(f"{observation.method} {path}  {status}", flush=True)


def select_test_event(events: list[dict], profile: dict, settings: dict) -> dict:
    slug = settings.get("CAL_DEFAULT_EVENT_TYPE_SLUG")
    allowed_slugs = {value.strip() for value in settings.get("CAL_EVENT_TYPE_SLUGS", "").split(",") if value.strip()}
    choices = [
        event for event in events
        if event.get("hidden") is False and event["lengthInMinutes"] == 30
        and (not slug or event["slug"] == slug)
        and (not allowed_slugs or event["slug"] in allowed_slugs)
    ]
    if len(choices) != 1:
        raise ProbeError("Select one visible 30-minute test event with CAL_DEFAULT_EVENT_TYPE_SLUG.")
    event = choices[0]
    if profile.get("id") is None or event.get("ownerId") != profile["id"]:
        raise ProbeError("The configured event must belong to the authenticated test account.")
    unsupported = (
        "seatsPerTimeSlot", "recurrence", "recurringEvent", "requiresConfirmation",
        "bookingRequiresAuthentication", "requiresBookerEmailVerification", "isPaid", "price", "teamId",
    )
    if any(event.get(flag) for flag in unsupported):
        raise ProbeError("The configured event must allow a standard individual booking.")
    return event


def note_field(event: dict) -> str | None:
    for field in event.get("bookingFields") or []:
        if field.get("field") == field.get("slug") == "notes" and field.get("hidden") is not True:
            return "notes"
    return None


def public_slots(event_id: int, begin: str, end: str) -> None:
    params = {"eventTypeId": event_id, "start": begin, "end": end, "timeZone": "UTC", "format": "range"}
    request = Request(f"{CAL_BASE_URL}/v2/slots?{urlencode(params)}", headers={
        "Accept": "application/json", "User-Agent": USER_AGENT, "cal-api-version": SLOTS_VERSION,
    })
    try:
        with urlopen(request, timeout=6) as response:
            body = json.load(response)
            if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
                raise ProbeError("The public slots control did not return the expected envelope.")
            print(f"GET /v2/slots without credentials  {response.status}")
    except HTTPError as error:
        with error:
            print(f"GET /v2/slots without credentials  {error.code}")


def report_write_error(label: str, error: CalError) -> None:
    body = error.body if isinstance(error.body, dict) else {}
    details = body.get("error") if isinstance(body.get("error"), dict) else {}
    message = str(details.get("message", "")).lower()
    markers = {
        "availability": ("already booked", "already taken", "not available", "no available", "unavailable"),
        "booking fields": ("bookingfields", "booking fields", "booking field", "required field", "additional note"),
        "email": ("email", "domain", "disposable", "spam"),
        "invalid email": ("invalid email", "valid email", "must be an email", "email is invalid"),
        "mail deliverability": ("mx record", "dns", "deliverab", "mail server", "does not exist"),
        "blocked domain": ("disposable", "blocklist", "blacklist", "not allowed", "blocked"),
        "email verification": ("verify", "verification"),
        "location": ("location", "meetingurl", "conferencing"),
        "time": ("start", "endtime", "timezone", "duration", "past"),
        "attendee": ("attendee", "booker", "name", "phone", "language"),
        "payment": ("payment", "stripe"),
        "authorization": ("permission", "authentication", "unauthorized", "forbidden"),
    }
    categories = [name for name, tokens in markers.items() if any(token in message for token in tokens)]
    print(f"{label} response  HTTP {error.status}, client classification {error.code}")
    print(f"Error envelope  status=error: {body.get('status') == 'error'}, error object: {bool(details)}, "
          f"code present: {'code' in details}, message present: {bool(message)}, categories: {', '.join(categories)}")


def matches_probe(booking: dict, email: str, event_id: int, start: datetime) -> bool:
    return (
        booking.get("eventTypeId") == event_id and datetime.fromisoformat(booking["start"]) == start
        and any(str(attendee.get("email", "")).casefold() == email.casefold()
                for attendee in booking.get("attendees") or [])
    )


def reconcile(client: CalClient, email: str, event_id: int, start: datetime) -> str | None:
    page = client.list_bookings(attendee_email=email, limit=5)
    matches = [booking["uid"] for booking in page["data"] if matches_probe(booking, email, event_id, start)]
    if len(matches) > 1 or page["pagination"]["hasMore"]:
        raise ProbeError("The synthetic booking could not be identified uniquely for cleanup.")
    return next(iter(matches), None)


def run_write_test(client: CalClient, event: dict, start: datetime, email: str, conflict_email: str) -> None:
    event_id = event["id"]
    field = note_field(event)
    bookings_to_cancel: dict[str, set[str]] = {}
    uncertain: set[str] = set()

    def create(attendee_email: str, name: str) -> dict:
        uncertain.add(attendee_email)
        try:
            booking = client.create_booking(start=start, event_type_id=event_id, name=name,
                                            email=attendee_email, time_zone="UTC", notes=PROBE_NOTE,
                                            notes_field=field)
        except CalError as error:
            if error.status is not None and 400 <= error.status < 500:
                uncertain.discard(attendee_email)
            elif error.status is not None and 200 <= error.status < 300:
                body = error.body if isinstance(error.body, dict) else {}
                returned = body.get("data")
                uid = returned.get("uid") if isinstance(returned, dict) else None
                if body.get("status") == "success" and isinstance(uid, str) and uid:
                    bookings_to_cancel.setdefault(uid, set()).add(attendee_email)
                    uncertain.discard(attendee_email)
            raise
        bookings_to_cancel.setdefault(booking["uid"], set()).add(attendee_email)
        uncertain.discard(attendee_email)
        if not matches_probe(booking, attendee_email, event_id, start):
            raise ProbeError("The create response did not match the synthetic attendee, event and time.")
        return booking

    try:
        try:
            booking = create(email, "Synthetic Calendar Probe")
        except CalError as error:
            report_write_error("Create", error)
            raise
        found = client.booking(booking["uid"])
        if found["uid"] != booking["uid"] or not matches_probe(found, email, event_id, start):
            raise ProbeError("The single-booking read did not match the created booking.")
        if reconcile(client, email, event_id, start) != booking["uid"]:
            raise ProbeError("The filtered bookings list did not return the created booking.")
        notes_match = field is not None and (found.get("bookingFieldsResponses") or {}).get(field) == PROBE_NOTE
        print(f"Synthetic create, single-booking read and filtered list agree. Notes round trip: {notes_match}")
        if field and not notes_match:
            raise ProbeError("The advertised notes field did not round trip.")

        try:
            create(conflict_email, "Synthetic Occupied Slot Probe")
        except CalError as error:
            report_write_error("Occupied-slot", error)
            if error.code != "slot_taken":
                raise ProbeError("The occupied time did not classify as slot_taken.") from None
        else:
            raise ProbeError("Cal.com accepted a second booking for the same slot.")
    finally:
        cleanup_failed = False
        for address in uncertain:
            try:
                match = reconcile(client, address, event_id, start)
            except (CalError, ProbeError):
                match = None
            if match is None:
                cleanup_failed = True
            else:
                bookings_to_cancel.setdefault(match, set()).add(address)

        for uid, addresses in bookings_to_cancel.items():
            try:
                current = client.booking(uid)
            except CalError:
                cleanup_failed = True
                continue
            if not any(matches_probe(current, address, event_id, start) for address in addresses):
                cleanup_failed = True
                continue
            try:
                client.cancel_booking(uid, "Synthetic API probe cleanup")
            except CalError:
                # Check for the effect without repeating an uncertain cancellation.
                pass
            try:
                cancelled = client.booking(uid)["status"] == "cancelled"
            except CalError:
                cancelled = False
            cleanup_failed |= not cancelled
            print(f"Synthetic booking cancellation verified: {cancelled}")

        if cleanup_failed:
            raise ProbeError("Cleanup needs verification in the test account.") from None


def test_addresses(settings: dict, write_test: bool) -> tuple[str, str]:
    if not write_test:
        nonce = uuid4().hex
        return f"livex-probe-{nonce}@example.com", f"livex-conflict-{nonce}@example.com"
    first = settings.get("CAL_PROBE_ATTENDEE_EMAIL", "")
    second = settings.get("CAL_PROBE_CONFLICT_EMAIL", "")
    if not EMAIL.fullmatch(first) or not EMAIL.fullmatch(second) or first.casefold() == second.casefold():
        raise ProbeError("Set two distinct test email aliases you control in .env.local before using --write-test.")
    return first, second


def run(write_test: bool) -> None:
    settings = load_local_environment()
    if not settings.get("CAL_API_KEY"):
        raise ProbeError("Set CAL_API_KEY in .env.local for the test account.")
    if settings.get("CAL_API_BASE_URL", CAL_BASE_URL).rstrip("/") != CAL_BASE_URL:
        raise ProbeError("The live probe only supports the official Cal.com API host.")
    email, conflict_email = test_addresses(settings, write_test)

    client = CalClient(settings["CAL_API_KEY"], CAL_BASE_URL, observer=report_response)
    profile = client.me()
    print("Cal.com returned the account profile without a version header.")
    event = select_test_event(client.event_types(profile["username"]), profile, settings)
    print(f"Selected one visible 30-minute account-owned event. Built-in notes field: {bool(note_field(event))}")

    now = datetime.now(timezone.utc)
    begin = now.date().isoformat()
    end = (now.date() + timedelta(days=7)).isoformat()
    slots = client.slots(event["id"], begin, end)
    starts = [datetime.fromisoformat(item["start"]) for day in slots.values() for item in day]
    print(f"Slots returned a date-keyed map of ranges. Non-empty: {bool(starts)}")
    public_slots(event["id"], begin, end)

    page = client.list_bookings(attendee_email=email, limit=5)
    print(f"Filtered booking list returned {len(page['data'])} entries with cursor pagination.")
    if not write_test:
        return
    start = min((candidate for candidate in starts if candidate > now + timedelta(hours=1)), default=None)
    conflict_page = client.list_bookings(attendee_email=conflict_email, limit=5)
    if start is None or page["data"] or conflict_page["data"]:
        raise ProbeError("The probe requires a free future time and test aliases without upcoming bookings.")
    run_write_test(client, event, start, email, conflict_email)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-test", action="store_true", help="Create and clean up a synthetic test booking.")
    args = parser.parse_args()
    try:
        run(args.write_test)
    except ProbeError as error:
        print(error)
        return 1
    except CalError as error:
        print(f"Cal.com probe failed with HTTP {error.status} and classification {error.code}.")
        return 1
    except Exception as error:
        print(f"Cal.com probe stopped with {type(error).__name__}.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
