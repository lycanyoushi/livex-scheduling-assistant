"""Exercise the Gemini adapter with a stubbed SDK transport."""

import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from google.genai._interactions.core.error import APIStatusError

from backend.gemini import GeminiClient
from backend.model import ModelCompletion, ModelError, ModelText, ToolCall


def step_start(index, step_type, **fields):
    return SimpleNamespace(event_type="step.start", index=index, step=SimpleNamespace(type=step_type, **fields))


def step_delta(index, delta_type, **fields):
    return SimpleNamespace(event_type="step.delta", index=index, delta=SimpleNamespace(type=delta_type, **fields))


def step_stop(index):
    return SimpleNamespace(event_type="step.stop", index=index)


def completed(status="completed", **usage):
    return SimpleNamespace(
        event_type="interaction.completed",
        interaction=SimpleNamespace(id="new-interaction", status=status, usage=SimpleNamespace(**usage)),
    )


def error_event(code):
    return SimpleNamespace(event_type="error", error=SimpleNamespace(code=code, message="private provider data"))


class StubStream:
    def __init__(self, events=(), *, block=False):
        self.events = events
        self.block = block
        self.waiting = asyncio.Event()
        self.closed = False
        self.read_cancelled = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def __aiter__(self):
        for event in self.events:
            yield event
        if self.block:
            self.waiting.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.read_cancelled = True
                raise


class StubAsyncClient:
    def __init__(self):
        self.interactions = SimpleNamespace(create=AsyncMock())
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True


