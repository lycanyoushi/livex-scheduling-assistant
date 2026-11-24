"""Probe free-tier Gemini Interactions with synthetic, read-only scheduling tools."""

import argparse
import asyncio
from dataclasses import dataclass, field
import json
import logging
from typing import Any
import warnings

from google import genai
from google.genai import types

from scripts.local_environment import load_local_environment


FLASH = "gemini-3.8-flash"
FLASH_LITE = "gemini-3.5-flash-lite"
TIMEOUT_SECONDS = 20
SYSTEM = "This is a synthetic scheduling test. Follow the exact response instructions."
TOOLS = [
    {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "The requested synthetic time window."}
            },
            "required": ["window"],
        },
    }
    for name, description in (
        ("find_slots", "Look up synthetic available slots for the requested window."),
        ("check_duration", "Look up synthetic event durations for the requested window."),
    )
]
USAGE_FIELDS = (
    "total_cached_tokens",
    "total_input_tokens",
    "total_output_tokens",
    "total_thought_tokens",
    "total_tokens",
    "total_tool_use_tokens",
)
SERVICE_CODES = {
    "ABORTED", "BLOCKLIST", "DEADLINE_EXCEEDED", "FAILED_PRECONDITION", "INTERNAL",
    "INVALID_ARGUMENT", "JAILBREAK", "MAX_TOKENS", "NOT_FOUND", "PERMISSION_DENIED",
    "PROHIBITED_CONTENT", "RESOURCE_EXHAUSTED", "SAFETY", "UNAUTHENTICATED", "UNAVAILABLE", "UNKNOWN",
}
INTERACTION_STATUSES = {
    "cancelled", "completed", "failed", "in_progress", "incomplete",
    "requires_action", "requires_continuation",
}
TOOL_TURN_STATUSES = {"completed", "requires_action"}
TRANSIENT_SERVICE_CODES = {"DEADLINE_EXCEEDED", "INTERNAL", "RESOURCE_EXHAUSTED", "UNAVAILABLE"}


class ProbeUnavailable(Exception):
    """Stop further calls after a provider outage or project quota error."""


@dataclass(repr=False)
class Call:
    id: str
    name: str
    arguments: dict[str, Any]
    fragments: list[str] = field(default_factory=list)


@dataclass(repr=False)
class Observation:
    id: str | None = None
    status: str | None = None
    event_types: set[str] = field(default_factory=set)
    delta_types: set[str] = field(default_factory=set)
    step_types: set[str] = field(default_factory=set)
    calls: list[Call] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    error_type: str | None = None
    service_code: str | None = None
    http_status: int | None = None
    argument_fragments: int = 0

    def contains(self, marker: str) -> bool:
        return marker in "".join(self.text)

    def unavailable(self) -> bool:
        return (
            self.http_status == 429
            or (self.http_status is not None and self.http_status >= 500)
            or self.service_code in TRANSIENT_SERVICE_CODES
            or self.error_type in {"APIConnectionError", "APITimeoutError", "TimeoutError"}
            or (self.error_type == "StreamErrorEvent" and self.service_code in {None, "OTHER", "UNKNOWN"})
        )


def user_input(*texts: str) -> dict:
    return {"type": "user_input", "content": [{"type": "text", "text": text} for text in texts]}


def fixture_result(call: Call) -> dict:
    result = {"find_slots": "ORBIT-17", "check_duration": "COMET-42"}[call.name]
    return {"type": "function_result", "call_id": call.id, "name": call.name, "result": result}


def service_code(value) -> str | None:
    if value is None:
        return None
    code = str(value).upper()
    return code if code in SERVICE_CODES or code.isdigit() and len(code) <= 3 else "OTHER"


