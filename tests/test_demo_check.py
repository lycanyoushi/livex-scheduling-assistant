from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from zoneinfo import ZoneInfo

from google.genai import errors as genai_errors

from backend.cal_client import CalClient
from backend.demo_calendar import DemoCalendar
from backend.server import create_app, load_service
from backend.time import parse_instant
from scripts.check import main


ZONE = "America/Los_Angeles"
NOW = datetime.fromisoformat("2027-05-10T10:01:00-07:00")
SETTINGS = {"GEMINI_API_KEY": "gemini-test-value", "STATE_SIGNING_SECRET": "test-signing-key-for-demo-mode-0000"}


@contextmanager
def configured(**overrides):
    with patch.dict(os.environ, overrides, clear=True), patch("backend.server.ENV_FILE") as env_file:
        env_file.is_file.return_value = False
        yield


class DemoCalendarTests(unittest.TestCase):
    def test_opens_future_weekdays_from_current_clock_and_keeps_bookings_in_memory(self):
        now = NOW
        calendar = DemoCalendar(clock=lambda: now)
        event = calendar.event_types(calendar.me()["username"])[0]
        self.assertNotIn("bookingUrl", event)
        self.assertEqual(calendar.me()["timeZone"], ZONE)

        def read(**kwargs):
            return calendar.slots(event["id"], now, now + timedelta(days=7), ZONE, **kwargs)

        slots = read()
        self.assertEqual(set(slots), {"2027-05-10", "2027-05-11", "2027-05-12", "2027-05-13", "2027-05-14", "2027-05-17"})
        first, second = slots["2027-05-10"][:2]
        self.assertEqual(datetime.fromisoformat(first["start"]).hour, 10)
        self.assertEqual(datetime.fromisoformat(first["start"]).minute, 30)
        self.assertGreater(parse_instant(first["start"]), now)
        booking = calendar.create_booking(start=first["start"], event_type_id=event["id"], name="Casey",
                                          email="casey@example.com", time_zone=ZONE)
        self.assertNotIn(first, read()["2027-05-10"])
        self.assertIn(first, read(booking_uid_to_reschedule=booking["uid"])["2027-05-10"])
        moved = calendar.reschedule_booking(booking["uid"], second["start"])
        self.assertNotEqual(booking["uid"], moved["uid"])
        self.assertIn(first, read()["2027-05-10"])
        self.assertNotIn(second, read()["2027-05-10"])
        calendar.cancel_booking(moved["uid"])
        self.assertIn(second, read()["2027-05-10"])

        now += timedelta(days=7)
        self.assertNotIn("2027-05-10", read())
        self.assertTrue(all(parse_instant(slot["start"]) > now for rows in read().values() for slot in rows))
        restarted = DemoCalendar(clock=lambda: now).list_bookings(status=None)["data"]
        self.assertEqual(len(restarted), 5)
        self.assertTrue(all(row["uid"].startswith("demo-seed-") for row in restarted))
        self.assertEqual(parse_instant(restarted[0]["start"]).astimezone(ZoneInfo(ZONE)).date(), now.date())

    def test_demo_service_warms_and_books_through_signed_ui_without_real_calendar(self):
        with configured(**SETTINGS, DEMO_MODE="fake", CAL_EVENT_TYPE_SLUGS="real-only", CAL_DEFAULT_EVENT_TYPE_SLUG="real-only"), \
                patch("backend.server.CalClient", side_effect=AssertionError("Unexpected real calendar")), \
                patch("backend.server.DemoCalendar", wraps=DemoCalendar) as demo:
            service = load_service()
        self.addCleanup(service.close_prefetch)
        self.assertIsInstance(service.calendar, DemoCalendar)
        self.assertEqual(demo.call_count, 1)
        service.model = Mock()
        client = create_app(service).test_client()

        response = client.get("/api/warm", query_string={"timeZone": ZONE})
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json["demoMode"], True)
        self.assertIsNone(response.json["bookingUrl"])
        self.assertEqual(response.json["hostName"], "Demo Calendar")

        def send(payload, prior=None):
            response = client.post("/api/chat", json={"stateToken": prior, "timeZone": ZONE,
                                                      "input": payload}, buffered=True)
            self.assertEqual(response.status_code, 200)
            events = [json.loads(line) for line in response.data.splitlines()]
            self.assertEqual(events[-1]["metrics"]["modelCalls"], [])
            return events

        today = service.clock().astimezone(ZoneInfo(ZONE)).date().isoformat()
        offered = send({"kind": "ui", "action": {"type": "more_slots", "fromDate": today}})
        slot = next(event["refs"][0] for event in offered if event["type"] == "slot_chips")
        selected = send({"kind": "ui", "action": {"type": "pick_slot", "slotRef": slot}}, offered[-1]["stateToken"])
        self.assertFalse(any(event["type"] == "proposal" for event in selected))
        collected = send({"kind": "text", "text": "Casey Reader, casey@example.com"}, selected[-1]["stateToken"])
        proposal = next(event["proposal"]["id"] for event in collected if event["type"] == "proposal")
        result = send({"kind": "ui", "action": {"type": "confirm_proposal", "proposalId": proposal}},
                      collected[-1]["stateToken"])
        self.assertEqual(next(event["kind"] for event in result if event["type"] == "booking_result"), "booked")
        self.assertEqual(len(service.calendar.list_bookings(attendee_email="casey@example.com")["data"]), 1)
        self.assertEqual(service.model.mock_calls, [])


