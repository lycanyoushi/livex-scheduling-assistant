"""Plan or seed five fictional bookings on an isolated Cal.com test event."""

import argparse
from dataclasses import replace
from datetime import date, timedelta
from time import sleep
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.cal_client import Booking, CalClient, CalError
from backend.demo_calendar import DemoBooking, demo_bookings
from backend.time import local_bounds, parse_instant, utc_now
from scripts.cal_probe import CAL_BASE_URL, EMAIL, ProbeError, note_field, select_test_event
from scripts.local_environment import load_local_environment


_ACTIVE = {"accepted", "pending"}


def _aliases(settings: dict[str, str]) -> list[str]:
    addresses = [address.strip() for address in settings.get("CAL_DEMO_ATTENDEE_EMAILS", "").split(",")]
    if (
        len(addresses) != 5
        or len({address.casefold() for address in addresses}) != 5
        or not all(len(address) <= 254 and EMAIL.fullmatch(address) for address in addresses)
    ):
        raise ProbeError("Set five distinct test aliases you control in CAL_DEMO_ATTENDEE_EMAILS.")
    return addresses


def _matches_booking(booking: Booking, seed: DemoBooking, event_id: int) -> bool:
    return (
        booking.get("eventTypeId") == event_id
        and parse_instant(booking["start"]) == seed.start
        and parse_instant(booking["end"]) == seed.end
        and any((attendee.get("email") or "").casefold() == seed.email.casefold()
                for attendee in booking.get("attendees") or ())
    )


def _has_marker(booking: Booking, marker: str, field: str) -> bool:
    fields = booking.get("bookingFieldsResponses")
    return isinstance(fields, dict) and fields.get(field) == marker


def _find_created(
    client: CalClient, seed: DemoBooking, event_id: int, marker: str, field: str, uid: str | None,
) -> Booking | None:
    if uid is not None:
        booking = client.booking(uid)
        if booking["uid"] != uid or not _matches_booking(booking, seed, event_id) or not _has_marker(booking, marker, field):
            raise ProbeError("A known booking did not match this seed run.")
        return booking

    page = client.list_bookings(
        attendee_email=seed.email, status=None, after_start=seed.start, before_end=seed.end, limit=100,
    )
    if page["pagination"]["hasMore"]:
        raise ProbeError("A seed recovery search exceeded one page.")
    matches = []
    for candidate in page["data"]:
        if _matches_booking(candidate, seed, event_id):
            booking = client.booking(candidate["uid"])
            if (
                booking["uid"] == candidate["uid"]
                and _matches_booking(booking, seed, event_id)
                and _has_marker(booking, marker, field)
            ):
                matches.append(booking)
    if len(matches) > 1:
        raise ProbeError("More than one booking matched this seed run.")
    return next(iter(matches), None)


def _rollback(
    client: CalClient, attempted: dict[DemoBooking, str | None], event_id: int, marker: str, field: str,
) -> tuple[int, int]:
    cancelled, unresolved = 0, 0
    for seed, uid in reversed(attempted.items()):
        try:
            booking = _find_created(client, seed, event_id, marker, field, uid)
            if booking is None:
                continue
            if booking["status"] != "cancelled":
                try:
                    client.cancel_booking(booking["uid"], "Synthetic demo seed rollback")
                except CalError:
                    # A timed-out cancellation may already have taken effect.
                    pass
                booking = _find_created(client, seed, event_id, marker, field, booking["uid"])
            if booking is not None and booking["status"] == "cancelled":
                cancelled += 1
            else:
                unresolved += 1
        except (CalError, ProbeError, TypeError, ValueError, KeyError):
            unresolved += 1
    return cancelled, unresolved