async def stream_interaction(client, label: str, text_or_steps, *, model: str, **extra) -> Observation:
    observation = Observation()
    active_calls: dict[int, Call] = {}
    config = {
        "thinking_level": "low" if model == FLASH else "minimal",
        "max_output_tokens": 256,
        **extra.pop("generation_config", {}),
    }
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS), await client.interactions.create(
            model=model,
            input=text_or_steps,
            system_instruction=SYSTEM,
            generation_config=config,
            stream=True,
            automatic_continuation=False,
            timeout=TIMEOUT_SECONDS,
            **extra,
        ) as events:
            async for event in events:
                observation.event_types.add(event.event_type)
                if event.event_type == "error":
                    observation.error_type = "StreamErrorEvent"
                    observation.service_code = service_code(getattr(event.error, "code", None))
                elif event.event_type == "step.start":
                    step = event.step
                    observation.step_types.add(step.type)
                    if step.type == "function_call":
                        active_calls[event.index] = Call(step.id, step.name, step.arguments)
                elif event.event_type == "step.delta":
                    delta = event.delta
                    observation.delta_types.add(delta.type)
                    if delta.type == "text":
                        observation.text.append(delta.text)
                    elif delta.type == "arguments_delta" and delta.arguments:
                        active_calls[event.index].fragments.append(delta.arguments)
                        observation.argument_fragments += 1
                elif event.event_type == "step.stop" and event.index in active_calls:
                    call = active_calls.pop(event.index)
                    if call.fragments:
                        call.arguments = json.loads("".join(call.fragments))
                    observation.calls.append(call)
                elif event.event_type == "interaction.completed":
                    terminal = event.interaction
                    observation.id = terminal.id
                    observation.status = terminal.status if terminal.status in INTERACTION_STATUSES else "other"
                    observation.usage = {
                        name: value
                        for name in USAGE_FIELDS
                        if isinstance(value := getattr(terminal.usage, name, None), int)
                    }
    except Exception as error:
        observation.error_type = type(error).__name__
        status = getattr(error, "status_code", getattr(error, "code", None))
        if isinstance(status, int):
            observation.http_status = int(status)
        body = getattr(error, "body", None)
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            details = body["error"]
            code = details.get("code")
            observation.service_code = service_code(code if isinstance(code, str) else details.get("status"))

    # Only structural fields, counts, and recognized fixture names leave the probe.
    print(json.dumps({
        "probe": label,
        "model": model,
        "status": observation.status,
        "events": sorted(observation.event_types),
        "steps": sorted(observation.step_types),
        "deltas": sorted(observation.delta_types),
        "argument_fragments": observation.argument_fragments,
        "calls": [call.name if call.name in {"find_slots", "check_duration"} else "other"
                  for call in observation.calls],
        "usage": observation.usage,
        "error_type": observation.error_type,
        "service_code": observation.service_code,
        "http_status": observation.http_status,
    }), flush=True)
    return observation


async def probe(client, model: str) -> bool:
    async def run(label: str, text_or_steps, **settings) -> Observation:
        observed = await stream_interaction(client, label, text_or_steps, model=model, **settings)
        if observed.unavailable():
            raise ProbeUnavailable
        return observed

    checks: dict[str, bool] = {}
    base = await stream_interaction(
        client, "default_store", "The fixture code is ASTER-23. Reply only READY.", model=model,
    )
    if base.status != "completed" or not base.id:
        return False

    followups = [
        await run(
            f"branch_{direction.lower()}",
            [user_input("<turn_context>synthetic fixture</turn_context>",
                        f"Reply with {direction} then the fixture code from the previous turn.")],
            previous_interaction_id=base.id, store=True,
        )
        for direction in ("NORTH", "SOUTH")
    ]
    branch_ids = {branch.id for branch in followups if branch.id}
    checks["default_storage_and_two_branches"] = (
        len(branch_ids) == 2
        and all(branch.status == "completed" and branch.contains("ASTER-23")
                and branch.contains(direction) for branch, direction in zip(followups, ("NORTH", "SOUTH")))
    )

    pending = await run(
        "pending_tool_call",
        "Call find_slots with window exactly morning. Reply with the tool result after it arrives.",
        tools=TOOLS, store=True,
    )
    checks["streamed_function_arguments"] = (
        pending.error_type is None and pending.id is not None and pending.status in TOOL_TURN_STATUSES
        and pending.argument_fragments > 0
        and [(call.name, call.arguments) for call in pending.calls] == [("find_slots", {"window": "morning"})]
    )
    if checks["streamed_function_arguments"]:
        no_result = await run(
            "new_input_without_tool_result", [user_input("Reply only SKIPPED.")],
            previous_interaction_id=pending.id, tools=TOOLS, store=True,
        )
        result = "inconclusive"
        if no_result.status == "completed" and no_result.contains("SKIPPED"):
            result = "accepted"
        elif no_result.http_status == 400 or no_result.service_code == "INVALID_ARGUMENT":
            result = "rejected"
        checks["user_input_without_result_observed"] = result != "inconclusive"
        print(json.dumps({"check": "user_input_without_result", "result": result}), flush=True)

        combined = await run(
            "result_then_new_user_input",
            [fixture_result(pending.calls[0]),
             user_input("<turn_context>synthetic tool returned</turn_context>",
                        "Reply with the function result and the word TOGETHER. Do not call another tool.")],
            previous_interaction_id=pending.id, tools=TOOLS, store=True,
        )
        checks["result_then_new_user_input"] = (
            combined.status == "completed" and combined.contains("ORBIT-17") and combined.contains("TOGETHER")
        )

    parallel = await run(
        "parallel_tool_calls",
        "Call both find_slots and check_duration in parallel with window afternoon for each. "
        "Both calls are independent. Do not wait for the first result before calling the other. "
        "After receiving results, reply with both results and do not call any more tools.",
        tools=TOOLS, store=True,
    )
    checks["two_parallel_calls"] = (
        parallel.error_type is None and parallel.id is not None and parallel.status in TOOL_TURN_STATUSES
        and len(parallel.calls) == 2 and {call.name: call.arguments for call in parallel.calls}
        == {"find_slots": {"window": "afternoon"}, "check_duration": {"window": "afternoon"}}
    )
    if checks["two_parallel_calls"]:
        batch = await run(
            "parallel_tool_results", [fixture_result(call) for call in parallel.calls],
            previous_interaction_id=parallel.id, tools=TOOLS, store=True,
        )
        checks["parallel_tool_results"] = (
            batch.status == "completed" and batch.contains("ORBIT-17") and batch.contains("COMET-42")
        )

    short = await run(
        "output_token_limit", "Write 40 short lines about different kinds of clouds.",
        generation_config={"max_output_tokens": 1}, store=False,
    )
    checks["output_token_limit"] = short.status == "incomplete"
    checks["completed_usage"] = all(name in base.usage for name in ("total_input_tokens", "total_tokens"))

    print(json.dumps({"checks": checks}, sort_keys=True), flush=True)
    return all(checks.values())


