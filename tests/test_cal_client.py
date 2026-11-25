from collections import deque
from datetime import date, datetime
import json
import unittest
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from backend.cal_client import CalClient, CalError


DAY = "2030-11-25"
START = f"{DAY}T18:00:00Z"
END = f"{DAY}T18:30:00Z"
PROFILE = {"username": "test-host", "id": 123, "avatarUrl": None}
EVENT_TYPE = {
    "id": 12,
    "slug": "test-meeting",
    "title": "Test meeting",
    "lengthInMinutes": 30,
    "bookingUrl": "https://cal.com/test-host/test-meeting",
}
SLOTS = {DAY: [{"start": START, "end": END}]}
BOOKING = {
    "uid": "example-booking",
    "title": "Test meeting",
    "eventTypeId": 12,
    "start": START,
    "end": END,
    "status": "accepted",
    "attendees": [{"name": "Test Attendee", "email": "attendee@example.com"}],
}


def success(data, http_status=200, **extra):
    return http_status, {"status": "success", "data": data, **extra}


class ScriptedTransport:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response = response()
        status, body = response
        return status, body if isinstance(body, bytes) else json.dumps(body).encode()


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


class CalClientTests(unittest.TestCase):
    def make_client(self, *responses):
        self.transport = ScriptedTransport(*responses)
        self.clock = Clock()
        self.observations = []
        return CalClient(
            "test-key", transport=self.transport, clock=self.clock, observer=self.observations.append
        )

    def query_for(self, index):
        request, _ = self.transport.calls[index]
        return parse_qs(urlsplit(request.full_url).query)

    def body_for(self, index):
        request, _ = self.transport.calls[index]
        return json.loads(request.data)

    def test_request_paths_versions_and_write_payloads(self):
        page = {"nextCursor": "example+cursor=", "hasMore": True}
        client = self.make_client(
            success(PROFILE),
            success([EVENT_TYPE, {**EVENT_TYPE, "hidden": True}]),
            success(SLOTS),
            success([BOOKING], pagination=page),
            success(BOOKING),
            success(BOOKING, 201),
            success({**BOOKING, "status": "cancelled"}),
            success({**BOOKING, "uid": "replacement"}),
        )

        self.assertEqual(client.me(), PROFILE)
        self.assertEqual(client.event_types("test-host"), [EVENT_TYPE])
        self.assertEqual(
            client.slots(
                12,
                date.fromisoformat(DAY),
                DAY,
                "America/Los_Angeles",
                booking_uid_to_reschedule="example-booking",
            ),
            SLOTS,
        )
        self.assertEqual(
            client.list_bookings(attendee_email="attendee@example.com", cursor="prior+cursor="),
            {"status": "success", "data": [BOOKING], "pagination": page},
        )
        self.assertEqual(client.booking("example-booking"), BOOKING)
        self.assertEqual(
            client.create_booking(
                start=datetime.fromisoformat(f"{DAY}T10:00:00-08:00"),
                event_type_id=12,
                name="Test Attendee",
                email="attendee@example.com",
                time_zone="America/Los_Angeles",
                notes="A test note",
                notes_field="custom-notes",
            ),
            BOOKING,
        )
        self.assertEqual(client.cancel_booking("example-booking", reason="Changed plans")["status"], "cancelled")
        self.assertEqual(client.reschedule_booking("example-booking", START)["uid"], "replacement")

        expected = [
            ("GET", "/v2/me", None),
            ("GET", "/v2/event-types", "2026-06-12"),
            ("GET", "/v2/slots", "2024-09-04"),
            ("GET", "/v2/bookings", "2026-05-01"),
            ("GET", "/v2/bookings/example-booking", "2026-02-25"),
            ("POST", "/v2/bookings", "2026-02-25"),
            ("POST", "/v2/bookings/example-booking/cancel", "2026-02-25"),
            ("POST", "/v2/bookings/example-booking/reschedule", "2026-02-25"),
        ]
        actual = []
        for request, timeout in self.transport.calls:
            headers = {key.lower(): value for key, value in request.header_items()}
            actual.append((request.method, urlsplit(request.full_url).path, headers.get("cal-api-version")))
            self.assertEqual(headers["authorization"], "Bearer test-key")
            self.assertEqual(headers["accept"], "application/json")
            self.assertIn("LiveX", headers["user-agent"])
            self.assertEqual(timeout, 6)
        self.assertEqual(actual, expected)
        self.assertEqual(self.query_for(1), {"username": ["test-host"]})
        self.assertEqual(
            self.query_for(2),
            {
                "eventTypeId": ["12"],
                "start": [DAY],
                "end": [DAY],
                "format": ["range"],
                "timeZone": ["America/Los_Angeles"],
                "bookingUidToReschedule": ["example-booking"],
            },
        )
        self.assertEqual(
            self.query_for(3),
            {
                "attendeeEmail": ["attendee@example.com"],
                "status": ["upcoming"],
                "sortStart": ["asc"],
                "limit": ["20"],
                "cursor": ["prior+cursor="],
            },
        )
        self.assertEqual(
            self.body_for(5),
            {
                "start": START,
                "eventTypeId": 12,
                "attendee": {
                    "name": "Test Attendee",
                    "email": "attendee@example.com",
                    "timeZone": "America/Los_Angeles",
                    "language": "en",
                },
                "metadata": {"source": "chat-assistant"},
                "bookingFieldsResponses": {"custom-notes": "A test note"},
            },
        )
        self.assertEqual(self.body_for(6), {"cancellationReason": "Changed plans"})
        self.assertEqual(self.body_for(7), {"start": START, "reschedulingReason": "User requested reschedule"})
        self.assertEqual(self.observations[5].status, 201)
        self.assertEqual(self.observations[5].path, "/v2/bookings")

    def test_booking_reads_are_uncached_and_optional_filters_are_omitted(self):
        empty_page = success([], pagination={"nextCursor": None, "hasMore": False})
        client = self.make_client(empty_page, empty_page, success(BOOKING), success(BOOKING))
        for _ in range(2):
            client.list_bookings(status=None)
        for _ in range(2):
            client.booking("example-booking")
        self.assertEqual(len(self.transport.calls), 4)
        self.assertEqual(self.query_for(0), {"sortStart": ["asc"], "limit": ["20"]})

    def test_booking_date_bounds_are_sent_as_utc_with_cursor_pagination(self):
        empty_page = success([], pagination={"nextCursor": None, "hasMore": False})
        client = self.make_client(empty_page, empty_page)
        client.list_bookings(
            after_start=datetime.fromisoformat(f"{DAY}T10:00:00-08:00"),
            before_end=f"{DAY}T19:30:00+01:00",
            limit=10,
            cursor="later+page=",
        )
        self.assertEqual(
            self.query_for(0),
            {
                "afterStart": [START],
                "beforeEnd": [END],
                "status": ["upcoming"],
                "sortStart": ["asc"],
                "limit": ["10"],
                "cursor": ["later+page="],
            },
        )
        client.list_bookings(attendee_email="attendee@example.com", status=None, after_start=START)
        self.assertEqual(
            self.query_for(1),
            {
                "attendeeEmail": ["attendee@example.com"],
                "afterStart": [START],
                "sortStart": ["asc"],
                "limit": ["20"],
            },
        )

    def test_invalid_or_reversed_booking_date_bounds_do_not_call_cal(self):
        client = self.make_client()
        for bounds in (
            {"after_start": DAY},
            {"before_end": datetime.fromisoformat(f"{DAY}T18:30:00")},
            {"after_start": END, "before_end": START},
            {"after_start": START, "before_end": f"{DAY}T10:00:00-08:00"},
        ):
            with self.subTest(bounds=bounds), self.assertRaises(CalError) as raised:
                client.list_bookings(**bounds)
            self.assertEqual(raised.exception.code, "invalid")
        self.assertEqual(self.transport.calls, [])

    def test_retry_only_network_failures_and_5xx_reads(self):
        client = self.make_client(
            URLError("timeout"),
            success(PROFILE),
            (503, b"gateway unavailable"),
            success([EVENT_TYPE]),
            (502, b"upstream failure"),
        )
        self.assertEqual(client.me(), PROFILE)
        self.assertEqual(client.event_types("test-host"), [EVENT_TYPE])
        with self.assertRaises(CalError) as caught:
            client.cancel_booking("example-booking")
        self.assertEqual(caught.exception.code, "upstream")
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(caught.exception.body, "upstream failure")
        self.assertEqual([observation.status for observation in self.observations], [None, 200, 503, 200, 502])
        self.assertEqual(len(self.transport.calls), 5)

    def test_rate_limit_not_found_and_auth_failures_are_not_retried(self):
        client = self.make_client((429, {}), (404, {}), (401, {"secret_detail": "private response"}))
        for status, code in [(429, "rate_limited"), (404, "not_found"), (401, "invalid")]:
            with self.subTest(status=status), self.assertRaises(CalError) as caught:
                client.booking("example-booking")
            self.assertEqual(caught.exception.status, status)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("private response", str(caught.exception))
        self.assertEqual(len(self.transport.calls), 3)

    def test_invalid_envelope_and_payload_shapes_raise_typed_errors(self):
        client = self.make_client(
            (200, b"not JSON"),
            success({DAY: [START]}),
            success([], pagination={"hasMore": True, "nextCursor": None}),
        )
        for call in (client.me, lambda: client.slots(12, DAY, DAY), client.list_bookings):
            with self.assertRaises(CalError) as caught:
                call()
            self.assertEqual(caught.exception.code, "upstream")
            self.assertEqual(caught.exception.status, 200)
        self.assertEqual(len(self.transport.calls), 3)

    def test_profile_cache_expires_and_protects_cached_values(self):
        client = self.make_client(success(PROFILE), success({**PROFILE, "username": "updated-host"}))
        client.me()["username"] = "modified locally"
        self.clock.now = 3599
        self.assertEqual(client.me()["username"], "test-host")
        self.clock.now = 3600
        self.assertEqual(client.me()["username"], "updated-host")
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual([observation.cache_hit for observation in self.observations], [False, True, False])
        self.assertIsNone(self.observations[1].body)

    def test_slot_cache_distinguishes_query_bounds_and_reschedule_uid(self):
        client = self.make_client(
            success(SLOTS), success({}), success({}), success({}), success({}), success(SLOTS)
        )
        self.assertEqual(client.slots(12, DAY, DAY), SLOTS)
        self.assertEqual(client.slots(12, DAY, DAY, booking_uid_to_reschedule="one"), {})
        self.assertEqual(client.slots(12, DAY, DAY, booking_uid_to_reschedule="two"), {})
        self.assertEqual(client.slots(12, DAY, END), {})
        self.assertEqual(client.slots(12, DAY, DAY, "America/Los_Angeles"), {})
        self.clock.now = 59
        self.assertEqual(client.slots(12, DAY, DAY), SLOTS)
        self.assertEqual(len(self.transport.calls), 5)
        self.clock.now = 60
        self.assertEqual(client.slots(12, DAY, DAY), SLOTS)
        self.assertEqual(len(self.transport.calls), 6)

    def test_inflight_slots_are_not_cached_after_a_write(self):
        def availability_fetched_before_write():
            client.reschedule_booking("example-booking", START)
            return success(SLOTS)

        client = self.make_client(availability_fetched_before_write, success(BOOKING), success({}))
        self.assertEqual(client.slots(12, DAY, DAY), SLOTS)
        self.assertEqual(client.slots(12, DAY, DAY), {})
        self.assertEqual(len(self.transport.calls), 3)

    def create(self, client, **changes):
        details = {
            "start": START,
            "event_type_id": 12,
            "name": "Test Attendee",
            "email": "attendee@example.com",
            "time_zone": "UTC",
            **changes,
        }
        return client.create_booking(**details)

    def test_conflict_rechecks_fresh_slots_after_a_failed_write(self):
        error_body = {"status": "error", "error": {"message": "rejected"}}
        client = self.make_client(success(SLOTS), (400, error_body), success({}))
        client.slots(12, DAY, DAY)
        with self.assertRaises(CalError) as caught:
            self.create(client, notes="Do not send without a known notes field")
        self.assertEqual(caught.exception.code, "slot_taken")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.body, error_body)
        self.assertNotIn("bookingFieldsResponses", self.body_for(1))
        self.assertEqual(self.query_for(2)["start"], [DAY])
        self.assertEqual([call[0].method for call in self.transport.calls], ["GET", "POST", "GET"])

    def test_conflict_remains_invalid_when_the_same_instant_is_available(self):
        offset_slots = {DAY: [{"start": f"{DAY}T10:00:00-08:00", "end": f"{DAY}T10:30:00-08:00"}]}
        client = self.make_client((400, {}), success(offset_slots))
        with self.assertRaises(CalError) as caught:
            self.create(client)
        self.assertEqual(caught.exception.code, "invalid")
        self.assertEqual(len(self.transport.calls), 2)

    def test_failed_availability_check_preserves_original_write_error(self):
        client = self.make_client((409, {}), URLError("timeout"), URLError("timeout"))
        with self.assertRaises(CalError) as caught:
            self.create(client)
        self.assertEqual(caught.exception.code, "invalid")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual([call[0].method for call in self.transport.calls], ["POST", "GET", "GET"])

    def test_rate_limited_write_avoids_a_conflict_probe(self):
        client = self.make_client((429, {}))
        with self.assertRaises(CalError) as caught:
            self.create(client)
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertEqual(len(self.transport.calls), 1)

    def test_fresh_slots_and_a_successful_write_invalidate_cached_availability(self):
        client = self.make_client(success(SLOTS), success({}), success(BOOKING, 201), success(SLOTS))
        self.assertEqual(client.slots(12, DAY, DAY), SLOTS)
        self.assertEqual(client.slots(12, DAY, DAY, fresh=True), {})
        self.assertEqual(client.slots(12, DAY, DAY), {})
        self.create(client)
        self.assertEqual(client.slots(12, DAY, DAY), SLOTS)
        self.assertEqual(len(self.transport.calls), 4)

    def test_rejects_naive_write_timestamps_before_any_request(self):
        client = self.make_client()
        with self.assertRaises(ValueError):
            self.create(client, start=f"{DAY}T10:00:00")
        self.assertEqual(len(self.transport.calls), 0)


if __name__ == "__main__":
    unittest.main()