def _write_seeds(
    client: CalClient, seeds: tuple[DemoBooking, ...], event_id: int, zone: str, field: str,
) -> None:
    marker = f"Synthetic scheduling demo seed {uuid4().hex}"
    attempted: dict[DemoBooking, str | None] = {}
    try:
        for seed in seeds:
            attempted[seed] = None
            try:
                created = client.create_booking(
                    start=seed.start, event_type_id=event_id, name=seed.name, email=seed.email,
                    time_zone=zone, notes=marker, notes_field=field,
                )
            except CalError as error:
                if error.status is not None and 400 <= error.status < 500 and error.status != 408:
                    del attempted[seed]
                else:
                    sleep(2)
                raise
            if not _matches_booking(created, seed, event_id):
                raise ProbeError("Cal.com returned a booking that did not match its seed.")
            attempted[seed] = created["uid"]
            verified = _find_created(client, seed, event_id, marker, field, created["uid"])
            if verified is None or verified["status"] not in _ACTIVE:
                raise ProbeError("Could not verify an active seed booking.")
    except (Exception, KeyboardInterrupt):
        cancelled, unresolved = _rollback(client, attempted, event_id, marker, field)
        print(f"Rollback verified {cancelled} cancelled bookings. Unresolved attempts: {unresolved}.")
        raise


def run(*, write_test: bool = False, start_date: date | None = None, controlled_aliases: bool = False) -> None:
    settings = load_local_environment()
    if not settings.get("CAL_API_KEY"):
        raise ProbeError("Set CAL_API_KEY in .env.local for the isolated test account.")
    if write_test and not settings.get("CAL_DEFAULT_EVENT_TYPE_SLUG"):
        raise ProbeError("Set CAL_DEFAULT_EVENT_TYPE_SLUG to the isolated test event.")
    if settings.get("CAL_API_BASE_URL", CAL_BASE_URL).rstrip("/") != CAL_BASE_URL:
        raise ProbeError("The seeder only supports the official Cal.com API host.")
    aliases = _aliases(settings) if controlled_aliases else None

    client = CalClient(settings["CAL_API_KEY"], CAL_BASE_URL)
    profile = client.me(retry=False)
    event = select_test_event(client.event_types(profile["username"]), profile, settings)
    field = note_field(event)
    if field is None:
        raise ProbeError("Enable the built-in notes field on the test event for verifiable rollback.")
    try:
        zone = ZoneInfo(profile.get("timeZone") or "")
    except (ValueError, ZoneInfoNotFoundError):
        raise ProbeError("The test calendar needs a valid IANA time zone.") from None
    now = utc_now()
    first_day = start_date or now.astimezone(zone).date()
    seeds = demo_bookings(first_day, zone.key)
    if aliases is not None:
        seeds = tuple(replace(seed, email=email) for seed, email in zip(seeds, aliases, strict=True))
    if any(seed.start <= now for seed in seeds):
        raise ProbeError("The seed window contains a past time. Use --start-date for a future three-day window.")

    begin, end = local_bounds(first_day, first_day + timedelta(days=2), zone.key)
    open_slots = client.slots(event["id"], begin, end, zone.key, fresh=True)
    available = {
        (parse_instant(slot["start"]), parse_instant(slot["end"]))
        for slots in open_slots.values() for slot in slots
    }
    missing = sum((seed.start, seed.end) not in available for seed in seeds)
    if missing:
        raise ProbeError(f"{missing} of five seed intervals are unavailable. Choose another three-day window.")
    print(f"Verified five test openings in {zone.key}.")
    for seed in seeds:
        print(f"  {seed.start.astimezone(zone):%Y-%m-%d %H:%M}")
    if not write_test:
        print("Dry run complete. No bookings were changed.")
        return
    _write_seeds(client, seeds, event["id"], zone.key, field)
    print("Created and verified five synthetic test bookings.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Read-only plan. This is the default.")
    mode.add_argument("--write-test", action="store_true", help="Create bookings on the configured test event.")
    parser.add_argument("--start-date", type=date.fromisoformat, help="First day in the owner's time zone. Defaults to today.")
    parser.add_argument(
        "--controlled-aliases", action="store_true",
        help="Use five inbox aliases from CAL_DEMO_ATTENDEE_EMAILS instead of the fictional example.com addresses.",
    )
    args = parser.parse_args()
    try:
        run(write_test=args.write_test, start_date=args.start_date, controlled_aliases=args.controlled_aliases)
    except ProbeError as error:
        print(error)
        return 1
    except CalError as error:
        print(f"Cal.com seed request failed with HTTP {error.status} and classification {error.code}.")
        return 1
    except KeyboardInterrupt:
        print("Seeder interrupted.")
        return 130
    except Exception as error:
        print(f"Seeder stopped with {type(error).__name__}.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
