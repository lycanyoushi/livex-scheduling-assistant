from datetime import datetime
from io import BytesIO, StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend.cal_client import CalError
from backend.model import ToolCall
from scripts import live_chat


class LocalResponse(BytesIO):
    def __init__(self, *events, fixture=live_chat.FIXTURE_ID):
        super().__init__(b"".join(json.dumps(event).encode("utf-8") + b"\n" for event in events))
        self.status = 200
        self.headers = {live_chat.FIXTURE_HEADER: fixture}


class LiveChatDriverTests(unittest.TestCase):
    def test_first_raw_event_summary_counts_only_healthy_measured_calls(self):
        rows = [{"modelCalls": [
            {"status": "completed", "firstEventMs": 100},
            {"status": "requires_action", "firstEventMs": 1000},
            {"status": "completed", "firstEventMs": 2500},
            {"status": "timeout", "firstEventMs": 4500},
            {"status": "incomplete", "firstEventMs": 4200},
            {"status": "completed"},
        ]}]
        self.assertEqual(live_chat._healthy_first_event(rows),
                         {"p50": 1000, "p90": 2200, "max": 2500, "calls": 3})
        self.assertIsNone(live_chat._healthy_first_event([]))

    def test_fixture_keeps_future_openings_except_thursday_afternoon_and_refuses_writes(self):
        calendar = live_chat.fixture_calendar()
        start = datetime.fromisoformat("2026-11-26T08:00:00+08:00")
        end = datetime.fromisoformat("2026-11-26T17:00:00+08:00")
        slots = calendar.slots(9001, start, end, live_chat.ZONE)

        self.assertEqual(live_chat.FIXTURE_CLOCK.isoformat(), "2026-11-22T21:00:00+08:00")
        self.assertEqual(len(live_chat.PROMPTS), 10)
        self.assertEqual([item["start"][11:16] for item in slots["2026-11-26"]], ["09:00", "10:00", "11:00"])
        for write in (calendar.create_booking, calendar.cancel_booking, calendar.reschedule_booking):
            with self.subTest(write=write.__name__), self.assertRaises(CalError) as error:
                write("example")
            self.assertEqual(error.exception.status, 403)

    def test_requires_a_loopback_origin_and_the_read_only_fixture_marker(self):
        self.assertEqual(live_chat.loopback_origin("http://127.0.0.1:3000/"), "http://127.0.0.1:3000")
        for invalid in ("http://localhost:3000", "https://127.0.0.1:3000", "http://example.com:3000",
                        "http://127.0.0.1:3000/api/chat", "http://name@127.0.0.1:3000"):
            with self.subTest(origin=invalid), self.assertRaises(ValueError):
                live_chat.loopback_origin(invalid)
        with patch.object(live_chat, "_open_local", return_value=LocalResponse({"eventTypes": [{"id": 9001}]}, fixture=None)):
            self.assertFalse(live_chat.warm("http://127.0.0.1:3000")["ok"])

    def test_report_preserves_safe_tool_args_and_omits_provider_fields(self):
        trace = live_chat.safe_tool_call(ToolCall("unpublished-provider-call-id", "find_slots", {
            "from_date": "2026-11-26", "to_date": "2026-11-27", "part_of_day": "afternoon",
            "for_booking_ref": "B4", "event_type": "unpublished-account-slug", "unpublished-extra": "unpublished-value",
        }))
        self.assertEqual(trace, {
            "name": "find_slots", "omittedArgumentCount": 1,
            "arguments": {"from_date": "2026-11-26", "to_date": "2026-11-27", "part_of_day": "afternoon",
                          "for_booking_ref": "B4", "event_type": "[redacted]"},
        })
        self.assertEqual(live_chat.safe_tool_call(ToolCall("id", "unpublished-tool", {"email": "body"})), {
            "name": "unknown", "arguments": {}, "omittedArgumentCount": 1,
        })

    def test_observer_times_only_eligible_events_and_keeps_session_token_out_of_report(self):
        final = {
            "type": "turn_end", "stateToken": "private-signed-state.fixture",
            "metrics": {
                "outcome": "ok", "totalMs": 30, "prescan": {"dates": ["2026-11-26"]}, "prefetch": {"slots": 1},
                "modelCalls": [{"ttftMs": 20, "thoughtTokens": 4}], "toolCalls": [], "calCalls": [], "unknownRefs": 0,
                "driverToolTrace": [{"calls": []}],
            },
        }
        response = LocalResponse(
            {"type": "status", "text": "Checking."},
            {"type": "refs", "bookings": {"B1": {"title": "Fixture booking"}}},
            {"type": "proposal", "proposal": {"kind": "book"}},
            {"type": "slot_chips", "refs": ["S9"]},
            {"type": "refs", "slots": {"S1": {}}},
            {"type": "slot_chips", "refs": ["S1"]},
            {"type": "text_delta", "text": "Private raw provider text."},
            final,
        )
        saved_slots = set()
        with patch.object(live_chat, "_open_local", return_value=response) as open_local, \
                patch.object(live_chat, "_elapsed", side_effect=range(1, 10)):
            row, token = live_chat.observe_turn("http://127.0.0.1:3000", live_chat.PROMPTS[0], None, saved_slots)

        self.assertEqual(row["firstStatusMs"], 1)
        self.assertEqual(row["firstChipsMs"], row["firstUsefulMs"])
        self.assertEqual(row["firstUsefulMs"], 6)
        self.assertEqual(row["firstTextMs"], 7)
        self.assertEqual(row["modelCalls"][0]["thoughtTokens"], 4)
        self.assertEqual(row["offeredSlotCount"], 1)
        self.assertEqual(token, "private-signed-state.fixture")
        self.assertEqual(saved_slots, {"S1"})
        self.assertNotIn(token, json.dumps(row))
        self.assertNotIn("Private raw provider text", json.dumps(row))
        self.assertEqual(json.loads(open_local.call_args.args[0].data), {
            "input": {"kind": "text", "text": live_chat.PROMPTS[0]}, "stateToken": None, "timeZone": live_chat.ZONE,
        })

    def test_paces_all_ten_prompts_and_waits_after_a_quota_error(self):
        results = [({"outcome": "failed", "error": "rate_limited", "elapsedMs": 10}, "in-memory-token")]
        results += [({"outcome": "ok", "elapsedMs": 10, "modelCalls": [{}]}, "in-memory-token")] * 9
        with TemporaryDirectory() as directory, patch.object(live_chat, "warm", return_value={"type": "warm", "ok": True}), \
                patch.object(live_chat, "observe_turn", side_effect=results) as turn, \
                patch.object(live_chat, "sleep") as pace, patch("sys.stdout", new_callable=StringIO):
            path = Path(directory) / "sample.jsonl"
            self.assertEqual(live_chat.run("http://127.0.0.1:3000", "before", path), 1)
            content = path.read_text(encoding="utf-8")

        self.assertEqual([item.args[1] for item in turn.call_args_list], list(live_chat.PROMPTS))
        self.assertEqual([item.args[2] for item in turn.call_args_list], [None] + ["in-memory-token"] * 9)
        self.assertEqual([item.args[0] for item in pace.call_args_list], [65] + [10] * 8)
        self.assertNotIn("in-memory-token", content)
        self.assertIn('"successful":9', content)
