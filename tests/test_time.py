"""Coverage for scheduling times across zones and DST transitions."""

from datetime import UTC, date, datetime, time, timedelta, timezone
import unittest
from zoneinfo import ZoneInfoNotFoundError

from backend.time import local_bounds, local_datetime, parse_instant, slot_labels, utc_now


class SchedulingTimeTests(unittest.TestCase):
    def test_utc_clock_and_offset_normalization(self):
        instant = utc_now()
        self.assertIs(instant.tzinfo, UTC)
        self.assertEqual(instant.utcoffset(), timedelta(0))
        expected = datetime(2026, 11, 26, 1, tzinfo=UTC)
        self.assertEqual(parse_instant("2026-11-26T09:00:00+08:00"), expected)
        self.assertIs(parse_instant("2026-11-26T01:00:00Z").tzinfo, UTC)
        self.assertEqual(
            parse_instant(datetime(2026, 11, 25, 20, tzinfo=timezone(timedelta(hours=-5)))),
            expected,
        )
        for naive in ("2026-11-26T09:00:00", datetime(2026, 11, 26, 9)):
            with self.subTest(naive=naive), self.assertRaises(ValueError):
                parse_instant(naive)

    def test_normal_wall_times_have_the_users_utc_offset(self):
        day = date(2026, 11, 26)
        for zone, offset, expected_utc_hour in (
            ("Asia/Singapore", 8, 1),
            ("America/New_York", -5, 14),
        ):
            with self.subTest(zone=zone):
                local = local_datetime(day, time(9), zone)
                self.assertEqual(local.utcoffset(), timedelta(hours=offset))
                self.assertEqual(parse_instant(local), datetime(2026, 11, 26, expected_utc_hour, tzinfo=UTC))

    def test_rejects_nonexistent_and_ambiguous_wall_times(self):
        zone = "America/New_York"
        with self.assertRaisesRegex(ValueError, "does not exist"):
            local_datetime(date(2026, 3, 8), time(2, 30), zone)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            local_datetime(date(2026, 11, 1), time(1, 30), zone)
        with self.assertRaises(ZoneInfoNotFoundError):
            local_datetime(date(2026, 3, 8), time(9), "Invalid/Zone")

    def test_explicit_offset_selects_either_repeated_wall_time(self):
        day = date(2026, 11, 1)
        for offset, utc_hour, fold in ((-4, 5, 0), (-5, 6, 1)):
            with self.subTest(offset=offset):
                specified = time(1, 30, tzinfo=timezone(timedelta(hours=offset)))
                local = local_datetime(day, specified, "America/New_York")
                self.assertEqual(local.fold, fold)
                self.assertEqual(parse_instant(local), datetime(2026, 11, 1, utc_hour, 30, tzinfo=UTC))
        with self.assertRaisesRegex(ValueError, "offset does not match"):
            local_datetime(day, time(1, 30, tzinfo=UTC), "America/New_York")

    def test_inclusive_date_ranges_have_half_open_utc_bounds(self):
        cases = (
            ("Asia/Singapore", date(2026, 3, 7), date(2026, 3, 8), "2026-03-06T16:00:00Z", "2026-03-08T16:00:00Z"),
            ("America/New_York", date(2026, 3, 8), date(2026, 3, 8), "2026-03-08T05:00:00Z", "2026-03-09T04:00:00Z"),
            ("America/New_York", date(2026, 11, 1), date(2026, 11, 1), "2026-11-01T04:00:00Z", "2026-11-02T05:00:00Z"),
        )
        for zone, first, last, start, end in cases:
            with self.subTest(zone=zone, first=first):
                self.assertEqual(local_bounds(first, last, zone), (parse_instant(start), parse_instant(end)))
        with self.assertRaises(ValueError):
            local_bounds(date(2026, 11, 26), date(2026, 11, 25), "Asia/Singapore")

    def test_date_bounds_use_the_first_real_instant_when_midnight_is_missing(self):
        day = date(2018, 11, 4)
        self.assertEqual(
            local_bounds(day, day, "America/Sao_Paulo"),
            (parse_instant("2018-11-04T03:00:00Z"), parse_instant("2018-11-05T02:00:00Z")),
        )
        skipped_day = date(2011, 12, 30)
        next_day_start = parse_instant("2011-12-30T10:00:00Z")
        self.assertEqual(local_bounds(skipped_day, skipped_day, "Pacific/Apia"), (next_day_start, next_day_start))

    def test_slot_label_shows_the_local_date_and_time_range(self):
        start, end = "2026-11-26T01:00:00Z", "2026-11-26T01:30:00Z"
        self.assertEqual(slot_labels(start, end, "Asia/Singapore"), ("Thu 26 Nov 2026", "09:00–09:30"))
        self.assertEqual(slot_labels(start, end, "America/New_York"), ("Wed 25 Nov 2026", "20:00–20:30"))
        self.assertEqual(
            slot_labels("2026-12-31T15:45:00Z", "2026-12-31T16:15:00Z", "Asia/Singapore"),
            ("Thu 31 Dec 2026 – Fri 1 Jan 2027", "23:45–00:15"),
        )

    def test_actual_slots_in_the_repeated_hour_show_distinct_offsets(self):
        zone = "America/New_York"
        for hour, offset in ((5, "-04:00"), (6, "-05:00")):
            with self.subTest(hour=hour):
                start = datetime(2026, 11, 1, hour, 30, tzinfo=UTC)
                end = start + timedelta(minutes=15)
                self.assertEqual(slot_labels(start, end, zone), ("Sun 1 Nov 2026", f"01:30 UTC{offset}–01:45 UTC{offset}"))

        self.assertEqual(
            slot_labels("2026-11-01T05:45:00Z", "2026-11-01T06:15:00Z", zone),
            ("Sun 1 Nov 2026", "01:45 UTC-04:00–01:15 UTC-05:00"),
        )
        with self.assertRaises(ValueError):
            slot_labels("2026-11-01T06:15:00Z", "2026-11-01T05:45:00Z", zone)
