"""Offline regressions for tool access on verified choices and exact selections."""

import json
from unittest import TestCase

from backend.model import ModelCompletion, ModelText, ToolCall
from evals.runner import DEFAULT_MODEL, SPEC_PATH, approved_spec, case_report, run_case


class SchedulingToolPolicyTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = approved_spec()
        cls.fixture = json.loads(SPEC_PATH.with_name(cls.spec["calendar_fixture"]).read_text(encoding="utf-8"))
        cls.cases = {case["id"]: case for case in cls.spec["cases"]}

    def run_fixture(self, case_id, model):
        case = self.cases[case_id]
        result = case_report(case, 1, run_case(self.spec, case, self.fixture, model, None, None,
                                              model_name=DEFAULT_MODEL))
        failures = [check for turn in result["turns"] for check in turn["checks"] if check["result"] != "pass"]
        self.assertEqual(result["status"], "pass", failures)

    def test_checked_openings_and_contact_update_are_response_only(self):
        test = self

        class ReplyOnly:
            def __init__(self):
                self.calls = 0

            async def stream(self, *, tools, previous_interaction_id, **_):
                self.calls += 1
                test.assertEqual(tools, [])
                if self.calls > 1:
                    test.assertEqual(previous_interaction_id, "previous-1")
                yield ModelText("Which time works for you?")
                yield ModelCompletion(f"previous-{self.calls}", "completed")

        for case_id, calls in ((2, 1), (30, 2)):
            with self.subTest(case=case_id):
                model = ReplyOnly()
                self.run_fixture(case_id, model)
                self.assertEqual(model.calls, calls)

    def test_replacing_a_pending_booking_binds_its_exact_start_and_closes_the_previous_call(self):
        test = self

        class ConfirmExact:
            def __init__(self):
                self.calls = 0

            async def stream(self, *, tools, input_steps, **_):
                self.calls += 1
                test.assertEqual([tool["name"] for tool in tools], ["propose_booking"])
                [ref] = tools[0]["parameters"]["properties"]["slot_ref"]["enum"]
                if self.calls == 2:
                    test.assertEqual(input_steps[0]["type"], "function_result")
                    test.assertEqual(input_steps[0]["call_id"], "book-1")
                    test.assertEqual(input_steps[0]["result"]["outcome"], "no_action_yet")
                yield ModelCompletion(f"exact-{self.calls}", "requires_action", (
                    ToolCall(f"book-{self.calls}", "propose_booking", {"slot_ref": ref, "name": "", "notes": ""}),
                ))

        model = ConfirmExact()
        self.run_fixture(8, model)
        self.assertEqual(model.calls, 2)

    def test_verified_reschedule_options_do_not_propose_automatically(self):
        self.run_fixture(25, None)
