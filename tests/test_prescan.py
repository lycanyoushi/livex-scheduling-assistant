from datetime import date, datetime, time, timedelta
import unittest
from zoneinfo import ZoneInfo

from backend.prescan import ATTACHED_TIME, DateHint, _requested_time, prescan, warm_prescan


ZONES = ("Asia/Singapore", "America/New_York")


class PrescanTests(unittest.TestCase):
    def test_at_ten_with_no_meridiem_is_ten_am(self):
        self.assertEqual(_requested_time(ATTACHED_TIME.match("at 10")), time(10))

    def scan(self, phrase, zone, now=None):
        reference = now or datetime(2026, 11, 23, 10, tzinfo=ZoneInfo(zone))
        return prescan(phrase, now=reference, time_zone=zone)

    def test_required_phrases_in_both_zones(self):
        for zone in ZONES:
            with self.subTest(zone=zone):
                afternoon = self.scan("Thursday afternoon", zone)
                self.assertEqual(afternoon.dates, (DateHint(date(2026, 11, 26)),))
                self.assertEqual(afternoon.part_of_day, "afternoon")
                self.assertEqual(self.scan("next Friday", zone).dates, (DateHint(date(2026, 12, 4)),))
                self.assertEqual(self.scan("tomorrow 9am", zone).dates, (DateHint(date(2026, 11, 24), time(9)),))
                self.assertEqual(self.scan("the 3rd", zone).dates, (DateHint(date(2026, 12, 3)),))

    def test_same_day_weekday_rule_in_both_zones(self):
        examples = {
            "Monday": DateHint(date(2026, 11, 23)),
            "Monday at 3": DateHint(date(2026, 11, 23), time(15)),
            "Monday at 10": DateHint(date(2026, 11, 30), time(10)),
            "Monday at 11am": DateHint(date(2026, 11, 23), time(11)),
            "Monday 10am": DateHint(date(2026, 11, 30), time(10)),
            "Monday 9am": DateHint(date(2026, 11, 30), time(9)),
            "at 9am Monday": DateHint(date(2026, 11, 30), time(9)),
        }
        for zone in ZONES:
            for phrase, expected in examples.items():
                with self.subTest(zone=zone, phrase=phrase):
                    self.assertEqual(self.scan(phrase, zone).dates, (expected,))

    def test_bare_hours_and_explicit_clock_times_in_both_zones(self):
        tomorrow = date(2026, 11, 24)
        examples = {
            "tomorrow at 1": time(13),
            "tomorrow at 3": time(15),
            "tomorrow at 7": time(19),
            "tomorrow at 8": time(8),
            "tomorrow at 10": time(10),
            "tomorrow at 12": time(12),
            "tomorrow at 23": time(23),
            "tomorrow at 3am": time(3),
            "tomorrow at 3pm": time(15),
            "tomorrow at 12am": time(0),
            "tomorrow at 12pm": time(12),
            "tomorrow at 3:00": time(3),
            "tomorrow at 07:45": time(7, 45),
            "tomorrow at 3:30pm": time(15, 30),
            "tomorrow at 3:60": None,
            "mañana a las 3": time(15),
        }
        for zone in ZONES:
            for phrase, expected in examples.items():
                with self.subTest(zone=zone, phrase=phrase):
                    self.assertEqual(self.scan(phrase, zone).dates, (DateHint(tomorrow, expected),))

    def test_relative_dates_start_from_the_users_local_day(self):
        instant = datetime.fromisoformat("2026-11-23T02:00:00Z")
        self.assertEqual(self.scan("tomorrow 9am", ZONES[0], instant).dates, (DateHint(date(2026, 11, 24), time(9)),))
        self.assertEqual(self.scan("tomorrow 9am", ZONES[1], instant).dates, (DateHint(date(2026, 11, 23), time(9)),))

    def test_week_range_and_explicit_past(self):
        self.assertEqual(
            self.scan("sometime next week", ZONES[0]).dates,
            tuple(DateHint(date(2026, 11, 30) + timedelta(days=index)) for index in range(5)),
        )
        self.assertEqual(self.scan("yesterday at 3", ZONES[0]).dates, (DateHint(date(2026, 11, 22), time(15)),))

    def test_email_intents_and_unrelated_numbers(self):
        hints = self.scan("Book a 30 min call Tuesday 10am for Sam@Example.com", ZONES[0])
        self.assertEqual(hints.email, "sam@example.com")
        self.assertEqual(hints.dates, (DateHint(date(2026, 11, 24), time(10)),))
        self.assertIn("book", hints.intents)
        self.assertIn("list", self.scan("What meetings do I have?", ZONES[0]).intents)
        self.assertIn("cancel", self.scan("Cancel this", ZONES[0]).intents)
        self.assertIn("reschedule", self.scan("Move it an hour later", ZONES[0]).intents)
        self.assertFalse(self.scan("What about 30 minutes?", ZONES[0]).dates)

    def test_english_and_spanish_calendar_spans(self):
        warm_prescan()
        for phrase in ("Book November 26", "Reservar el 26 de noviembre", "¿Puedo reservar el jueves?"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.scan(phrase, ZONES[0]).dates, (DateHint(date(2026, 11, 26)),))
        self.assertEqual(self.scan("jueves por la mañana", ZONES[0]).part_of_day, "morning")
