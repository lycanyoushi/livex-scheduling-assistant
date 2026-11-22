import asyncio
from datetime import datetime, timedelta
import json
import re
from threading import Event
import unittest
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from backend.cal_client import CalClient, CalObservation
from backend.model import ModelCompletion, ModelText
from backend.prefetch import plan_prefetch, warm_slot_bounds
from backend.prescan import prescan
from backend.protocol import ChatRequest
from backend.state import add_booking, new_state, sign_state, verify_state
from backend.tools import booking_entry
from backend.turn import TurnService, observe_cal
from evals.fake_cal import DEFAULT_CLOCK, FakeCal


NOW = DEFAULT_CLOCK
ZONE = "Asia/Singapore"
KEY = "test-only-secret-for-prefetch-observations"
PREFETCH_REF = re.compile(r"^Prefetched slot (S[1-9][0-9]*):", re.MULTILINE)


class ReplyFromContext:
    def __init__(self):
        self.contexts = []
        self.calls = 0

    async def stream(self, **request):
        self.calls += 1
        context = request["input_steps"][-1]["content"][0]["text"]
        self.contexts.append(context)
        refs = PREFETCH_REF.findall(context)[:3]
        text = "I can help with those times."
        if refs:
            text += " " + " ".join(f"[[{ref}]]" for ref in refs)
        yield ModelText(text)
        yield ModelCompletion(f"fixture-{self.calls}", "completed")


def request(text, token=None, zone=ZONE):
    return ChatRequest.model_validate({
        "stateToken": token, "timeZone": zone, "input": {"kind": "text", "text": text},
    })


