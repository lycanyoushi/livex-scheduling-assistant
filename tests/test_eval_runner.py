"""Offline coverage for the approved evaluation runner."""

from datetime import datetime, time, timedelta
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from backend.model import ModelCompletion, ModelError, ModelText, ToolCall
from evals.checker import EvalError, check_turn, confirms_signed_proposal, observed_availability, render, safe_reason
from evals.runner import (
    COMPARISON_MODEL, DEFAULT_MODEL, SPEC_PATH, Pacing, Trace, TracingModel, aggregate, approved_spec, case_report, checkpoint,
    TracedCalendar, held_out_cases, model_profile, regression_cases, run_case, suite_fixture_path, turn_payload,
)


class CancellationScenario:
    async def stream(self, **_):
        yield ModelCompletion("second-turn", "requires_action", (
            ToolCall("cancel-target", "propose_cancel", {"booking_ref": "B2", "reason": ""}),
        ))


class OwnerScenario:
    def __init__(self):
        self.calls = 0

    async def stream(self, *, input_steps, **_):
        self.calls += 1
        previous = input_steps[-1]["content"]
        context, user_text = previous[0]["text"], previous[-1]["text"]
        interaction = f"mock-owner-{self.calls}"
        if user_text.startswith("Cancel my 4pm"):
            target = re.search(r"^Prefetched booking (B[1-9][0-9]*): ", context, re.MULTILINE)
            if target is None:
                raise AssertionError("The 4pm booking was missing.")
            yield ModelCompletion(interaction, "requires_action", (
                ToolCall("cancel-target", "propose_cancel", {"booking_ref": target[1], "reason": ""}),
            ))
            return
        yield ModelText("Here are the calendar options.")
        yield ModelCompletion(interaction, "completed")


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

    def test_fixture_only_cases_pass_and_checkpoint_each_turn(self):
        for number in (3, 9, 16, 18, 22, 23, 26, 27):
            with self.subTest(case=number):
                case = self.cases[number]
                snapshots = []
                turns = run_case(self.spec, case, self.fixture, None, None, None, model_name=DEFAULT_MODEL,
                                 on_turn=lambda progress: snapshots.append(case_report(case, 1, progress)))
                result = case_report(case, 1, turns)
                self.assertEqual(len(snapshots), len(case["turns"]))
                if len(case["turns"]) > 1:
                    self.assertEqual(snapshots[0]["status"], "incomplete")
                self.assertEqual(result["status"], "pass", result["turns"])
                self.assertNotIn("@example.com", json.dumps(result))
                if number == 23:
                    errors = result["turns"][0]["provider_errors"]
                    self.assertEqual([item["http_status"] for item in errors], [429, 503])
                    self.assertTrue(all(item["simulated"] for item in errors))

    def test_case_20_credits_only_a_verified_exact_code_proposal(self):
        case = self.cases[20]
        with patch("evals.runner.check_turn", wraps=check_turn) as observed:
            result = case_report(case, 1, run_case(self.spec, case, self.fixture, None, None, None,
                                                  model_name=DEFAULT_MODEL))
        turn, = result["turns"]
        required = next(check for check in turn["checks"] if check["check"] == "required_tool:propose_booking")
        self.assertEqual(result["status"], "pass", turn["checks"])
        self.assertEqual(required["observed"], {"matching_calls": 0, "tool_name_attempts": 0, "code_proposal_match": True})
        self.assertEqual(turn["tool_calls"], [])
        self.assertEqual(turn["code_proposal"]["slot_local_start"], "2026-11-24T14:00")
        self.assertTrue(turn["code_proposal"]["no_calendar_writes"])

        case_id, fixture_turn, payload, events, *state_and_trace = observed.call_args.args
        wrong_slot = {**fixture_turn, "checks": {**fixture_turn["checks"], "tool_calls": [
            {"name": "propose_booking", "args": {"slot_ref": {"local_start": "2026-11-24T15:00"}}},
        ]}}
        wrong_id = [
            {**event, "proposal": {**event["proposal"], "id": "P999"}} if event["type"] == "proposal" else event
            for event in events
        ]
        no_card = [event for event in events if event["type"] != "proposal"]
        for label, identifier, expected, wire in (
            ("wrong slot", case_id, wrong_slot, events),
            ("wrong proposal ID", case_id, fixture_turn, wrong_id),
            ("marker only", case_id, fixture_turn, no_card),
            ("other case", 99, fixture_turn, events),
        ):
            with self.subTest(label=label):
                report = check_turn(identifier, expected, payload, wire, *state_and_trace, rephrases=case_id)
                check = next(row for row in report["checks"] if row["check"] == "required_tool:propose_booking")
                self.assertEqual(check["result"], "failure")

    def test_typed_confirmation_is_a_literal_text_request_with_a_matching_signed_proposal(self):
        prior = {1: {"proposal": {"id": "P1"}}}
        state = SimpleNamespace(proposal=SimpleNamespace(id="P1"))
        replies = {32: "yes", 33: "yes please", 34: "ok"}
        for case in regression_cases(self.spec, set(replies)):
            with self.subTest(case=case["id"]):
                reply = replies[case["id"]]
                turn = case["turns"][1]
                literal = turn_payload(turn, prior)
                self.assertEqual(literal, {"kind": "text", "text": reply, "proposalId": "P1"})
                self.assertTrue(confirms_signed_proposal(turn, literal, prior, state))
                self.assertFalse(confirms_signed_proposal({"user": reply}, literal, prior, state))
                self.assertFalse(confirms_signed_proposal(turn, {"kind": "text", "text": reply}, prior, state))
                self.assertFalse(confirms_signed_proposal(turn, {**literal, "text": f"{reply} if it's tomorrow"}, prior, state))
                other_reply = next(value for value in replies.values() if value != reply)
                self.assertFalse(confirms_signed_proposal(turn, {**literal, "text": other_reply}, prior, state))
                self.assertFalse(confirms_signed_proposal(turn, {**literal, "proposalId": "P2"}, prior, state))
                self.assertFalse(confirms_signed_proposal(turn, literal, prior, SimpleNamespace(proposal=None)))
                self.assertIsNone(turn_payload(turn, {}))

    def test_typed_confirmation_variants_book_through_flask_without_model_calls(self):
        for case in regression_cases(self.spec, {32, 33, 34}):
            with self.subTest(case=case["id"]):
                result = case_report(case, 1, run_case(self.spec, case, self.fixture, None, None, None,
                                                      model_name=DEFAULT_MODEL))
                self.assertEqual(result["status"], "pass", result["turns"])
                proposed, confirmed = result["turns"]
                self.assertEqual(proposed["model_calls"], 0)
                self.assertEqual(confirmed["kind"], "text")
                self.assertEqual(confirmed["model_calls"], 0)
                before = {row["check"]: row["observed"] for row in proposed["checks"]}
                after = {row["check"]: row["observed"] for row in confirmed["checks"]}
                self.assertEqual(before["exact_calendar_writes"], [])
                self.assertEqual([write["operation"] for write in after["exact_calendar_writes"]], ["create_booking"])
                self.assertTrue(after["typed_confirmation_matches_signed_proposal"])
                self.assertTrue(after["typed_confirmation_clears_proposal"])

    def test_case_35_lists_the_owners_cards_without_slot_searches(self):
        case, = regression_cases(self.spec, {35})
        model_calls = []

        async def reply(*, tools, **_):
            model_calls.append(tools)
            yield ModelText("Here are your upcoming bookings.")
            yield ModelCompletion(f"schedule-turn-{len(model_calls)}", "completed")

        result = case_report(case, 1, run_case(self.spec, case, self.fixture, SimpleNamespace(stream=reply), None, None,
                                              model_name=DEFAULT_MODEL))
        self.assertEqual(result["status"], "pass", result["turns"])
        self.assertEqual(model_calls, [[], []])
        for turn in result["turns"]:
            observations = {check["check"]: check["observed"] for check in turn["checks"]}
            self.assertEqual(observations["booking_cards"], [["other_intro", "sam_intro"]])
            self.assertEqual(observations["forbidden_calendar_reads"], [])
            self.assertEqual(observations["exact_calendar_writes"], [])
            self.assertEqual(turn["slot_offer_starts"], [])

    def test_owner_chat_scenarios_cross_flask_and_resolve_prior_refs(self):
        for case_id in (10, 24, 25, 28, 29, 30):
            with self.subTest(case=case_id):
                case = self.cases[case_id]
                runs = run_case(self.spec, case, self.fixture, OwnerScenario(), None, None, model_name=DEFAULT_MODEL)
                result = case_report(case, 1, runs)
                failures = {
                    number: [check for check in report["checks"] if check["result"] != "pass"]
                    for number, report in runs.items()
                }
                self.assertEqual(result["status"], "pass", failures)
                self.assertNotIn("@example.com", json.dumps(result))
                if case_id == 29:
                    self.assertTrue(all(turn["kind"] == "text" for turn in result["turns"]))

    def test_missed_move_follows_the_displayed_meeting_through_typed_confirmation(self):
        case = self.cases[31]
        first, selection, offer, confirm = case["turns"]
        for reply in (selection["user"], "the one with Avery"):
            with self.subTest(reply=reply):
                scenario = {**case, "turns": [first, {**selection, "user": reply}, offer, confirm]}
                runs = run_case(self.spec, scenario, self.fixture, None, None, None, model_name=DEFAULT_MODEL)
                result = case_report(scenario, 1, runs)
                failures = {number: [check for check in report["checks"] if check["result"] != "pass"]
                            for number, report in runs.items()}
                self.assertEqual(result["status"], "pass", failures)
                self.assertEqual(result["turns"][0]["booking_offer_order"], ["owner_tomorrow_early", "owner_tomorrow_late"])
                self.assertEqual(result["turns"][2]["proposal_fixture_booking"], "owner_tomorrow_early")

    def test_thresholds_and_wording_failures_stay_distinct(self):
        statuses = ["pass", "wording_only_failure", "pass"]
        runs = [{"case": case, "status": status, "checks": {}, "turns": []}
                for case in (4, 16) for status in statuses]
        data = aggregate(runs, [4, 16], 3, 2, {16}, self.spec["performance_targets"])
        self.assertTrue(data["cases"]["4"]["passed"])
        self.assertFalse(data["cases"]["16"]["passed"])
        self.assertEqual(data["run_status"]["wording_only_failure"], 2)

    def test_an_unconfirmed_attempt_fails_the_case_even_with_two_passing_repetitions(self):
        unsafe = {"check": "mutations_require_confirmation", "result": "failure", "observed": 1}
        runs = [
            {"case": 1, "status": "structural_failure", "checks": {}, "turns": [{"checks": [unsafe]}]},
            *({"case": 1, "status": "pass", "checks": {}, "turns": []} for _ in range(2)),
        ]
        case = aggregate(runs, [1], 3, 2, set(), self.spec["performance_targets"])["cases"]["1"]
        self.assertEqual(case["passing_runs"], 2)
        self.assertEqual(case["safety_failures"], 1)
        self.assertFalse(case["passed"])

    def test_per_turn_token_totals_and_pace_adjusted_latency(self):
        def model_call(input_tokens, output_tokens):
            return {"inputTokens": input_tokens, "outputTokens": output_tokens, "cachedTokens": 0, "ttftMs": 2000}

        turn = {"kind": "text", "model_calls": 2, "turn_ms": 4500, "first_feedback_ms": 50, "cal_ms": 0, "checks": [],
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
        self.assertEqual(safe_reason("first_event_timeout"), "first_event_timeout")
        self.assertEqual(safe_reason("stream_idle_timeout"), "stream_idle_timeout")
        self.assertIsNone(safe_reason("provider quoted private@example.com"))

    def test_frozen_cases_use_distinct_prompts_and_one_default_repetition(self):
        cases = held_out_cases(self.spec)
        existing = {turn["user"] for case in self.spec["cases"] for turn in case["turns"] if "user" in turn}
        prompts = {turn["user"] for case in cases for turn in case["turns"] if "user" in turn}
        self.assertEqual([case["id"] for case in cases], list(range(101, 107)))
        self.assertEqual(len(prompts), 6)
        self.assertFalse(prompts & existing)
        self.assertEqual(model_profile(self.spec, DEFAULT_MODEL, held_out=True)["repetitions_per_case"], 1)

    def test_follow_up_uses_a_separate_weekday_calendar_with_grounded_availability(self):
        suite = "scheduling_follow_up"
        cases = held_out_cases(self.spec, suite=suite)
        self.assertEqual([case["category"] for case in cases], ["A"] * 6 + ["B"] * 3 + ["C"] * 2)
        path = suite_fixture_path(self.spec, suite)
        self.assertNotEqual(path, suite_fixture_path(self.spec, "scheduling_held_out"))
        fixture = json.loads(path.read_text(encoding="utf-8"))
        for day, starts in fixture["availability"]["local_starts_by_date"].items():
            for start in starts:
                local = datetime.fromisoformat(f"{day}T{start}")
                self.assertLess(local.weekday(), 5)
                self.assertGreaterEqual(local.time(), time(9))
                self.assertLessEqual((local + timedelta(minutes=30)).time(), time(17))

        zone = self.spec["default_time_zone"]
        now = datetime.fromisoformat(self.spec["default_clock"])
        calendar = TracedCalendar(cases[-1], now, zone, fixture, path)
        tomorrow = "2026-11-24"
        at_three, at_four = f"{tomorrow}T15:00", f"{tomorrow}T16:00"
        self.assertEqual(observed_availability(calendar.reads, at_three, zone)["statuses"], [])
        calendar.slots(fixture["availability"]["event_type_id"], tomorrow, tomorrow)
        self.assertEqual(observed_availability(calendar.reads, at_three, zone),
                         {"covering_reads": 1, "statuses": ["unavailable"]})
        self.assertEqual(observed_availability(calendar.reads, at_four, zone),
                         {"covering_reads": 1, "statuses": ["available"]})
        self.assertEqual(observed_availability(calendar.reads, "2026-11-27T08:00", zone)["statuses"], [])

    def test_new_comparison_uses_flash_lite_minimal_once(self):
        profile = model_profile(self.spec, COMPARISON_MODEL)
        self.assertEqual(profile["model"], "gemini-3.1-flash-lite")
        self.assertEqual(profile["thinking_level"], "minimal")
        self.assertEqual(profile["repetitions_per_case"], 1)


class PacingTests(IsolatedAsyncioTestCase):
    async def test_model_input_email_is_blocked_without_contact_in_report(self):
        trace = Trace()
        adapter = SimpleNamespace(stream=AsyncMock())
        model = TracingModel(adapter, DEFAULT_MODEL, "primary", trace, None)
        request = {
            "input_steps": [{"type": "user_input", "content": [{"type": "text", "text": "guest@example.com"}]}],
            "previous_interaction_id": None,
        }
        with self.assertRaises(ModelError):
            await anext(model.stream(**request))
        adapter.stream.assert_not_called()
        self.assertTrue(trace.inputs[0]["contains_email"])
        self.assertEqual(trace.provider_errors[0]["reason"], "fixture_contact_leak")
        self.assertNotIn("guest@example.com", json.dumps([trace.inputs, trace.provider_errors]))

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

    async def test_comparison_tracks_availability_without_stopping_other_cases(self):
        pace = Pacing(10)
        pace.outcome(COMPARISON_MODEL, ModelError("model_down", 503))
        pace.outcome(COMPARISON_MODEL, ModelError("timeout"))
        self.assertIsNone(pace.stop)
        self.assertEqual(pace.http_errors[COMPARISON_MODEL, 503], 1)
