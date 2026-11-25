from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from backend.cal_client import CalError
from backend.demo_calendar import DemoCalendar
from backend.time import local_bounds, parse_instant
from scripts import seed_demo


_ZONE = ZoneInfo("America/Los_Angeles")
_NOW = datetime.fromisoformat("2027-03-12T09:00:00-08:00")
_SETTINGS = {"CAL_API_KEY": "fixture-private-key", "CAL_DEFAULT_EVENT_TYPE_SLUG": "30-minute-meeting"}
_CLOCKS = ["Fri 15:00 -0800", "Sat 10:00 -0800", "Sat 14:00 -0800", "Sun 15:00 -0700", "Sun 15:30 -0700"]


def _local_starts(bookings):
    return [parse_instant(booking["start"]).astimezone(_ZONE).strftime("%a %H:%M %z") for booking in bookings]


class SeedCalendar(DemoCalendar):
    def __init__(self, failure=None):
        super().__init__(clock=lambda: _NOW)
        self._bookings.clear()
        self.failure = failure
        self.creates = 0
        self.cancellations = 0

    def me(self, *, retry=True):
        return super().me()

    def create_booking(self, **kwargs):
        self.creates += 1
        if self.creates == 3 and self.failure == "reject":
            raise CalError("invalid", "Synthetic rejection.", status=400)
        booking = super().create_booking(**kwargs)
        if self.creates == 3 and self.failure == "timeout":
            raise CalError("unavailable", "Synthetic timeout after creating the booking.")
        return booking

    def cancel_booking(self, uid, reason="User requested cancellation"):
        self.cancellations += 1
        return super().cancel_booking(uid, reason)


class DemoCalendarSeedTests(unittest.TestCase):
    def test_new_calendar_has_five_seeded_bookings_and_three_days_of_openings(self):
        calendar = DemoCalendar(clock=lambda: _NOW)
        meetings = calendar.list_bookings()["data"]
        self.assertEqual(_local_starts(meetings), _CLOCKS)
        self.assertEqual(len({row["attendees"][0]["email"] for row in meetings}), 5)
        self.assertTrue(all(row["attendees"][0]["email"].endswith("@example.com") for row in meetings))
        begin, end = local_bounds(_NOW.date(), datetime.fromisoformat("2027-03-15").date(), _ZONE.key)
        event = calendar.event_types(calendar.me()["username"])[0]
        days = calendar.slots(event["id"], begin, end, _ZONE.key)
        self.assertEqual(list(days), ["2027-03-12", "2027-03-13", "2027-03-14", "2027-03-15"])
        starts = {parse_instant(slot["start"]) for slots in days.values() for slot in slots}
        self.assertTrue(all(parse_instant(row["start"]) not in starts for row in meetings))
        self.assertTrue(all(any(parse_instant(slot["start"]).astimezone(_ZONE).hour == 16 for slot in slots)
                            for slots in days.values()))

    def test_late_start_rolls_all_five_openings_to_tomorrow(self):
        calendar = DemoCalendar(clock=lambda: _NOW.replace(hour=18))
        self.assertEqual(calendar.list_bookings(status="past")["data"], [])
        self.assertEqual(_local_starts(calendar.list_bookings()["data"]), [
            "Sat 15:00 -0800", "Sun 10:00 -0700", "Sun 14:00 -0700", "Mon 15:00 -0700", "Mon 15:30 -0700",
        ])

    def test_configured_zone_applies_to_daily_cutoff_and_opening_hours(self):
        zone = ZoneInfo("Asia/Singapore")
        before = datetime.fromisoformat("2027-03-12T05:59:59Z")
        at_cutoff = datetime.fromisoformat("2027-03-12T06:00:00Z")
        for instant, first_day in ((before, "2027-03-12"), (at_cutoff, "2027-03-13")):
            with self.subTest(instant=instant):
                calendar = DemoCalendar(clock=lambda: instant, time_zone=zone.key)
                self.assertEqual(calendar.me()["timeZone"], zone.key)
                meetings = calendar.list_bookings()["data"]
                self.assertEqual(len(meetings), 5)
                first_start = parse_instant(meetings[0]["start"]).astimezone(zone)
                self.assertEqual(first_start.isoformat(), f"{first_day}T15:00:00+08:00")
                self.assertGreater(first_start, instant)

        calendar = DemoCalendar(clock=lambda: at_cutoff, time_zone=zone.key)
        event = calendar.event_types(calendar.me()["username"])[0]
        slots = calendar.slots(event["id"], "2027-03-13", "2027-03-13", zone.key)["2027-03-13"]
        self.assertEqual(slots[0]["start"], "2027-03-13T09:00:00+08:00")
        self.assertEqual(slots[-1]["end"], "2027-03-13T17:00:00+08:00")

    def test_invalid_demo_time_zone_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "DEMO_TIME_ZONE"):
            DemoCalendar(time_zone="Invalid/Zone")


