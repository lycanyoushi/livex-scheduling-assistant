"""Offline coverage for the approved evaluation runner."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from backend.model import ModelCompletion, ModelError, ModelText, ToolCall
from evals.checker import EvalError, render, safe_reason
from evals.runner import (
    COMPARISON_MODEL, DEFAULT_MODEL, SPEC_PATH, Pacing, aggregate, approved_spec, case_report, checkpoint, run_case,
)


class CancellationScenario:
    def __init__(self):
        self.calls = 0

    async def stream(self, **_):
        self.calls += 1
        if self.calls == 1:
            yield ModelText("Which booking do you want to cancel?")
            yield ModelCompletion("first-turn", "completed")
        else:
            yield ModelCompletion("second-turn", "requires_action", (
                ToolCall("cancel-target", "propose_cancel", {"booking_ref": "B2", "reason": ""}),
            ))


class EvalRunnerTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = approved_spec()
        cls.fixture = json.loads(SPEC_PATH.with_name(cls.spec["calendar_fixture"]).read_text(encoding="utf-8"))
        cls.cases = {case["id"]: case for case in cls.spec["cases"]}

    def test_booking_visibility_respects_event_order(self):
        inventory = {"type": "refs", "bookings": {"B1": {}, "B2": {}}}
        cards = {"type": "booking_cards", "refs": ["B1", "B2"]}
        first = {"type": "text_delta", "text": "Maybe [[B"}
        second = {"type": "text_delta", "text": "2]]?"}
        self.assertEqual(render([inventory])[3], [])
        self.assertEqual(render([inventory, cards, first, second])[3], ["B1", "B2"])
        self.assertEqual(render([inventory, first, cards, second])[3], ["B1", "B2"])
        self.assertEqual(render([inventory, first, second, cards])[3], ["B2", "B1"])

    def test_case_14_resolves_second_displayed_booking_without_inline_marker(self):
        case = self.cases[14]
        result = case_report(case, 1, run_case(self.spec, case, self.fixture, CancellationScenario(), None, None,
                                              model_name=DEFAULT_MODEL))
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["turns"][0]["booking_offer_order"], ["sam_intro", "sam_followup"])
        self.assertEqual(result["turns"][0]["inline_booking_order"], [])
        required = next(item for item in result["turns"][1]["checks"] if item["check"] == "required_tool:propose_cancel")
        self.assertEqual(required["observed"], {"matching_calls": 1, "tool_name_attempts": 1})
        self.assertNotIn("fixture-sam-followup", json.dumps(result))
        self.assertNotIn("cancel-target", json.dumps(result))

    def test_fixture_only_cases_9_22_23_pass_and_checkpoint_each_turn(self):
        for number in (9, 22, 23):
            with self.subTest(case=number):
                case = self.cases[number]
                snapshots = []
                turns = run_case(self.spec, case, self.fixture, None, None, None, model_name=DEFAULT_MODEL,
                                 on_turn=lambda progress: snapshots.append(case_report(case, 1, progress)))
                result = case_report(case, 1, turns)
                self.assertEqual(len(snapshots), len(case["turns"]))
                self.assertEqual(snapshots[0]["status"], "incomplete")
                self.assertEqual(result["status"], "pass")
                self.assertNotIn("@example.com", json.dumps(result))
                if number == 23:
                    errors = result["turns"][0]["provider_errors"]
                    self.assertEqual([item["http_status"] for item in errors], [429, 503])
                    self.assertTrue(all(item["simulated"] for item in errors))

    def test_thresholds_and_wording_failures_stay_distinct(self):
        statuses = ["pass", "wording_only_failure", "pass"]
        runs = [{"case": case, "status": status, "checks": {}, "turns": []}
                for case in (4, 16) for status in statuses]
        data = aggregate(runs, [4, 16], 3, 2, {16}, self.spec["performance_targets"])
        self.assertTrue(data["cases"]["4"]["passed"])
        self.assertFalse(data["cases"]["16"]["passed"])
        self.assertEqual(data["run_status"]["wording_only_failure"], 2)

    def test_per_turn_token_totals_and_pace_adjusted_latency(self):
        def model_call(input_tokens, output_tokens):
            return {"inputTokens": input_tokens, "outputTokens": output_tokens, "cachedTokens": 0, "ttftMs": 2000}

        turn = {"kind": "text", "model_calls": 2, "turn_ms": 4500, "first_feedback_ms": 50, "cal_ms": 0,
                "models": [model_call(600, 15), model_call(700, 25)], "model_pace_wait_ms": [1000, 1500]}
        runs = [{"case": 1, "status": "pass", "checks": {}, "turns": [turn]}]
        data = aggregate(runs, [1], 1, 1, set(), self.spec["performance_targets"])
        self.assertEqual(data["input_tokens_per_turn"], {"n": 1, "p50": 1300, "p90": 1300})
        self.assertEqual(data["output_tokens_per_turn"], {"n": 1, "p50": 40, "p90": 40})
        self.assertEqual(data["text_turn_latency_ms_excluding_pace"]["p50"], 2000)

    def test_checkpoint_replaces_partial_report(self):
        with TemporaryDirectory() as folder:
            output = Path(folder) / "result.json"
            checkpoint(output, {"complete": False, "runs": []})
            checkpoint(output, {"complete": True, "runs": [1]})
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"complete": True, "runs": [1]})
            self.assertEqual(list(Path(folder).iterdir()), [output])

    def test_provider_reason_is_allowlisted(self):
        self.assertEqual(safe_reason("stream_ended_without_completion"), "stream_ended_without_completion")
        self.assertEqual(safe_reason("provider_http_503"), "provider_http_503")
        self.assertIsNone(safe_reason("provider quoted private@example.com"))


class PacingTests(IsolatedAsyncioTestCase):
    async def test_paces_and_backs_off_before_repeated_quota_stops_run(self):
        pace = Pacing(10)
        with patch("evals.runner.monotonic", return_value=100), patch("evals.runner.asyncio.sleep", new=AsyncMock()) as wait:
            await pace.start(DEFAULT_MODEL)
            await pace.start(DEFAULT_MODEL)
            wait.assert_awaited_with(10)
            pace.outcome(DEFAULT_MODEL, ModelError("rate_limited", 429))
            await pace.start(DEFAULT_MODEL)
            wait.assert_awaited_with(65)
            pace.outcome(DEFAULT_MODEL, ModelError("rate_limited", 429))
            with self.assertRaises(ModelError):
                await pace.start(DEFAULT_MODEL)
        self.assertEqual(pace.stop, "repeated_provider_quota")
        self.assertEqual(pace.http_errors[DEFAULT_MODEL, 429], 2)

    async def test_comparison_stops_after_consecutive_early_provider_failures(self):
        pace = Pacing(10, comparison=True)
        pace.outcome(COMPARISON_MODEL, ModelError("model_down", 503), saw_text=False)
        pace.outcome(COMPARISON_MODEL, ModelError("timeout"), saw_text=False)
        self.assertEqual(pace.stop, "comparison_unavailable")