class GeminiClientTests(IsolatedAsyncioTestCase):
    async def test_unexpected_failure_logs_only_exception_type(self):
        self.sdk.interactions.create.side_effect = RuntimeError("unredacted provider content")
        with self.assertLogs("backend.gemini", level="WARNING") as logged, self.assertRaises(ModelError) as raised:
            await anext(self.stream())
        self.assertEqual(raised.exception.code, "model_down")
        self.assertEqual(raised.exception.reason, "unexpected_exception:RuntimeError")
        self.assertEqual(logged.output, ["WARNING:backend.gemini:Gemini model_down reason=unexpected_exception:RuntimeError"])

    def setUp(self):
        self.sdk = StubAsyncClient()
        self.sdk_client = self.enterContext(patch("backend.gemini.genai.Client", return_value=SimpleNamespace(aio=self.sdk)))
        self.input_steps = [{"type": "user_input", "content": [{"type": "text", "text": "Find an open time."}]}]
        self.tools = [{"type": "function", "name": "find_slots"}]
        self.client = GeminiClient("test-key")

    def stream(self, previous_id=None):
        return self.client.stream(
            input_steps=self.input_steps,
            previous_interaction_id=previous_id,
            system_instruction="Test scheduler",
            tools=self.tools,
        )

    async def test_streams_text_incrementally_and_counts_final_usage(self):
        stream = StubStream((
            step_start(0, "thought"),
            step_delta(0, "thought_signature", signature="private"),
            step_delta(0, "text", text="private thought"),
            step_stop(0),
            step_start(1, "model_output"),
            step_delta(1, "text", text="Available"),
            step_delta(1, "text", text=" Thursday."),
            step_stop(1),
            completed(total_input_tokens=20, total_output_tokens=5, total_cached_tokens=3, total_thought_tokens=2),
        ))
        self.sdk.interactions.create.return_value = stream
        events = self.stream()
        self.assertEqual(await anext(events), ModelText("Available"))
        self.assertFalse(stream.closed)
        self.assertEqual(await anext(events), ModelText(" Thursday."))
        self.assertEqual(await anext(events), ModelCompletion(
            "new-interaction", "completed", (), input_tokens=20, output_tokens=5, cached_tokens=3, thought_tokens=2,
        ))
        self.assertTrue(stream.closed)
        self.assertTrue(self.sdk.closed)
        with self.assertRaises(StopAsyncIteration):
            await anext(events)

        self.sdk.interactions.create.assert_awaited_once_with(
            model="gemini-3.5-flash-lite",
            input=self.input_steps,
            system_instruction="Test scheduler",
            tools=self.tools,
            generation_config={"thinking_level": "minimal", "max_output_tokens": 800},
            store=True,
            stream=True,
            automatic_continuation=False,
            timeout=20,
        )
        options = self.sdk_client.call_args.kwargs["http_options"]
        self.assertEqual(options.timeout, 20_000)
        self.assertEqual(options.retry_options.attempts, 1)
        self.assertFalse(self.sdk_client.call_args.kwargs["vertexai"])

    async def test_persistent_client_reuses_one_sdk_transport_until_closed(self):
        class CountingClient(StubAsyncClient):
            enters = 0
            exits = 0

            async def __aenter__(self):
                self.enters += 1
                return await super().__aenter__()

            async def __aexit__(self, *error):
                self.exits += 1
                await super().__aexit__(*error)

        sdk = CountingClient()
        self.sdk_client.return_value = SimpleNamespace(aio=sdk)
        self.client = GeminiClient("test-key", persistent=True)
        streams = [StubStream((completed(),)), StubStream((completed(),))]
        sdk.interactions.create.side_effect = streams

        try:
            for stream in streams:
                self.assertEqual([item async for item in self.stream()], [
                    ModelCompletion("new-interaction", "completed", ()),
                ])
                self.assertTrue(stream.closed)
                self.assertFalse(sdk.closed)
            self.sdk_client.assert_called_once()
            self.assertEqual(sdk.enters, 1)
            self.assertEqual(sdk.exits, 0)
            self.assertEqual(sdk.interactions.create.await_count, 2)
        finally:
            await self.client.aclose()

        self.assertTrue(sdk.closed)
        self.assertEqual(sdk.exits, 1)

    async def test_assembles_interleaved_tool_arguments_in_step_order(self):
        self.client = GeminiClient("test-key", "gemini-3.1-flash-lite", "low")
        self.sdk.interactions.create.return_value = StubStream((
            step_start(2, "function_call", id="call-1", name="find_slots", arguments={}),
            step_delta(2, "arguments_delta", arguments='{"window":'),
            step_start(3, "function_call", id="call-2", name="list_bookings", arguments={}),
            step_delta(3, "arguments_delta", arguments='{"range":"upcoming"}'),
            step_stop(3),
            step_delta(2, "arguments_delta", arguments='"afternoon"}'),
            step_stop(2),
            completed("requires_action"),
        ))
        self.assertEqual([event async for event in self.stream("parent")], [ModelCompletion(
            "new-interaction", "requires_action", (
                ToolCall("call-1", "find_slots", {"window": "afternoon"}),
                ToolCall("call-2", "list_bookings", {"range": "upcoming"}),
            ),
        )])
        request = self.sdk.interactions.create.call_args.kwargs
        self.assertEqual(request["previous_interaction_id"], "parent")
        self.assertEqual(request["model"], "gemini-3.1-flash-lite")
        self.assertEqual(request["generation_config"], {"thinking_level": "low", "max_output_tokens": 800})

    async def test_incomplete_or_malformed_stream_exposes_no_calls(self):
        start = step_start(1, "function_call", id="call-1", name="find_slots", arguments={})
        for events, reason in (
            ((start, step_stop(1), completed("incomplete")), "incomplete_without_text"),
            ((start, completed("requires_action")), "unclosed_stream_step"),
            ((start, step_delta(1, "arguments_delta", arguments='{"window":'), step_stop(1)), "invalid_arguments_json"),
            ((start, step_delta(1, "arguments_delta", arguments='["afternoon"]'), step_stop(1)), "arguments_not_an_object"),
            ((start, step_stop(1)), "stream_ended_without_completion"),
            ((completed("failed"),), "terminal_status_failed"),
        ):
            with self.subTest(event_count=len(events)):
                stream = StubStream(events)
                self.sdk.interactions.create.return_value = stream
                with self.assertLogs("backend.gemini", level="WARNING"), self.assertRaises(ModelError) as raised:
                    await anext(self.stream("parent"))
                self.assertEqual(raised.exception.code, "model_down")
                self.assertEqual(raised.exception.reason, reason)
                self.assertTrue(stream.closed)

    async def test_capped_text_matches_probe_and_disallows_continuing_an_unfinished_tool(self):
        output = (step_start(0, "model_output"), step_delta(0, "text", text="Partial reply"), step_stop(0))
        for tool_steps, can_continue in (
            ((), True),
            ((step_start(1, "function_call", id="call-1", name="find_slots", arguments={}),), False),
        ):
            with self.subTest(tool_steps=bool(tool_steps)):
                self.sdk.interactions.create.return_value = StubStream((*output, *tool_steps, completed("incomplete")))
                self.assertEqual([event async for event in self.stream("parent")], [
                    ModelText("Partial reply"),
                    ModelCompletion("new-interaction", "incomplete", can_continue=can_continue),
                ])

    async def test_sanitizes_sdk_status_errors_and_retains_transport_status(self):
        for status, service_code, previous_id, expected in (
            (400, "safety", None, "refusal"),
            (404, "NOT_FOUND", "parent", "interaction_expired"),
            (404, "NOT_FOUND", None, "model_down"),
            (429, "RESOURCE_EXHAUSTED", None, "rate_limited"),
            (503, "UNAVAILABLE", None, "model_down"),
        ):
            with self.subTest(status=status, previous_id=previous_id):
                self.sdk.interactions.create.reset_mock()
                self.sdk.interactions.create.side_effect = APIStatusError(
                    "private provider data",
                    response=SimpleNamespace(status_code=status, request=None),
                    body={"error": {"code": service_code}},
                )
                with self.assertRaises(ModelError) as raised:
                    await anext(self.stream(previous_id))
                self.assertEqual(raised.exception.code, expected)
                self.assertEqual(raised.exception.http_status, status)
                self.assertEqual(str(raised.exception), expected)
                self.assertIsNone(raised.exception.__cause__)
                self.sdk.interactions.create.assert_awaited_once()

    async def test_maps_stream_safety_and_quota_errors(self):
        for service_code, expected in (("jailbreak", "refusal"), ("RESOURCE_EXHAUSTED", "rate_limited")):
            with self.subTest(service_code=service_code):
                self.sdk.interactions.create.return_value = StubStream((error_event(service_code),))
                with self.assertRaises(ModelError) as raised:
                    await anext(self.stream())
                self.assertEqual(raised.exception.code, expected)
                self.assertIsNone(raised.exception.http_status)

    async def test_first_event_deadline_cancels_a_stalled_open(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def waiting_create(**_request):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        self.sdk.interactions.create.side_effect = waiting_create
        self.client = GeminiClient("test-key", first_event_timeout=0.02)
        with self.assertRaises(ModelError) as raised:
            await anext(self.stream())
        self.assertEqual((raised.exception.code, raised.exception.reason), ("timeout", "first_event_timeout"))
        self.assertTrue(entered.is_set())
        self.assertTrue(cancelled.is_set())
        self.assertTrue(self.sdk.closed)

    async def test_primary_cancels_the_first_raw_event_read_and_closes_stream(self):
        self.client = GeminiClient("test-key", first_event_timeout=0.02)
        stream = StubStream(block=True)
        self.sdk.interactions.create.return_value = stream
        with self.assertRaises(ModelError) as raised:
            await anext(self.stream())
        self.assertEqual((raised.exception.code, raised.exception.reason), ("timeout", "first_event_timeout"))
        self.assertTrue(stream.read_cancelled)
        self.assertTrue(stream.closed)
        self.assertTrue(self.sdk.closed)

    async def test_nontext_first_event_and_backup_can_wait_beyond_primary_start_deadline(self):
        class DelayedCompletion(StubStream):
            def __init__(self, earlier):
                super().__init__()
                self.earlier = earlier

            async def __aiter__(self):
                if self.earlier:
                    yield self.earlier
                await asyncio.sleep(0.05)
                yield completed()

        for leading_event, fast_start in (
            (SimpleNamespace(event_type="interaction.created"), 0.025),
            (step_start(0, "thought"), 0.025),
            (None, None),
        ):
            with self.subTest(leading_event=getattr(leading_event, "event_type", None)):
                self.client = GeminiClient("test-key", first_event_timeout=fast_start)
                stream = DelayedCompletion(leading_event)
                self.sdk.interactions.create.return_value = stream
                self.assertEqual([event async for event in self.stream()], [ModelCompletion("new-interaction", "completed")])
                self.assertTrue(stream.closed)

    async def test_raw_activity_renews_idle_deadline_and_idle_exposes_only_partial_text(self):
        class DeadlineClock:
            now = 0.0
            expire_next = False

            def time(self):
                return self.now

            def timeout_at(self, _deadline):
                # Keep virtual time independent of the scheduler, but genuinely cancel the last read.
                return asyncio.timeout_at(asyncio.get_running_loop().time() if self.expire_next else None)

        class ThoughtActivity(StubStream):
            async def __aiter__(self):
                yield step_start(1, "model_output")
                yield step_delta(1, "text", text="First")
                clock.now = 5.0
                yield step_stop(1)
                clock.now = 10.0
                yield step_start(2, "function_call", id="uncommitted", name="propose_booking", arguments={})
                clock.expire_next = True
                yield step_stop(2)
                self.waiting.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    self.read_cancelled = True
                    raise

        for fast_start in (8.0, None):
            with self.subTest(fast_start=fast_start):
                clock = DeadlineClock()
                adapter_asyncio = SimpleNamespace(get_running_loop=lambda: clock, timeout_at=clock.timeout_at)
                self.client = GeminiClient("test-key", first_event_timeout=fast_start)
                stream = ThoughtActivity()
                self.sdk.interactions.create.return_value = stream
                with patch("backend.gemini.asyncio", adapter_asyncio):
                    self.assertEqual([event async for event in self.stream("prior")], [
                        ModelText("First"),
                        ModelCompletion("", "incomplete", can_continue=False, reason="stream_idle_timeout"),
                    ])
                self.assertTrue(stream.waiting.is_set())
                self.assertTrue(stream.read_cancelled)
                self.assertTrue(stream.closed)

    async def test_deadline_covers_the_remaining_stream_after_a_text_chunk(self):
        stream = StubStream((step_start(0, "model_output"), step_delta(0, "text", text="First")), block=True)
        self.sdk.interactions.create.return_value = stream
        with patch("backend.gemini._TIMEOUT_SECONDS", 0.05):
            events = self.stream()
            self.assertEqual(await anext(events), ModelText("First"))
            with self.assertRaises(ModelError) as raised:
                await anext(events)
        self.assertEqual(raised.exception.code, "timeout")
        self.assertTrue(stream.closed)
        self.assertTrue(self.sdk.closed)

    async def test_cancellation_closes_both_stream_and_transport(self):
        stream = StubStream(block=True)
        self.sdk.interactions.create.return_value = stream

        async def consume():
            return [event async for event in self.stream()]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(stream.waiting.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stream.closed)
        self.assertTrue(self.sdk.closed)
