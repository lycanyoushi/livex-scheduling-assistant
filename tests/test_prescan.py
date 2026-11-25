from datetime import date, datetime, time, timedelta
import unittest
from zoneinfo import ZoneInfo

from backend.prescan import ATTACHED_TIME, BOOKING_REQUEST, DateHint, TimeWindow, _requested_time, prescan, warm_prescan


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

    def test_move_without_a_destination_date_keeps_the_booking_day(self):
        today, tomorrow = date(2026, 11, 23), date(2026, 11, 24)
        expectations = {
            "move my 3pm tomorrow to after 4": (tomorrow, time(16)),
            "move my 3pm tomorrow to later": (tomorrow, None),
            "move my 3pm tomorrow to after 4 today": (today, time(16)),
            "move my 3pm tomorrow to this afternoon": (today, None),
        }
        for message, (destination, after) in expectations.items():
            with self.subTest(message=message):
                hints = self.scan(message, ZONES[0])
                self.assertEqual(hints.existing_meeting, DateHint(tomorrow, time(15)))
                self.assertEqual(hints.dates, (DateHint(destination),))
                self.assertEqual(hints.move_after, after)

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
            "Book me next week, I'm busy on Monday": {0},
            "Book me next week. I'm busy Tuesday.": {1},
            "Find a time next week. I'll be away on Wednesday.": {2},
            "Show openings next week. I'm unavailable Thursday.": {3},
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

    def test_personal_schedule_and_calendar_requests_are_listing_only(self):
        for phrase in (
            "my schedule", "  Show me my schedule  ", "What's my schedule", "What’s my schedule?",
            "What’s on my schedule?", "What is on my schedule?",
            "What's on my calendar?", "What’s on my calendar?", "What's my calendar", "What’s my calendar",
            "What is on my calendar?", "on my calendar",
        ):
            with self.subTest(phrase=phrase):
                hints = self.scan(phrase, ZONES[0])
                self.assertEqual(hints.intents, ("list",))
                self.assertFalse(hints.availability_request)

        dated = self.scan("What's on my calendar tomorrow?", ZONES[0])
        self.assertEqual(dated.intents, ("list",))
        self.assertEqual(dated.dates, (DateHint(date(2026, 11, 24)),))
        self.assertFalse(dated.availability_request)

    def test_scheduling_verb_and_combined_requests_keep_independent_intents(self):
        for phrase in ("schedule a meeting", "Schedule a meeting around my schedule"):
            with self.subTest(phrase=phrase):
                hints = self.scan(phrase, ZONES[0])
                self.assertEqual(hints.intents, ("book",))
                self.assertTrue(hints.availability_request)
        on_calendar = self.scan("Schedule a meeting on my calendar", ZONES[0])
        self.assertEqual(on_calendar.intents, ("book",))
        self.assertTrue(on_calendar.availability_request)

        for phrase in ("Show me my schedule and schedule a meeting", "Book a meeting, then show me my schedule"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.scan(phrase, ZONES[0]).intents, ("book", "list"))
        self.assertEqual(self.scan("Show me my schedule and cancel this", ZONES[0]).intents, ("list", "cancel"))

    def test_booking_phrases_bind_the_full_requested_interval(self):
        requests = (
            ("Can I add to my schedule a meeting with Avery Example on 12 Dec from 1100 to 1300?", 12),
            ("Can I add to my schedule a meeting with Avery Example on 11 Dec from 1100 to 1300?", 11),
            ("Put a meeting with Avery Example on my calendar on 11 Dec 11:00-13:00", 11),
            ("I want to schedule a meeting with Avery Example on Dec 11 from 11 to 1", 11),
            ("Add a meeting from 11 to 1 on Dec 11", 11),
            ("Set up a meeting Dec 11 11:00–13:00", 11),
            ("Pencil in a chat on Dec 11 from 11am to 1pm", 11),
            ("Arrange a call on Dec 11 from 11:00 to 13:00", 11),
            ("Add a meeting to my schedule on 2026-12-11 from 11am to 1pm", 11),
        )
        for zone in ZONES:
            for phrase, day in requests:
                with self.subTest(phrase=phrase, zone=zone):
                    hints = self.scan(phrase, zone)
                    self.assertEqual(hints.intents, ("book",))
                    self.assertTrue(hints.availability_request)
                    self.assertTrue(hints.date_time_complete)
                    self.assertEqual(hints.dates, (DateHint(date(2026, 12, day)),))
                    self.assertEqual(hints.time_window, TimeWindow(time(11), time(13), full_meeting=True))
                    self.assertEqual(hints.time_window.minutes, 120)

    def test_openings_and_existing_guest_edits_do_not_request_a_whole_meeting(self):
        for phrase in ("Anything between 2 and 5 on 11 Dec?", "Show openings Dec 11, 14:00–17:00"):
            with self.subTest(phrase=phrase):
                hints = self.scan(phrase, ZONES[0])
                self.assertTrue(hints.availability_request)
                self.assertEqual(hints.time_window, TimeWindow(time(14), time(17)))
        for phrase in ("Add Avery to my 3pm", "Add Avery to my meeting", "Add a meeting note to my call"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(BOOKING_REQUEST.search(phrase))
                self.assertFalse(self.scan(phrase, ZONES[0]).availability_request)

    def test_english_and_spanish_calendar_spans(self):
        warm_prescan()
        for phrase in ("Book November 26", "Reservar el 26 de noviembre", "¿Puedo reservar el jueves?"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.scan(phrase, ZONES[0]).dates, (DateHint(date(2026, 11, 26)),))
        self.assertEqual(self.scan("jueves por la mañana", ZONES[0]).part_of_day, "morning")

    def test_abbreviated_english_months_with_optional_years_and_times(self):
        examples = {
            "2 Dec": DateHint(date(2026, 12, 2)),
            "Dec 2": DateHint(date(2026, 12, 2)),
            "DEC 2 at 3pm": DateHint(date(2026, 12, 2), time(15)),
            "2 Dec 2027": DateHint(date(2027, 12, 2)),
            "2 Dec, 2027": DateHint(date(2027, 12, 2)),
            "Dec 2 2027": DateHint(date(2027, 12, 2)),
            "Dec 2, 2025": DateHint(date(2025, 12, 2)),
            "Sept 2": DateHint(date(2027, 9, 2)),
        }
        for zone in ZONES:
            for phrase, expected in examples.items():
                with self.subTest(zone=zone, phrase=phrase):
                    self.assertEqual(self.scan(phrase, zone).dates, (expected,))

    def test_early_openings_require_the_entire_time_expression_to_be_read(self):
        complete = (
            "Book 30 minutes on Dec 2 from 1100 to 1300",
            "Find openings between 11:00 and 13:00 on Dec 2",
            "Book a meeting on Thursday afternoon",
            "Find openings next week except Monday",
            "Set up a meeting Dec 2 11:00–13:00",
        )
        unread = (
            "Book a meeting on Dec 2 at 1030",
            "Find openings on Dec 2 from 1100 until 1300",
            "Check next Tuesday or the week after",
            "Book a two hour meeting tomorrow",
            "See what is open Thu afternoon",
        )
        for expected, phrases in ((True, complete), (False, unread)):
            for phrase in phrases:
                with self.subTest(phrase=phrase):
                    self.assertEqual(self.scan(phrase, ZONES[0]).date_time_complete, expected)

    def test_unresolved_scheduling_cues_leave_no_partial_date_or_time_window(self):
        phrases = (
            "Book tomorrow at noon",
            "Book Wednesday at 0930",
            "Book tomorrow at 3.30pm",
            "Book the day after tomorrow at 10am",
            "Show openings in two weeks",
            "Show openings in three days",
            "Book tomorrow at 11am Tokyo time",
            "Book tomorrow at 11:00 Asia/Tokyo",
            "Book tomorrow at 11:00 in Tokyo",
            "Check Friday at 09:00 in new york time",
            "Book tomorrow at 3:60",
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                hints = self.scan(phrase, ZONES[0])
                self.assertTrue(hints.needs_interpretation)
                self.assertEqual(hints.dates, ())
                self.assertIsNone(hints.time_window)
                self.assertFalse(hints.invalid_time_window)

    def test_last_weekday_cannot_predate_or_roll_forward_to_a_new_booking(self):
        hints = self.scan("last Monday at 3", ZONES[0])
        self.assertTrue(hints.needs_interpretation)
        self.assertTrue(hints.past_date_requested)
        self.assertEqual(hints.dates, ())
        for phrase in ("Book the last Monday of November", "Check last Monday or next Friday"):
            with self.subTest(phrase=phrase):
                hints = self.scan(phrase, ZONES[0])
                self.assertTrue(hints.needs_interpretation)
                self.assertFalse(hints.past_date_requested)

    def test_complete_compact_range_and_ordinary_time_request_keep_their_hints(self):
        compact = self.scan("Book 30 minutes on Dec 2 from 1100 to 1300", ZONES[0])
        self.assertFalse(compact.needs_interpretation)
        self.assertEqual(compact.dates, (DateHint(date(2026, 12, 2)),))
        self.assertEqual((compact.time_window.start, compact.time_window.end), (time(11), time(13)))
        ordinary = self.scan("Book time tomorrow at 11am", ZONES[0])
        self.assertFalse(ordinary.needs_interpretation)
        self.assertEqual(ordinary.dates, (DateHint(date(2026, 11, 24), time(11)),))

    def test_timing_and_date_mentions_keep_revision_context_separate(self):
        for phrase, expected in (
            ("Tell me 3 jokes", (False, False)),
            ("Explain the dash — with three examples", (False, False)),
            ("4 instead", (True, False)),
            ("noon", (True, False)),
            ("0930", (True, False)),
            ("3.30 pm", (True, False)),
            ("Tokyo time", (True, False)),
            ("90 minutes", (True, False)),
            ("the day after tomorrow", (True, True)),
            ("in two weeks", (True, True)),
            ("last Mon at 3", (True, True)),
            ("2027/04/03 at 11", (True, True)),
            ("the 4th at noon", (True, True)),
            ("por la mañana", (True, False)),
        ):
            with self.subTest(phrase=phrase):
                hints = self.scan(phrase, ZONES[0])
                self.assertEqual((hints.timing_mentioned, hints.date_mentioned), expected)

    def test_abbreviated_month_exclusions_target_only_the_named_dates(self):
        for phrase in (
            "Book a time next week except 2 Dec or Dec 4, 2026.",
            "Book a time next week. 2 Dec and Dec 4 are out.",
        ):
            with self.subTest(phrase=phrase):
                hints = self.scan(phrase, ZONES[0])
                self.assertFalse(hints.exclusion_unresolved)
                self.assertEqual(hints.excluded_days, {date(2026, 12, 2), date(2026, 12, 4)})
                self.assertEqual(hints.dates, (
                    DateHint(date(2026, 11, 30)), DateHint(date(2026, 12, 1)), DateHint(date(2026, 12, 3)),
                ))
