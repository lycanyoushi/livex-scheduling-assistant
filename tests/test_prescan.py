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

    def test_relative_week_resolves_weekday_exclusions_inside_the_selected_week(self):
        hints = self.scan("sometime next week but not Monday", ZONES[0])
        self.assertTrue(hints.availability_request)
        self.assertEqual(hints.excluded_days, {date(2026, 11, 30)})
        self.assertEqual(hints.dates, tuple(DateHint(date(2026, 12, day)) for day in range(1, 5)))

        hints = self.scan("Show me openings this week in the afternoon except Monday, Wednesday, and Friday!", ZONES[0])
        self.assertTrue(hints.availability_request)
        self.assertEqual(hints.part_of_day, "afternoon")
        self.assertEqual(hints.excluded_days, {date(2026, 11, day) for day in (23, 25, 27)})
        self.assertEqual(hints.dates, tuple(DateHint(date(2026, 11, day)) for day in (24, 26)))

    def test_recurring_exclusions_and_positive_dates_in_both_zones(self):
        examples = {
            "Could we find a slot next week? Mondays and Wednesdays are out for me.": {0, 2},
            "I'd like a slot next week, anything but Tuesday or Friday.": {1, 4},
            "Can we meet next week? I can’t do Tuesdays or Thursdays.": {1, 3},
            "Book next week, no Mondays.": {0},
            "Show some times next week excluding Wednesday, Thursday, and Friday.": {2, 3, 4},
        }
        start = date(2026, 11, 30)
        for zone in ZONES:
            for phrase, excluded in examples.items():
                with self.subTest(phrase=phrase, zone=zone):
                    hints = self.scan(phrase, zone)
                    self.assertFalse(hints.exclusion_unresolved)
                    self.assertTrue(hints.availability_request)
                    self.assertEqual(hints.excluded_weekdays, excluded)
                    self.assertEqual(hints.dates, tuple(
                        DateHint(start + timedelta(days=day)) for day in range(5) if day not in excluded
                    ))

    def test_explicit_date_exclusions_are_limited_to_the_named_date(self):
        hints = self.scan("Book a time next week except Monday and December 2", ZONES[0])
        self.assertTrue(hints.availability_request)
        self.assertEqual(hints.excluded_weekdays, {0})
        self.assertTrue(hints.excludes(date(2026, 12, 2)))
        self.assertFalse(hints.excludes(date(2026, 12, 9)))
        self.assertEqual(hints.dates, tuple(DateHint(date(2026, 12, day)) for day in (1, 3, 4)))

        for phrase in ("Book next week except November 30", "Book next week except next Monday"):
            with self.subTest(phrase=phrase):
                hints = self.scan(phrase, ZONES[0])
                self.assertEqual(hints.excluded_days, {date(2026, 11, 30)})
                self.assertFalse(hints.excludes(date(2026, 11, 23)))

    def test_unresolved_constraints_block_early_availability_without_excluding_a_full_day(self):
        for phrase, weekdays in (
            ("Book a time next week except Monday afternoon", set()),
            ("Book a time next week except Monday and Tuesday mornings", set()),
            ("Book a time next week except Monday and the end of December", {0}),
            ("Show times next week but not before noon", set()),
            ("Find a slot next week except the first week of December", set()),
        ):
            with self.subTest(phrase=phrase):
                hints = self.scan(phrase, ZONES[0])
                self.assertTrue(hints.exclusion_unresolved)
                self.assertFalse(hints.availability_request)
                self.assertEqual(hints.excluded_weekdays, weekdays)
                self.assertEqual(hints.part_of_day, "any")
                self.assertEqual(hints.dates, ())

    def test_excluded_phrases_keep_explicit_positive_dates_and_intents(self):
        hints = self.scan("Book a time Thursday but not Monday", ZONES[0])
        self.assertTrue(hints.availability_request)
        self.assertEqual(hints.dates, (DateHint(date(2026, 11, 26)),))
        hints = self.scan("Book next week, except Monday, Wednesday is good", ZONES[0])
        self.assertEqual(hints.excluded_weekdays, {0})
        for phrase in (
            "Move my meeting to next week but not Monday",
            "Write a poem about how to book next week but not Monday",
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(self.scan(phrase, ZONES[0]).availability_request)

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