async def legacy_diagnostic(client, model: str) -> bool:
    """Check whether the same key and model can use the streaming fallback."""
    result = {"probe": "legacy_streaming", "model": model, "finish_reason": None, "http_status": None}
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            chunks = await client.models.generate_content_stream(
                model=model, contents="Reply only READY.",
                config=types.GenerateContentConfig(
                    max_output_tokens=64,
                    thinking_config=types.ThinkingConfig(thinking_level="LOW" if model == FLASH else "MINIMAL"),
                ),
            )
            async for chunk in chunks:
                for candidate in chunk.candidates or []:
                    if candidate.finish_reason:
                        result["finish_reason"] = candidate.finish_reason.value
    except Exception as error:
        result["error_type"] = type(error).__name__
        status = getattr(error, "status_code", getattr(error, "code", None))
        if isinstance(status, int):
            result["http_status"] = int(status)
    print(json.dumps(result), flush=True)
    return result["finish_reason"] == "STOP"


async def probe_incomplete(client, model: str) -> bool:
    marker = "AMBER-COMET-728"
    seed = await stream_interaction(
        client, "capped_stored_reply",
        [user_input(f"Remember {marker} as my code. Reply with {marker} first, then list 100 numbered planets from fiction.")],
        model=model, store=True, tools=[], generation_config={"max_output_tokens": 48},
    )
    if seed.status != "incomplete" or not seed.id or not "".join(seed.text).strip():
        print(json.dumps({"capped_reply_continued": None, "capped_reply_has_text": bool(seed.text)}), flush=True)
        return False
    await asyncio.sleep(10)
    reply = await stream_interaction(
        client, "continue_capped_stored_reply",
        [user_input("What code did I ask you to remember? Reply with the code alone.")],
        model=model, store=True, tools=[], previous_interaction_id=seed.id,
    )
    retained = reply.status == "completed" and reply.contains(marker)
    print(json.dumps({"capped_reply_continued": retained, "capped_reply_has_text": True}), flush=True)
    return retained


async def run_probe(key: str, model: str, use_legacy: bool, *, incomplete: bool = False) -> bool:
    async with genai.Client(
        api_key=key,
        vertexai=False,
        http_options=types.HttpOptions(
            timeout=TIMEOUT_SECONDS * 1000,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    ).aio as client:
        run = probe_incomplete if incomplete else legacy_diagnostic if use_legacy else probe
        return await run(client, model)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    backup = "gemini-3.1-flash-lite"
    parser.add_argument("--model", choices=(FLASH, FLASH_LITE, backup), help="Override GEMINI_MODEL.")
    diagnostics = parser.add_mutually_exclusive_group()
    diagnostics.add_argument("--legacy-diagnostic", action="store_true", help="Only check legacy streaming.")
    diagnostics.add_argument("--incomplete", action="store_true", help="Check continuation after an intentionally capped reply.")
    arguments = parser.parse_args()
    logging.disable(logging.CRITICAL)
    warnings.simplefilter("ignore")
    try:
        settings = load_local_environment()
        key = settings.get("GEMINI_API_KEY")
        if not key:
            print("Set GEMINI_API_KEY for an unbilled AI Studio project in .env.local.")
            return 1
        model = arguments.model or settings.get("GEMINI_MODEL") or FLASH_LITE
        if model not in (FLASH, FLASH_LITE, backup):
            print("Choose a planned Flash model.")
            return 1
        print(json.dumps({"google_genai_version": genai.__version__}), flush=True)
        return 0 if asyncio.run(run_probe(key, model, arguments.legacy_diagnostic, incomplete=arguments.incomplete)) else 1
    except ProbeUnavailable:
        return 1
    except Exception as error:
        print(json.dumps({"probe_initialization_error": type(error).__name__}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
