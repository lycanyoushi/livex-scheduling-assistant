from contextvars import ContextVar
from datetime import datetime, timedelta
from threading import Event
import unittest
from zoneinfo import ZoneInfo

from backend.cal_client import CalError
from backend.prefetch import Prefetcher, plan_prefetch
from backend.prescan import prescan
from backend.state import new_state
from backend.time import parse_instant
from backend.tools import CalendarTools
from evals.fake_cal import FakeCal


NOW = datetime.fromisoformat("2026-11-22T21:00:00+08:00")
ZONE = "Asia/Singapore"
OPENINGS = [
    "2026-11-23T10:00", "2026-11-23T11:00",
    "2026-11-24T10:00", "2026-11-24T14:00",
    "2026-11-25T09:30", "2026-11-25T11:00",
    "2026-11-26T09:00", "2026-11-26T11:00",
    "2026-11-27T09:00", "2026-11-27T10:30",
]


def build_plan(phrase, state=None):
    hints = prescan(phrase, now=NOW, time_zone=ZONE)
    return plan_prefetch(state or new_state(ZONE, NOW), NOW, hints, text=phrase)


def starts(result):
    return [parse_instant(slot.start_utc).astimezone(ZoneInfo(ZONE)) for slot in result.slots]


class AvailabilityHintTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, plan):
        calendar = FakeCal(clock=lambda: NOW, fixture_overrides={"open_local_starts": OPENINGS})
        prefetcher = Prefetcher(metrics_context=ContextVar("metrics", default=None))
        self.addCleanup(prefetcher.close)
        return await prefetcher.fetch(CalendarTools(calendar, new_state(ZONE, NOW), NOW), plan)

    async def test_thirty_minutes_thursday_is_a_checked_empty_afternoon(self):
        plan = build_plan("Can I grab thirty minutes Thursday afternoon?")
        self.assertTrue(plan.show_openings)
        self.assertEqual(plan.part_of_day, "afternoon")
        self.assertEqual([day.isoformat() for day in plan.days], ["2026-11-26"])
        result = await self.fetch(plan)
        self.assertTrue(result.slots_checked)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.slots, [])

    async def test_friday_morning_follow_up_keeps_both_actual_options(self):
        plan = build_plan("What about Friday morning?")
        self.assertTrue(plan.show_openings)
        self.assertEqual(plan.part_of_day, "morning")
        result = await self.fetch(plan)
        self.assertTrue(result.slots_checked)
        self.assertEqual(
            [start.isoformat(timespec="minutes") for start in starts(result)],
            ["2026-11-27T09:00+08:00", "2026-11-27T10:30+08:00"],
        )

    def test_bare_local_date_and_clock_requests_exact_availability(self):
        zone = "America/New_York"
        now = datetime.fromisoformat("2026-11-22T21:00:00-05:00")
        phrase = "tomorrow 9am"
        hints = prescan(phrase, now=now, time_zone=zone)
        plan = plan_prefetch(new_state(zone, now), now, hints, text=phrase)
        self.assertTrue(plan.show_openings)
        self.assertEqual(plan.times[0].day.isoformat(), "2026-11-23")
        self.assertEqual(plan.times[0].at.isoformat(), "09:00:00")

    async def test_next_week_times_are_recognized_and_available_for_display(self):
        plan = build_plan("Can you show me some times next week?")
        self.assertTrue(plan.show_openings)
        self.assertFalse(plan.spread_slots)
        result = await self.fetch(plan)
        self.assertTrue(result.slots_checked)
        self.assertEqual(
            {stamp.date().isoformat() for stamp in starts(result)},
            {"2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27"},
        )

    async def test_morning_option_without_a_date_searches_the_next_seven_local_days(self):
        plan = build_plan("Does any day have a morning option?")
        self.assertTrue(plan.show_openings)
        self.assertTrue(plan.spread_slots)
        self.assertEqual(plan.days, tuple(NOW.date() + timedelta(days=offset) for offset in range(7)))
        result = await self.fetch(plan)
        self.assertTrue(result.slots_checked)
        actual = starts(result)
        self.assertEqual(len(actual), 6)
        self.assertGreaterEqual(len({stamp.date() for stamp in actual}), 3)
        self.assertTrue(all(8 <= stamp.hour < 12 for stamp in actual))

    async def test_relative_week_weekday_exclusions_filter_automatic_offers(self):
        for phrase, excluded in (
            ("Can you show me some times next week, but not Monday?", "2026-11-23"),
            ("Can you show me some times next week except on Friday?", "2026-11-27"),
        ):
            with self.subTest(phrase=phrase):
                plan = build_plan(phrase)
                self.assertTrue(plan.show_openings)
                self.assertEqual({day.isoformat() for day in plan.excluded_days}, {excluded})
                result = await self.fetch(plan)
                self.assertTrue(result.slots_checked)
                self.assertEqual(len(starts(result)), 4)
                self.assertNotIn(excluded, {stamp.date().isoformat() for stamp in starts(result)})

    async def test_unsupported_exclusions_keep_model_context_without_automatic_offer(self):
        for phrase in (
            "Can you show me some times next week, but not November 23?",
            "Can you show me some times next week except on Friday afternoon?",
        ):
            with self.subTest(phrase=phrase):
                plan = build_plan(phrase)
                self.assertFalse(plan.show_openings)
                self.assertGreater(len(plan.days), 1)
                result = await self.fetch(plan)
                self.assertTrue(result.slots_checked)
                self.assertTrue(result.slots)

    def test_existing_booking_and_explanation_requests_are_not_automatic_offers(self):
        for phrase in (
            "Why is Friday morning not available?",
            "How does booking work for Friday morning?",
            "Show my bookings tomorrow",
            "Cancel my Friday morning appointment",
            "Move my meeting to Friday morning",
            "Good morning",
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(build_plan(phrase).show_openings)
        state = new_state(ZONE, NOW)
        state.rescheduling_booking_ref = "B1"
        self.assertFalse(build_plan("What about Friday morning?", state).show_openings)
        constrained = build_plan("sometime next week but not Monday", state)
        self.assertFalse(constrained.show_openings)
        self.assertFalse(constrained.excluded_days)

    async def test_failed_or_late_reads_do_not_make_an_empty_scope_authoritative(self):
        plan = build_plan("What about Friday morning?")
        state = new_state(ZONE, NOW)

        class FailedCalendar(FakeCal):
            def slots(self, *args, **kwargs):
                raise CalError("upstream", "Synthetic lookup failure.", status=503)

        broken = Prefetcher(metrics_context=ContextVar("metrics", default=None))
        self.addCleanup(broken.close)
        failed = await broken.fetch(CalendarTools(FailedCalendar(), state, NOW), plan)
        self.assertFalse(failed.slots_checked)
        missing = await broken.fetch(CalendarTools(FailedCalendar(), state, NOW), build_plan("Thank you"))
        self.assertFalse(missing.slots_checked)

        release = Event()

        class LateCalendar(FakeCal):
            def slots(self, *args, **kwargs):
                release.wait(1)
                return super().slots(*args, **kwargs)

        delayed = Prefetcher(budget_seconds=0.02, metrics_context=ContextVar("metrics", default=None))
        try:
            late = await delayed.fetch(CalendarTools(LateCalendar(), state, NOW), plan)
            self.assertTrue(late.timed_out)
            self.assertFalse(late.slots_checked)
        finally:
            release.set()
            delayed.close()


if __name__ == "__main__":
    unittest.main()