class PrefetchTests(unittest.IsolatedAsyncioTestCase):
    def service(self, calendar, *, budget=None):
        self.model = ReplyFromContext()
        kwargs = {"prefetch_budget_seconds": budget} if budget is not None else {}
        service = TurnService(model=self.model, calendar=calendar, secret=KEY, clock=lambda: NOW, **kwargs)
        self.addCleanup(service.close_prefetch)
        return service

    async def collect(self, service, text, token=None, zone=ZONE):
        return [event async for event in service.events(request(text, token, zone))]

    async def test_case_two_provides_afternoon_refs_before_the_only_model_call(self):
        service = self.service(FakeCal())
        stream = service.events(request("30 min Thursday afternoon?"))
        first = await anext(stream)
        self.assertEqual(first["type"], "status")
        self.assertIn("Thursday 26 Nov", first["text"])
        self.assertEqual(self.model.calls, 0)
        refs = await anext(stream)
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(refs["type"], "refs")
        local_times = [datetime.fromisoformat(view["startLocalIso"]) for view in refs["slots"].values()]
        self.assertTrue(all(stamp.date().isoformat() == "2026-11-26" and 12 <= stamp.hour < 17 for stamp in local_times))
        self.assertIn("12:00", {stamp.strftime("%H:%M") for stamp in local_times})
        chips = await anext(stream)
        self.assertEqual(chips["type"], "slot_chips")
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(len(chips["refs"]), 4)
        self.assertTrue(set(chips["refs"]) <= refs["slots"].keys())
        self.assertIn("Thu 26 Nov 2026", chips["note"])
        events = [event async for event in stream]
        self.assertIn("Openings already on screen", self.model.contexts[0])
        self.assertEqual(len(PREFETCH_REF.findall(self.model.contexts[0])), len(local_times))
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(events[-1]["metrics"]["toolCalls"], [])
        self.assertEqual(events[-1]["metrics"]["prefetch"]["slots"], len(local_times))
        self.assertFalse(events[-1]["metrics"]["prefetch"]["timedOut"])
        self.assertEqual(events[-1]["metrics"]["unknownRefs"], 0)
        self.assertEqual(set(verify_state(events[-1]["stateToken"], KEY, NOW).slots), set(refs["slots"]))

    async def test_repeated_prefetch_reuses_refs_without_resending_details(self):
        service = self.service(FakeCal())
        first = await self.collect(service, "30 min Thursday afternoon?")
        second = await self.collect(service, "30 min Thursday afternoon?", first[-1]["stateToken"])
        first_chips = next(event for event in first if event["type"] == "slot_chips")
        second_chips = next(event for event in second if event["type"] == "slot_chips")
        self.assertEqual(first_chips["refs"], second_chips["refs"])
        self.assertGreater(len(PREFETCH_REF.findall(self.model.contexts[0])), 0)
        self.assertEqual(PREFETCH_REF.findall(self.model.contexts[1]), [])
        self.assertIn("Openings already on screen", self.model.contexts[1])

    async def test_checked_empty_window_is_distinct_from_missing_availability(self):
        service = self.service(FakeCal(fixture_overrides={"open_local_starts": []}))
        events = await self.collect(service, "Can I grab thirty minutes Thursday afternoon?")
        self.assertFalse(any(event["type"] == "slot_chips" for event in events))
        self.assertIn("Availability checked for this request. No matching openings.", self.model.contexts[0])
        self.assertTrue(events[-1]["metrics"]["prefetch"]["slotsChecked"])

    async def test_warm_and_prefetch_share_the_exact_cache_key(self):
        calendar = FakeCal()
        transport_paths = []

        def transport(wire, _timeout):
            url = urlsplit(wire.full_url)
            transport_paths.append(url.path)
            query = parse_qs(url.query)
            if url.path == "/v2/me":
                data = calendar.me()
            elif url.path == "/v2/event-types":
                data = calendar.event_types(query["username"][0])
            else:
                self.assertEqual(url.path, "/v2/slots")
                data = calendar.slots(
                    int(query["eventTypeId"][0]), query["start"][0], query["end"][0], query["timeZone"][0]
                )
            return 200, json.dumps({"status": "success", "data": data}).encode()

        client = CalClient("synthetic-key", observer=observe_cal, transport=transport)
        event_type = client.event_types(client.me()["username"])[0]
        bounds = warm_slot_bounds(NOW, ZONE)
        client.slots(event_type["id"], *bounds, ZONE)
        service = self.service(client)
        events = await self.collect(service, "30 min Thursday afternoon?")
        self.assertEqual(transport_paths, ["/v2/me", "/v2/event-types", "/v2/slots"])
        self.assertGreater(events[-1]["metrics"]["prefetch"]["slots"], 0)
        self.assertIn(
            ("GET /v2/slots", True),
            {(call["endpoint"], call["cacheHit"]) for call in events[-1]["metrics"]["calCalls"]},
        )

    async def test_deadline_keeps_fast_booking_result_and_discards_late_slots_and_metrics(self):
        started, release = Event(), Event()

        class DelayedCalendar(FakeCal):
            def slots(self, *args, **kwargs):
                started.set()
                if not release.wait(2):
                    raise TimeoutError("The test did not release calendar slots.")
                observe_cal(CalObservation("GET", "/v2/slots", 200, None, 1000))
                return super().slots(*args, **kwargs)

            def list_bookings(self, *, attendee_email=None, status="upcoming", limit=20, cursor=None):
                if not started.wait(1):
                    raise AssertionError("Calendar lookups did not start together.")
                requested_addresses.append(attendee_email)
                observe_cal(CalObservation("GET", "/v2/bookings", 200, None, 1))
                # A bad upstream filter must not expose an unrelated attendee.
                return super().list_bookings(status=status, limit=limit, cursor=cursor)

        requested_addresses = []
        service = self.service(DelayedCalendar(fixture_overrides={"active_bookings": ["sam_intro", "other_intro"]}), budget=0.06)
        try:
            events = await asyncio.wait_for(
                self.collect(service, "What meetings do I have tomorrow for sam@example.com?"), timeout=0.6
            )
            terminal = events[-1]
            timings = terminal["metrics"]["prefetch"]
            self.assertTrue(timings["timedOut"])
            self.assertEqual((timings["slots"], timings["bookings"]), (0, 1))
            self.assertEqual(requested_addresses, ["sam@example.com"])
            ledger = terminal["metrics"]["calCalls"]
            self.assertEqual([call["endpoint"] for call in ledger], ["GET /v2/bookings"])
            serialized = json.dumps(self.model.contexts)
            self.assertNotIn("sam@example.com", serialized)
            self.assertNotIn("taylor@example.com", serialized)
            self.assertNotIn("fixture-other-intro", serialized)
            self.assertNotIn("Appointment", serialized)
            state = verify_state(terminal["stateToken"], KEY, NOW)
            self.assertFalse(state.slots)
            self.assertEqual([record.uid for record in state.bookings.values()], ["fixture-sam-intro"])
        finally:
            release.set()
            service.close_prefetch()
        self.assertEqual([call["endpoint"] for call in ledger], ["GET /v2/bookings"])

    async def test_slow_catalog_does_not_put_prefetch_back_on_the_model_critical_path(self):
        started, release = Event(), Event()

        class DelayedCatalog(FakeCal):
            def me(self):
                started.set()
                if not release.wait(2):
                    raise TimeoutError("The test did not release the calendar catalog.")
                return super().me()

        service = self.service(DelayedCatalog(), budget=0.04)
        try:
            events = await asyncio.wait_for(self.collect(service, "30 min Thursday afternoon?"), timeout=0.4)
            self.assertTrue(started.is_set())
            self.assertEqual(self.model.calls, 1)
            self.assertTrue(events[-1]["metrics"]["prefetch"]["timedOut"])
            self.assertEqual(events[-1]["metrics"]["prefetch"]["slots"], 0)
        finally:
            release.set()
            service.close_prefetch()

    async def test_cancelled_turn_never_starts_model_from_an_unfinished_prefetch(self):
        started, release = Event(), Event()

        class DelayedSlots(FakeCal):
            def slots(self, *args, **kwargs):
                started.set()
                if not release.wait(2):
                    raise TimeoutError("The test did not release calendar slots.")
                return super().slots(*args, **kwargs)

        service = self.service(DelayedSlots())
        received = []

        async def consume():
            async for event in service.events(request("30 min Thursday afternoon?")):
                received.append(event)

        waiting = asyncio.create_task(consume())
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 0.5))
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            self.assertEqual([event["type"] for event in received], ["status"])
            self.assertEqual(self.model.calls, 0)
        finally:
            release.set()
            service.close_prefetch()

    async def test_no_date_spreads_six_refs_over_at_least_three_days(self):
        service = self.service(FakeCal())
        events = await self.collect(service, "Book a meeting")
        refs = next(event["slots"] for event in events if event["type"] == "refs")
        self.assertEqual(len(refs), 6)
        self.assertGreaterEqual(len({view["startLocalIso"][:10] for view in refs.values()}), 3)
        self.assertEqual(events[-1]["metrics"]["prefetch"]["slots"], 6)

    async def test_capped_multi_day_hints_retain_a_slot_for_each_requested_day(self):
        class DenseCalendar(FakeCal):
            def slots(self, *args, **kwargs):
                slots = super().slots(*args, **kwargs)
                early = datetime.fromisoformat("2026-11-23T10:30:00+08:00")
                slots["2026-11-23"] = [
                    {"start": (early + timedelta(minutes=30 * index)).isoformat(),
                     "end": (early + timedelta(minutes=30 * (index + 1))).isoformat()}
                    for index in range(20)
                ]
                return slots

        service = self.service(DenseCalendar())
        events = await self.collect(service, "Book a time this week but not Monday")
        refs = next(event["slots"] for event in events if event["type"] == "refs")
        self.assertEqual(len(refs), 16)
        self.assertEqual({view["startLocalIso"][:10] for view in refs.values()}, {
            "2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27",
        })

    async def test_prefetched_reschedule_slots_keep_their_owned_booking_binding(self):
        class RecordedCalendar(FakeCal):
            def slots(self, *args, **kwargs):
                calendar_reads.append(kwargs.get("booking_uid_to_reschedule"))
                return super().slots(*args, **kwargs)

        calendar_reads = []
        calendar = RecordedCalendar(fixture_overrides={"active_bookings": ["sam_intro", "other_intro"]})
        state = new_state(ZONE, NOW)
        state.user.email = "sam@example.com"
        own = add_booking(state, booking_entry(calendar.booking("fixture-sam-intro")))
        foreign = add_booking(state, booking_entry(calendar.booking("fixture-other-intro")))
        duplicate = add_booking(state, booking_entry(calendar.booking("fixture-sam-intro")))
        state.rescheduling_booking_ref = own
        service = self.service(calendar)
        events = await self.collect(
            service, "Move my meeting to Thursday afternoon", sign_state(state, KEY)
        )
        token_state = verify_state(events[-1]["stateToken"], KEY, NOW)
        self.assertEqual(calendar_reads, ["fixture-sam-intro"])
        self.assertEqual({record.for_booking_ref for record in token_state.slots.values()}, {own})
        self.assertIn(f"for moving {own}", self.model.contexts[0])
        self.assertIn(f"Prefetched upcoming booking {own}", self.model.contexts[0])
        self.assertNotIn(f"booking {duplicate}", self.model.contexts[0])
        self.assertNotIn(f"booking {foreign}", self.model.contexts[0])
        self.assertNotIn("fixture-sam-intro", self.model.contexts[0])

    async def test_current_prefetched_booking_is_visible_even_if_its_ref_is_old(self):
        calendar = FakeCal(fixture_overrides={"active_bookings": ["sam_intro"]})
        state = new_state(ZONE, NOW)
        state.user.email = "sam@example.com"
        intro = booking_entry(calendar.booking("fixture-sam-intro"))
        first = add_booking(state, intro)
        for index in range(12):
            add_booking(state, intro.model_copy(update={"uid": f"old-fixture-{index}", "title": "Stale calendar entry"}))
        service = self.service(calendar)
        events = await self.collect(service, "What meetings do I have?", sign_state(state, KEY))
        self.assertIn(f"Prefetched upcoming booking {first}", self.model.contexts[0])
        self.assertNotIn("Stale calendar entry", self.model.contexts[0])
        refs = next(event["bookings"] for event in events if event["type"] == "refs")
        self.assertEqual(list(refs), [first])

    def test_warm_bounds_preserve_local_midnights_across_dst(self):
        zone = ZoneInfo("America/New_York")
        local = datetime.fromisoformat("2026-10-28T10:00:00-04:00")
        begin, end = warm_slot_bounds(local, zone.key)
        self.assertEqual(begin.astimezone(zone).isoformat(), "2026-10-28T00:00:00-04:00")
        self.assertEqual(end.astimezone(zone).isoformat(), "2026-11-11T00:00:00-05:00")
        self.assertEqual(end - begin, timedelta(days=14, hours=1))

    def test_unknown_or_foreign_attendees_do_not_resolve_as_prefetch_bindings(self):
        calendar = FakeCal(fixture_overrides={"active_bookings": ["other_intro", "sam_intro"]})
        state = new_state(ZONE, NOW)
        hints = prescan("Move my meeting to Thursday afternoon", now=NOW, time_zone=ZONE)
        add_booking(state, booking_entry(calendar.booking("fixture-other-intro")))
        no_email = plan_prefetch(state, NOW, hints)
        self.assertIsNone(no_email.attendee_email)
        self.assertIsNone(no_email.reschedule)
        state.user.email = "sam@example.com"
        with_email = plan_prefetch(state, NOW, hints)
        self.assertEqual(with_email.attendee_email, "sam@example.com")
        self.assertIsNone(with_email.reschedule)
        owned = add_booking(state, booking_entry(calendar.booking("fixture-sam-intro")))
        self.assertEqual(plan_prefetch(state, NOW, hints).reschedule.ref, owned)
        self.assertIsNone(plan_prefetch(state, NOW, hints, text="Move B1 to Thursday afternoon").reschedule)
        self.assertIsNone(plan_prefetch(state, NOW, hints, text="Move B999 to Thursday afternoon").reschedule)


if __name__ == "__main__":
    unittest.main()
