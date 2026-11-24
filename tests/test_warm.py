from datetime import datetime, timezone
import json
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from backend.cal_client import CalClient
from backend.prefetch import warm_slot_bounds
from backend.server import create_app
from backend.turn import TurnService


NOW = datetime(2026, 11, 23, 1, tzinfo=timezone.utc)
ZONE = "America/Los_Angeles"
BOOKING_URL = "https://cal.com/test-host/test-meeting"
PUBLIC_EVENT = {"id": 12, "slug": "test-meeting", "title": "Test meeting", "lengthInMinutes": 30}
SLOTS = {"2026-11-26": [{"start": "2026-11-26T18:30:00Z", "end": "2026-11-26T19:00:00Z"}]}


class WarmRouteTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            "/v2/me": {
                "username": "test-host", "name": " Test\n Host ", "email": "test-host@example.com",
                "avatarUrl": "https://example.com/test-avatar.png",
            },
            "/v2/event-types": [
                {**PUBLIC_EVENT, "id": 10, "slug": "alternate-meeting"},
                {**PUBLIC_EVENT, "bookingUrl": BOOKING_URL, "bookingFields": [{"slug": "attendee"}]},
                {**PUBLIC_EVENT, "id": 13, "slug": "hidden-meeting", "hidden": True},
                {**PUBLIC_EVENT, "id": 14, "slug": "disallowed-meeting"},
            ],
            "/v2/slots": SLOTS,
        }
        self.model = Mock()
        self.transport = Mock(side_effect=self.response)
        self.calendar = CalClient("test-cal-key", transport=self.transport)
        service = TurnService(
            model=self.model, calendar=self.calendar, secret="test-warm-signing-key-1234567890abcdef",
            clock=lambda: NOW, allowed_slugs=("alternate-meeting", "test-meeting", "hidden-meeting"),
            default_slug="test-meeting",
        )
        self.client = create_app(service).test_client()

    def response(self, request, _timeout):
        data = self.data[urlsplit(request.full_url).path]
        return 200, json.dumps({"status": "success", "data": data}).encode("utf-8")

    def test_returns_public_metadata_and_populates_shared_slot_cache(self):
        result = self.client.get("/api/warm", query_string={"timeZone": ZONE})

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["Cache-Control"], "no-store")
        self.assertEqual(result.json, {
            "hostName": "Test Host",
            "avatarUrl": "https://example.com/test-avatar.png",
            "eventTypes": [{**PUBLIC_EVENT, "id": 10, "slug": "alternate-meeting"}, PUBLIC_EVENT],
            "bookingUrl": BOOKING_URL,
        })
        calls = [urlsplit(call.args[0].full_url) for call in self.transport.call_args_list]
        self.assertEqual([call.path for call in calls], ["/v2/me", "/v2/event-types", "/v2/slots"])
        self.assertEqual(parse_qs(calls[-1].query), {
            "eventTypeId": ["12"], "timeZone": [ZONE], "format": ["range"],
            "start": ["2026-11-22T08:00:00Z"], "end": ["2026-12-06T08:00:00Z"],
        })
        begin, end = warm_slot_bounds(NOW, ZONE)
        self.assertEqual(self.calendar.slots(12, begin, end, ZONE), SLOTS)
        self.assertEqual(self.transport.call_count, 3)
        self.assertEqual(self.model.mock_calls, [])

    def test_rejects_invalid_iana_names_before_using_calendar(self):
        for zone in ("", "Unknown/Zone", "A" * 129):
            with self.subTest(zone=zone):
                response = self.client.get("/api/warm", query_string={"timeZone": zone})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json["code"], "invalid_request")
        self.transport.assert_not_called()
        self.assertEqual(self.model.mock_calls, [])

    def test_defaults_to_utc_and_excludes_unsafe_links(self):
        self.data["/v2/me"]["avatarUrl"] = "javascript:alert(1)"
        self.data["/v2/event-types"][1]["bookingUrl"] = "https://attendee@example.com/meeting"
        result = self.client.get("/api/warm")

        self.assertEqual(result.status_code, 200)
        self.assertIsNone(result.json["avatarUrl"])
        self.assertIsNone(result.json["bookingUrl"])
        query = parse_qs(urlsplit(self.transport.call_args.args[0].full_url).query)
        self.assertEqual(query["timeZone"], ["UTC"])
        self.assertEqual(query["start"], ["2026-11-23T00:00:00Z"])
        self.assertEqual(query["end"], ["2026-12-07T00:00:00Z"])

    def test_upstream_warm_failure_returns_generic_error_without_model(self):
        self.transport.side_effect = None
        self.transport.return_value = (503, b'{"internal":"provider-only detail"}')

        result = self.client.get("/api/warm")

        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json, {"code": "cal_down", "error": "The calendar could not be reached."})
        self.assertEqual(self.model.mock_calls, [])

    def test_slot_warm_failure_preserves_public_booking_url(self):
        def fail_slots(wire, timeout):
            if urlsplit(wire.full_url).path == "/v2/slots":
                return 503, b"{}"
            return self.response(wire, timeout)

        self.transport.side_effect = fail_slots
        result = self.client.get("/api/warm")

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["bookingUrl"], BOOKING_URL)
        self.assertEqual(self.model.mock_calls, [])