class EnvironmentCheckTests(unittest.TestCase):
    def setUp(self):
        self.sdk = MagicMock()
        self.sdk.__aenter__.return_value = self.sdk
        self.sdk.models.get = AsyncMock()
        self.sdk.interactions.create = AsyncMock()
        self.client = self.enterContext(patch("backend.gemini.genai.Client", return_value=SimpleNamespace(aio=self.sdk)))
        self.cal_transport = Mock(return_value=(200, b'{"status":"success","data":{"username":"demo-test"}}'))
        self.cal_client = self.enterContext(patch("backend.server.CalClient", side_effect=lambda *args, **kwargs:
                                                 CalClient(*args, **kwargs, transport=self.cal_transport)))

    def run_check(self, **settings):
        output = StringIO()
        with configured(**settings), redirect_stdout(output):
            result = main()
        return result, output.getvalue()

    def test_real_mode_reads_profile_and_primary_model_metadata_once(self):
        result, message = self.run_check(**SETTINGS, CAL_API_KEY="cal-test-value", GEMINI_MODEL="chosen-model")
        self.assertEqual(result, 0)
        self.assertIn("Cal.com authenticated", message)
        self.assertIn("Gemini returned metadata", message)
        self.cal_transport.assert_called_once()
        request, _ = self.cal_transport.call_args.args
        self.assertEqual(request.get_method(), "GET")
        self.assertTrue(request.full_url.endswith("/v2/me"))
        self.assertEqual(request.get_header("Authorization"), "Bearer cal-test-value")
        self.sdk.models.get.assert_awaited_once_with(model="chosen-model")
        self.sdk.interactions.create.assert_not_called()
        self.assertEqual(self.client.call_args.kwargs["http_options"].retry_options.attempts, 1)

    def test_reports_each_rejected_provider_without_retries_or_provider_content(self):
        self.cal_transport.return_value = (503, b'{"secret":"cal-test-value"}')
        self.sdk.models.get.side_effect = genai_errors.ClientError(401, {"message": "gemini-test-value"})
        result, message = self.run_check(**SETTINGS, CAL_API_KEY="cal-test-value")
        self.assertEqual(result, 1)
        self.assertIn("Cal.com could not complete the check (HTTP 503)", message)
        self.assertIn("Gemini rejected the check (HTTP 401)", message)
        self.assertNotIn("cal-test-value", message)
        self.assertNotIn("gemini-test-value", message)
        self.cal_transport.assert_called_once()
        self.sdk.models.get.assert_awaited_once()

    def test_demo_checks_gemini_and_skips_cal(self):
        result, message = self.run_check(**SETTINGS, DEMO_MODE="fake")
        self.assertEqual(result, 0)
        self.assertIn("Cal.com check was skipped", message)
        self.sdk.models.get.assert_awaited_once()
        self.cal_client.assert_not_called()
        self.cal_transport.assert_not_called()

    def test_missing_and_invalid_settings_exit_before_provider_activity(self):
        for settings, expected in (
            ({"DEMO_MODE": "fake"}, "GEMINI_API_KEY, STATE_SIGNING_SECRET"),
            ({**SETTINGS}, "CAL_API_KEY"),
            ({**SETTINGS, "DEMO_MODE": "wrong"}, "DEMO_MODE must be empty or fake"),
            ({**SETTINGS, "DEMO_MODE": "fake", "STATE_SIGNING_SECRET": "short"}, "must contain at least 32 bytes"),
        ):
            with self.subTest(expected=expected):
                result, message = self.run_check(**settings)
                self.assertEqual(result, 1)
                self.assertIn(expected, message)
        self.client.assert_not_called()
        self.cal_client.assert_not_called()
