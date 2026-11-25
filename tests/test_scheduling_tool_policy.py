"""Offline regressions for tool access on verified choices and exact selections."""

from datetime import datetime
import json
from unittest import IsolatedAsyncioTestCase, TestCase
from zoneinfo import ZoneInfo

from backend.demo_calendar import DemoCalendar
from backend.model import ModelCompletion, ModelText, ToolCall
from backend.protocol import ChatRequest
from backend.state import verify_state
from backend.turn import TurnService
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


class PublishedSlotTests(IsolatedAsyncioTestCase):
    async def test_slot_choices_stay_resolvable_across_batches_that_exhaust_signed_capacity(self):
        zone = ZoneInfo("Asia/Singapore")
        now = datetime(2026, 11, 23, 10, tzinfo=zone)
        secret = "fixture-signing-key-for-published-slots"
        for text, batches in (
            ("Find openings in two weeks.", (("2026-12-07", "2026-12-08", "2026-12-09"),)),
            ("What can you tell me about scheduling?", (("2026-12-07",), ("2026-12-08", "2026-12-09", "2026-12-10"))),
        ):
            with self.subTest(batches=len(batches)):
                class LookupBatches:
                    async def stream(self, **_):
                        dates = next(pending, None)
                        if dates is None:
                            yield ModelText("Here are the checked openings.")
                            yield ModelCompletion("complete", "completed")
                            return
                        yield ModelCompletion(f"lookup-{dates[0]}", "requires_action", tuple(
                            ToolCall(f"find-{day}", "find_slots", {
                                "from_date": day, "part_of_day": "any", "for_booking_ref": "",
                            }) for day in dates
                        ))

                pending = iter(batches)
                calendar = DemoCalendar(clock=lambda: now, time_zone=zone.key)
                service = TurnService(model=LookupBatches(), calendar=calendar, secret=secret, clock=lambda: now)
                self.addCleanup(service.close_prefetch)
                request = ChatRequest.model_validate({
                    "timeZone": zone.key, "stateToken": None, "input": {"kind": "text", "text": text},
                })
                published, displayed, retired = set(), set(), set()
                async for event in service.events(request):
                    if event["type"] == "refs":
                        removals = event.get("retiredSlots", ())
                        retired.update(removals)
                        published.difference_update(removals)
                        displayed.difference_update(removals)
                        published.update(event.get("slots", {}))
                    if event["type"] == "slot_chips":
                        self.assertTrue(set(event["refs"]) <= published)
                        displayed.update(event["refs"])
                    if event["type"] == "turn_end":
                        state = verify_state(event["stateToken"], secret, now)
                        self.assertTrue(published <= state.slots.keys())
                        self.assertEqual(displayed, set(state.last_offered_slots))
                        self.assertEqual(len(displayed), 4)
                        self.assertTrue(all(call["ok"] for call in event["metrics"]["toolCalls"]))
                        self.assertEqual(len(event["metrics"]["toolCalls"]), sum(map(len, batches)))
                self.assertEqual(bool(retired), len(batches) > 1)
