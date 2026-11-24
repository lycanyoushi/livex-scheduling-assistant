"""Gemini Interactions transport for the scheduling model."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
import json
import logging

from google import genai
from google.genai import errors as genai_errors, types
from google.genai._interactions.core.error import APIError, APITimeoutError

from backend.model import ModelCompletion, ModelError, ModelStreamStart, ModelText, ToolCall


_TIMEOUT_SECONDS = 20.0
_FIRST_EVENT_TIMEOUT_SECONDS = 8.0
_STREAM_IDLE_TIMEOUT_SECONDS = 8.0
_MAX_OUTPUT_TOKENS = 800
_LOGGER = logging.getLogger(__name__)
_REFUSAL_CODES = {"safety", "jailbreak", "prohibited_content", "blocklist"}
_TERMINAL_STATUSES = {"cancelled", "failed", "in_progress", "queued", "requires_continuation"}
_CONTINUABLE_INCOMPLETE_MODELS = {"gemini-3.5-flash-lite", "gemini-3.1-flash-lite"}
_MISSING_INTERACTION_CODES = {
    "interaction_expired",
    "interaction_not_found",
    "previous_interaction_not_found",
    "not_found",
}


class _DeadlineExpired(TimeoutError):
    """An absolute deadline elapsed before an otherwise immediate read."""


def _model_error(code: str | None, http_status: int | None, previous_id: str | None) -> ModelError:
    service_code = code.lower() if isinstance(code, str) else None
    if service_code in _REFUSAL_CODES:
        return ModelError("refusal", http_status)
    if http_status == 429 or service_code in {"429", "rate_limited", "resource_exhausted"}:
        return ModelError("rate_limited", http_status)
    if previous_id and (http_status in {404, 410} or service_code in _MISSING_INTERACTION_CODES):
        return ModelError("interaction_expired", http_status)
    if http_status == 408 or service_code in {"deadline_exceeded", "timeout"}:
        return ModelError("timeout", http_status)
    reason = f"provider_http_{http_status}" if type(http_status) is int else "provider_stream_error"
    return ModelError("model_down", http_status, reason=reason)


def _sdk_service_code(error: APIError) -> str | None:
    if not isinstance(error.body, dict):
        return None
    details = error.body.get("error", error.body)
    if not isinstance(details, dict):
        return None
    code = details.get("code")
    return code if isinstance(code, str) else details.get("status")


def _tokens(usage, field: str) -> int:
    value = getattr(usage, field, None)
    return value if type(value) is int and value >= 0 else 0


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.5-flash-lite",
        thinking_level: str = "minimal",
        *,
        persistent: bool = False,
        first_event_timeout: float | None = _FIRST_EVENT_TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._thinking_level = thinking_level
        self._persistent = persistent
        self._first_event_timeout = first_event_timeout
        self._client = None
        self._lifetime = AsyncExitStack()
        self._client_lock = asyncio.Lock()

    def _new_client(self):
        return genai.Client(
            api_key=self._api_key,
            vertexai=False,
            http_options=types.HttpOptions(
                timeout=int(_TIMEOUT_SECONDS * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        ).aio

    @asynccontextmanager
    async def _transport(self):
        if not self._persistent:
            async with self._new_client() as client:
                yield client
            return
        async with self._client_lock:
            if self._client is None:
                self._client = await self._lifetime.enter_async_context(self._new_client())
            client = self._client
        yield client

    async def aclose(self) -> None:
        async with self._client_lock:
            await self._lifetime.aclose()
            self._client = None

    async def check_metadata(self) -> None:
        """Verify the configured key and primary model with one metadata request."""
        try:
            async with asyncio.timeout(_TIMEOUT_SECONDS), self._transport() as client:
                await client.models.get(model=self._model)
        except genai_errors.APIError as error:
            raise _model_error(None, error.code, None) from None
        except TimeoutError:
            raise ModelError("timeout") from None
        except Exception as error:
            raise ModelError("model_down", reason=f"unexpected_exception:{type(error).__name__}") from None

    async def stream(
        self,
        *,
        input_steps: list[dict],
        previous_interaction_id: str | None,
        system_instruction: str,
        tools: list[dict],
    ) -> AsyncIterator[ModelStreamStart | ModelText | ModelCompletion]:
        session = {"previous_interaction_id": previous_interaction_id} if previous_interaction_id else {}
        completion = None
        output_steps: set[int] = set()
        pending_calls: dict[int, tuple[ToolCall, list[str]]] = {}
        finished_calls: dict[int, ToolCall] = {}
        argument_error = None
        received_event = False
        has_text = False
        loop = asyncio.get_running_loop()
        started = loop.time()
        overall_deadline = started + _TIMEOUT_SECONDS
        deadline = overall_deadline if self._first_event_timeout is None else min(
            overall_deadline, started + self._first_event_timeout,
        )
        timer = asyncio.timeout_at(deadline)

        try:
            async with AsyncExitStack() as stream:
                async with timer:
                    client = await stream.enter_async_context(self._transport())
                    sdk_stream = await client.interactions.create(
                        model=self._model,
                        input=input_steps,
                        system_instruction=system_instruction,
                        tools=tools,
                        generation_config={"thinking_level": self._thinking_level, "max_output_tokens": _MAX_OUTPUT_TOKENS},
                        store=True,
                        stream=True,
                        automatic_continuation=False,
                        timeout=_TIMEOUT_SECONDS,
                        **session,
                    )
                    events = aiter(await stream.enter_async_context(sdk_stream))
                while True:
                    if loop.time() >= deadline:
                        raise _DeadlineExpired
                    timer = asyncio.timeout_at(deadline)
                    async with timer:
                        event = await anext(events, None)
                    received = loop.time()
                    if event is not None and not received_event:
                        received_event = True
                        yield ModelStreamStart((received - started) * 1000)
                    if received >= deadline:
                        raise _DeadlineExpired
                    if event is None:
                        break
                    deadline = min(overall_deadline, received + _STREAM_IDLE_TIMEOUT_SECONDS) if has_text else overall_deadline
                    if event.event_type == "error":
                        code = getattr(event.error, "code", None)
                        raise _model_error(code, None, previous_interaction_id)
                    if event.event_type == "step.start":
                        step = event.step
                        if step.type == "model_output":
                            output_steps.add(event.index)
                        elif step.type == "function_call":
                            if event.index in pending_calls or event.index in finished_calls:
                                raise ModelError("model_down", reason="duplicate_call_step")
                            if not step.id or not step.name:
                                raise ModelError("model_down", reason="missing_call_id_or_name")
                            pending_calls[event.index] = (ToolCall(step.id, step.name, step.arguments), [])
                    elif event.event_type == "step.delta":
                        delta = event.delta
                        if delta.type == "text" and event.index in output_steps and delta.text:
                            if not has_text and delta.text.strip():
                                has_text = True
                                deadline = min(overall_deadline, received + _STREAM_IDLE_TIMEOUT_SECONDS)
                            yield ModelText(delta.text)
                        elif delta.type == "arguments_delta" and delta.arguments:
                            if event.index not in pending_calls:
                                raise ModelError("model_down", reason="arguments_without_call_step")
                            pending_calls[event.index][1].append(delta.arguments)
                    elif event.event_type == "step.stop":
                        output_steps.discard(event.index)
                        if event.index in pending_calls:
                            call, fragments = pending_calls.pop(event.index)
                            try:
                                arguments = json.loads("".join(fragments)) if fragments else call.arguments
                            except json.JSONDecodeError:
                                argument_error = "invalid_arguments_json"
                                continue
                            if not isinstance(arguments, dict):
                                argument_error = "arguments_not_an_object"
                                continue
                            finished_calls[event.index] = ToolCall(call.id, call.name, arguments)
                    elif event.event_type == "interaction.completed":
                        terminal = event.interaction
                        calls = tuple(finished_calls[index] for index in sorted(finished_calls))
                        incomplete = terminal.status == "incomplete"
                        if incomplete and not has_text:
                            raise ModelError("model_down", reason="incomplete_without_text")
                        if not incomplete:
                            if terminal.status not in {"completed", "requires_action"}:
                                status = terminal.status if terminal.status in _TERMINAL_STATUSES else "other"
                                raise ModelError("model_down", reason=f"terminal_status_{status}")
                            if not terminal.id:
                                raise ModelError("model_down", reason="missing_interaction_id")
                            if output_steps or pending_calls:
                                raise ModelError("model_down", reason="unclosed_stream_step")
                            if argument_error:
                                raise ModelError("model_down", reason=argument_error)
                            if len({call.id for call in calls}) != len(calls):
                                raise ModelError("model_down", reason="duplicate_call_id")
                            if terminal.status == "requires_action" and not calls:
                                raise ModelError("model_down", reason="requires_action_without_calls")
                        completion = ModelCompletion(
                            terminal.id or "",
                            terminal.status,
                            () if incomplete else calls,
                            input_tokens=_tokens(terminal.usage, "total_input_tokens"),
                            output_tokens=_tokens(terminal.usage, "total_output_tokens"),
                            cached_tokens=_tokens(terminal.usage, "total_cached_tokens"),
                            thought_tokens=_tokens(terminal.usage, "total_thought_tokens"),
                            can_continue=not incomplete or bool(
                                terminal.id and self._model in _CONTINUABLE_INCOMPLETE_MODELS
                                and not (calls or pending_calls or argument_error)
                            ),
                        )
                        break
            if completion is None:
                raise ModelError("model_down", reason=argument_error or "stream_ended_without_completion")
        except Exception as error:
            if isinstance(error, ModelError):
                mapped = error
            elif isinstance(error, TimeoutError) and (timer.expired() or isinstance(error, _DeadlineExpired)):
                if deadline == overall_deadline:
                    reason = "overall_timeout"
                else:
                    reason = "stream_idle_timeout" if has_text else "first_event_timeout"
                mapped = ModelError("timeout", reason=reason)
            elif isinstance(error, (TimeoutError, APITimeoutError)):
                mapped = ModelError("timeout", reason="provider_timeout")
            elif isinstance(error, APIError):
                mapped = _model_error(_sdk_service_code(error), error.status_code, previous_interaction_id)
            else:
                mapped = ModelError("model_down", reason=f"unexpected_exception:{type(error).__name__}")

            if has_text and mapped.code == "timeout":
                completion = ModelCompletion(
                    "", "incomplete", can_continue=False, reason=mapped.reason or "provider_timeout",
                )
            else:
                if mapped.code == "model_down" and mapped.reason:
                    _LOGGER.warning("Gemini model_down reason=%s", mapped.reason)
                if mapped is error:
                    raise
                raise mapped from None
        yield completion
