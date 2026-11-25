"""Check or enable many meeting lengths on the isolated Cal.com test event.

Cal.com books one event type at several lengths when the event lists them in
`lengthInMinutesOptions` (the "multiple durations" setting). The assistant then
asks Cal.com for openings and bookings at the requested length. The default run
is read-only. `--apply` changes only the configured test event's length list.
"""

import argparse
from datetime import timedelta

from backend.cal_client import CalClient, CalError, event_lengths
from backend.time import local_bounds, parse_instant, utc_now
from backend.tools import describe_lengths
from scripts.cal_probe import CAL_BASE_URL, ProbeError, select_test_event
from scripts.local_environment import load_local_environment


LENGTHS = tuple(range(5, 481, 5))
_CHECK_LENGTH = 120


def run(*, apply: bool = False) -> None:
    settings = load_local_environment()
    if not settings.get("CAL_API_KEY"):
        raise ProbeError("Set CAL_API_KEY in .env.local for the isolated test account.")
    if apply and not settings.get("CAL_DEFAULT_EVENT_TYPE_SLUG"):
        raise ProbeError("Set CAL_DEFAULT_EVENT_TYPE_SLUG to the isolated test event.")
    if settings.get("CAL_API_BASE_URL", CAL_BASE_URL).rstrip("/") != CAL_BASE_URL:
        raise ProbeError("This script only supports the official Cal.com API host.")

    client = CalClient(settings["CAL_API_KEY"], CAL_BASE_URL)
    profile = client.me(retry=False)
    event = select_test_event(client.event_types(profile["username"]), profile, settings)
    current = event_lengths(event)
    print(f"Test event {event['slug']} offers {describe_lengths(current)}.")
    wanted = tuple(sorted({event["lengthInMinutes"], *LENGTHS}))
    if set(wanted) <= set(current):
        print("Every planned length is already enabled.")
    elif not apply:
        print(f"Planned change: offer {describe_lengths(wanted)}. Run with --apply to save it.")
        return
    else:
        updated = client.update_event_type(event["id"], {"lengthInMinutesOptions": list(wanted)})
        if not set(wanted) <= set(event_lengths(updated)):
            raise ProbeError("Cal.com saved a different list of lengths. Check the event's duration settings.")
        print(f"Saved. The test event now offers {describe_lengths(event_lengths(updated))}.")

    # Read-only check that Cal.com returns openings of a longer length.
    zone = profile.get("timeZone") or "UTC"
    today = utc_now().date()
    begin, end = local_bounds(today + timedelta(days=1), today + timedelta(days=7), zone)
    days = client.slots(event["id"], begin, end, zone, duration=_CHECK_LENGTH, fresh=True)
    spans = {
        int((parse_instant(slot["end"]) - parse_instant(slot["start"])) // timedelta(minutes=1))
        for slots in days.values() for slot in slots
    }
    count = sum(len(slots) for slots in days.values())
    if spans and spans != {_CHECK_LENGTH}:
        raise ProbeError(f"Cal.com returned {sorted(spans)}-minute openings for a {_CHECK_LENGTH}-minute request.")
    print(f"Cal.com returned {count} openings of {_CHECK_LENGTH} minutes over the next week.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Save the length list on the configured test event.")
    args = parser.parse_args()
    try:
        run(apply=args.apply)
    except ProbeError as error:
        print(error)
        return 1
    except CalError as error:
        print(f"Cal.com request failed with HTTP {error.status} and classification {error.code}.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
