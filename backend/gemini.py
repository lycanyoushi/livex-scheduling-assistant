"""Gemini Interactions transport for the scheduling model."""

import asyncio
from collections.abc import AsyncIterator
import json

from google import genai
from google.genai import types
from google.genai._interactions.core.error import APIError, APITimeoutError

from backend.model import ModelCompletion, ModelError, ModelText, ToolCall


_TIMEOUT_SECONDS = 20.0
_REFUSAL_CODES = {"safety", "jailbreak", "prohibited_content", "blocklist"}
_MISSING_INTERACTION_CODES = {
    "interaction_expired",
    "interaction_not_found",
    "previous_interaction_not_found",
    "not_found",
}


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
    return ModelError("model_down", http_status)


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
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._thinking_level = thinking_level

    async def stream(
        self,
        *,
        input_steps: list[dict],
        previous_interaction_id: str | None,
        system_instruction: str,
        tools: list[dict],
    ) -> AsyncIterator[ModelText | ModelCompletion]:
        session = {"previous_interaction_id": previous_interaction_id} if previous_interaction_id else {}
        completion = None
        output_steps: set[int] = set()
        pending_calls: dict[int, tuple[ToolCall, list[str]]] = {}
        finished_calls: dict[int, ToolCall] = {}

        try:
            # Each Flask turn owns its event loop. Scope the transport to that loop.
            async with asyncio.timeout(_TIMEOUT_SECONDS), genai.Client(
                api_key=self._api_key,
                vertexai=False,
                http_options=types.HttpOptions(
                    timeout=int(_TIMEOUT_SECONDS * 1000),
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            ).aio as client:
                async with await client.interactions.create(
                    model=self._model,
                    input=input_steps,
                    system_instruction=system_instruction,
                    tools=tools,
                    generation_config={"thinking_level": self._thinking_level},
                    store=True,
                    stream=True,
                    automatic_continuation=False,
                    timeout=_TIMEOUT_SECONDS,
                    **session,
                ) as events:
                    async for event in events:
                        if event.event_type == "error":
                            code = getattr(event.error, "code", None)
                            raise _model_error(code, None, previous_interaction_id)
                        if event.event_type == "step.start":
                            step = event.step
                            if step.type == "model_output":
                                output_steps.add(event.index)
                            elif step.type == "function_call":
                                if event.index in pending_calls or event.index in finished_calls:
                                    raise ModelError("model_down")
                                if not step.id or not step.name:
                                    raise ModelError("model_down")
                                pending_calls[event.index] = (ToolCall(step.id, step.name, step.arguments), [])
                        elif event.event_type == "step.delta":
                            delta = event.delta
                            if delta.type == "text" and event.index in output_steps and delta.text:
                                yield ModelText(delta.text)
                            elif delta.type == "arguments_delta" and delta.arguments:
                                if event.index not in pending_calls:
                                    raise ModelError("model_down")
                                pending_calls[event.index][1].append(delta.arguments)
                        elif event.event_type == "step.stop":
                            output_steps.discard(event.index)
                            if event.index in pending_calls:
                                call, fragments = pending_calls.pop(event.index)
                                arguments = json.loads("".join(fragments)) if fragments else call.arguments
                                if not isinstance(arguments, dict):
                                    raise ModelError("model_down")
                                finished_calls[event.index] = ToolCall(call.id, call.name, arguments)
                        elif event.event_type == "interaction.completed":
                            terminal = event.interaction
                            calls = tuple(finished_calls[index] for index in sorted(finished_calls))
                            if (
                                terminal.status not in {"completed", "requires_action"}
                                or not terminal.id
                                or output_steps
                                or pending_calls
                                or len({call.id for call in calls}) != len(calls)
                                or (terminal.status == "requires_action" and not calls)
                            ):
                                raise ModelError("model_down")
                            completion = ModelCompletion(
                                terminal.id,
                                terminal.status,
                                calls,
                                input_tokens=_tokens(terminal.usage, "total_input_tokens"),
                                output_tokens=_tokens(terminal.usage, "total_output_tokens"),
                                cached_tokens=_tokens(terminal.usage, "total_cached_tokens"),
                            )
                            break
        except ModelError:
            raise
        except (TimeoutError, APITimeoutError):
            raise ModelError("timeout") from None
        except APIError as error:
            raise _model_error(_sdk_service_code(error), error.status_code, previous_interaction_id) from None
        except Exception:
            raise ModelError("model_down") from None

        if completion is None:
            raise ModelError("model_down")
        yield completion