class LiveSeederTests(unittest.TestCase):
    def setUp(self):
        self.settings = dict(_SETTINGS)
        self.output = StringIO()
        self.enterContext(patch("scripts.seed_demo.load_local_environment", return_value=self.settings))
        self.client = self.enterContext(patch("scripts.seed_demo.CalClient"))
        self.clock = self.enterContext(patch("scripts.seed_demo.utc_now", return_value=_NOW))
        self.wait = self.enterContext(patch("scripts.seed_demo.sleep"))

    def assert_sanitized(self):
        output = self.output.getvalue()
        self.assertNotIn("@", output)
        self.assertNotIn("fixture-private-key", output)
        self.assertNotIn("fixture-created-", output)

    def test_default_is_dry_run_and_accepts_the_only_eligible_event(self):
        self.settings.pop("CAL_DEFAULT_EVENT_TYPE_SLUG")
        calendar = SeedCalendar()
        self.client.return_value = calendar
        with redirect_stdout(self.output):
            seed_demo.run()
        self.assertEqual(calendar.creates, 0)
        self.assertIn("2027-03-12 15:00", self.output.getvalue())
        self.assertIn("Dry run complete", self.output.getvalue())
        self.assert_sanitized()

    def test_write_uses_all_five_reserved_example_addresses(self):
        calendar = SeedCalendar()
        self.client.return_value = calendar
        with redirect_stdout(self.output):
            seed_demo.run(write_test=True)
        meetings = calendar.list_bookings()["data"]
        self.assertEqual(_local_starts(meetings), _CLOCKS)
        self.assertEqual(calendar.creates, 5)
        self.assertTrue(all(row["attendees"][0]["email"].endswith("@example.com") for row in meetings))
        self.assertEqual(len({row["bookingFieldsResponses"]["notes"] for row in meetings}), 1)
        self.assert_sanitized()

    def test_controlled_aliases_are_used_only_with_the_explicit_option(self):
        aliases = [f"fixture+{index}@mail.example.org" for index in range(5)]
        self.settings["CAL_DEMO_ATTENDEE_EMAILS"] = ",".join(aliases)
        calendar = SeedCalendar()
        self.client.return_value = calendar
        with redirect_stdout(self.output):
            seed_demo.run(write_test=True, controlled_aliases=True)
        self.assertEqual([row["attendees"][0]["email"] for row in calendar.list_bookings()["data"]], aliases)
        self.assert_sanitized()

    def test_write_requires_explicit_event_selection(self):
        self.settings.pop("CAL_DEFAULT_EVENT_TYPE_SLUG")
        with self.assertRaisesRegex(seed_demo.ProbeError, "CAL_DEFAULT_EVENT_TYPE_SLUG"):
            seed_demo.run(write_test=True)
        self.client.assert_not_called()

    def test_past_or_blocked_intervals_abort_before_any_write(self):
        calendar = SeedCalendar()
        self.client.return_value = calendar
        self.clock.return_value = _NOW.replace(hour=17)
        with self.assertRaisesRegex(seed_demo.ProbeError, "past time"):
            seed_demo.run(write_test=True)
        self.clock.return_value = _NOW
        calendar._occupied.add(parse_instant(_NOW.replace(hour=15)))
        with self.assertRaisesRegex(seed_demo.ProbeError, "1 of five"):
            seed_demo.run(write_test=True)
        self.assertEqual(calendar.creates, 0)

    def test_partial_rollback_also_reconciles_an_applied_timeout(self):
        for failure, count in (("reject", 2), ("timeout", 3)):
            with self.subTest(failure=failure):
                calendar = SeedCalendar(failure)
                self.client.return_value = calendar
                self.wait.reset_mock()
                with redirect_stdout(self.output), self.assertRaises(CalError):
                    seed_demo.run(write_test=True)
                self.assertEqual(calendar.creates, 3)
                self.assertEqual(calendar.cancellations, count)
                self.assertEqual(calendar.list_bookings()["data"], [])
                self.assertEqual(len(calendar.list_bookings(status="cancelled")["data"]), count)
                self.assertEqual(self.wait.call_count, int(failure == "timeout"))
                self.assertIn(f"Rollback verified {count} cancelled bookings.", self.output.getvalue())
                self.assert_sanitized()


if __name__ == "__main__":
    unittest.main()
