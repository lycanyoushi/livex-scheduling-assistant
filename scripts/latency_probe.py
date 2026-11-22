"""Measure serial Gemini Interactions calls on an unbilled test project."""

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
from secrets import token_hex
from statistics import median, quantiles
from tempfile import NamedTemporaryFile
from time import monotonic, perf_counter, sleep
import warnings

from google import genai
from google.genai import types
from google.genai._interactions.core.error import APIError, APITimeoutError

from backend.state import new_state
from backend.tools import CalendarTools
from evals.fake_cal import DEFAULT_CLOCK, FakeCal
from scripts.local_environment import load_local_environment
from scripts.model_probe import INTERACTION_STATUSES, USAGE_FIELDS, service_code, user_input


DEFAULT_MODEL = "gemini-3.5-flash-lite"
THINKING = {DEFAULT_MODEL: "minimal", "gemini-3.1-flash-lite": "minimal", "gemini-3.8-flash": "low"}
ATTEMPT_CAP = 55
PACE_SECONDS = 10
QUOTA_BACKOFF_SECONDS = 65
REQUEST_TIMEOUT_SECONDS = 20
SYSTEM = "This is a synthetic scheduling benchmark. Follow the output format. Never call a function."
PROMPT = (
    "For the Thursday afternoon scheduling benchmark, return the fixture marker from the prior turn. "
    "If there is no prior marker, return READY. Output the marker only."
)


@dataclass(frozen=True)
class Configuration:
    name: str
    model: str = DEFAULT_MODEL
    full_tools: bool = True
    stored: bool = True
    persistent: bool = False


CONFIGURATIONS = (
    Configuration("baseline"),
    Configuration("no_tools", full_tools=False),
    Configuration("fresh", stored=False),
    Configuration("persistent", persistent=True),
    Configuration("flash_3_1", model="gemini-3.1-flash-lite"),
    Configuration("flash_3_8", model="gemini-3.8-flash"),
)


def emit(report, data):
    line = json.dumps(data, sort_keys=True)
    report.write(line + "\n")
    report.flush()
    print(line, flush=True)


class Budget:
    def __init__(self):
        self.attempts = 0
        self.next_start = 0.0
        self.outage_streak = Counter()
        self.quota_streak = Counter()
        self.blocked = {}

    def wait(self, model):
        if model in self.blocked or self.attempts >= ATTEMPT_CAP:
            return False
        sleep(max(0, self.next_start - monotonic()))
        self.attempts += 1
        self.next_start = monotonic() + PACE_SECONDS
        return True

    def record(self, model, row):
        error = row.get("error")
        http_status, code = row.get("http_status"), row.get("service_code")
        if http_status == 429 or code in {"RESOURCE_EXHAUSTED", "429"}:
            self.next_start = max(self.next_start, monotonic() + QUOTA_BACKOFF_SECONDS)
            self.quota_streak[model] += 1
            if self.quota_streak[model] >= 2:
                self.blocked[model] = "repeated_quota_error"
            return
        self.quota_streak[model] = 0
        early_outage = row.get("ttft_ms") is None and (
            (isinstance(http_status, int) and http_status >= 500)
            or code in {"UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED"}
            or error in {"timeout", "connection", "stream_error", "missing_completion"}
        )
        self.outage_streak[model] = self.outage_streak[model] + 1 if early_outage else 0
        if self.outage_streak[model] >= 2:
            self.blocked[model] = "repeated_early_provider_failure"


def elapsed_ms(start):
    return (perf_counter() - start) * 1000


@contextmanager
def sdk_client(key):
    loop_started = perf_counter()
    with asyncio.Runner() as runner:
        runner.get_loop()
        loop_ms = elapsed_ms(loop_started)

        async def connect():
            client = genai.Client(
                api_key=key, vertexai=False,
                http_options=types.HttpOptions(
                    timeout=REQUEST_TIMEOUT_SECONDS * 1000,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            ).aio
            return client, client.interactions

        sdk_started = perf_counter()
        client, interactions = runner.run(connect())
        setup = {"loop_setup_ms": loop_ms, "sdk_setup_ms": elapsed_ms(sdk_started)}
        try:
            yield runner, interactions, setup
        finally:
            runner.run(client.aclose())


async def measure(interactions, configuration, tools, prompt, expected, previous_id, wall_started, prebuilt_ms):
    output_steps = set()
    text = []
    interaction_id = None
    data = {"ttft_ms": None, "wall_ttft_ms": None, "request_ms": None, "ok": False}
    started = perf_counter()
    request = {"previous_interaction_id": previous_id} if previous_id else {}
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS), await interactions.create(
            model=configuration.model, input=[user_input(prompt)], tools=tools,
            system_instruction=SYSTEM, generation_config={"thinking_level": THINKING[configuration.model]},
            store=True, stream=True, automatic_continuation=False,
            timeout=REQUEST_TIMEOUT_SECONDS, **request,
        ) as stream:
            async for event in stream:
                if event.event_type == "step.start" and event.step.type == "model_output":
                    output_steps.add(event.index)
                elif event.event_type == "step.start" and event.step.type == "function_call":
                    data["error"] = "unexpected_function_call"
                    break
                elif event.event_type == "step.stop":
                    output_steps.discard(event.index)
                elif (
                    event.event_type == "step.delta" and event.index in output_steps
                    and event.delta.type == "text" and event.delta.text
                ):
                    if data["ttft_ms"] is None:
                        data["ttft_ms"] = elapsed_ms(started)
                        data["wall_ttft_ms"] = elapsed_ms(wall_started) + prebuilt_ms
                    text.append(event.delta.text)
                elif event.event_type == "error":
                    data["error"] = "stream_error"
                    data["service_code"] = service_code(getattr(event.error, "code", None))
                    break
                elif event.event_type == "interaction.completed":
                    terminal = event.interaction
                    interaction_id = terminal.id
                    data["status"] = terminal.status if terminal.status in INTERACTION_STATUSES else "other"
                    data["usage"] = {
                        name: value for name in USAGE_FIELDS
                        if type(value := getattr(terminal.usage, name, None)) is int and value >= 0
                    }
                    returned_model = getattr(terminal, "model", None)
                    data["model_matches"] = returned_model in {None, configuration.model, f"models/{configuration.model}"}
                    data["request_ms"] = elapsed_ms(started)
                    break
    except (TimeoutError, APITimeoutError):
        data["error"] = "timeout"
    except APIError as error:
        data["error"] = "connection" if error.status_code is None else "http_error"
        data["http_status"] = error.status_code
        body = error.body.get("error", error.body) if isinstance(error.body, dict) else {}
        if isinstance(body, dict):
            code = body.get("code")
            data["service_code"] = service_code(code if isinstance(code, str) else body.get("status"))
    except Exception as error:
        data["error"] = type(error).__name__

    if data["request_ms"] is None:
        data["request_ms"] = elapsed_ms(started)
    if "error" not in data:
        if not interaction_id or not data.get("status"):
            data["error"] = "missing_completion"
        elif data["status"] != "completed" or not data["model_matches"]:
            data["error"] = "unexpected_completion"
        elif "".join(text).strip().strip("`\"'. ") != expected:
            data["error"] = "marker_mismatch"
        else:
            data["ok"] = True
    return data, interaction_id


def attempted_row(report, budget, configuration, tools, prompt, expected, previous_id, sample, key, shared=None):
    if not budget.wait(configuration.model):
        return None
    started = perf_counter()
    try:
        if shared is None:
            with sdk_client(key) as (runner, interactions, setup):
                data, interaction_id = runner.run(measure(
                    interactions, configuration, tools, prompt, expected, previous_id, started, 0,
                ))
        else:
            runner, interactions, initial_setup = shared
            setup = initial_setup if sample == 1 else {"loop_setup_ms": 0, "sdk_setup_ms": 0}
            data, interaction_id = runner.run(measure(
                interactions, configuration, tools, prompt, expected, previous_id, started, sum(setup.values()),
            ))
        wall_ms = elapsed_ms(started) + (sum(setup.values()) if shared is not None else 0)
        data.update(setup, wall_ms=wall_ms)
    except Exception as error:
        data, interaction_id = {"ok": False, "error": type(error).__name__, "wall_ms": elapsed_ms(started)}, None
    data.update({"type": "sample", "configuration": configuration.name, "sample": sample,
                 "model": configuration.model, "attempt": budget.attempts, "stored": bool(previous_id)})
    budget.record(configuration.model, data)
    emit(report, data)
    return data, interaction_id


def percentiles(values):
    if not values:
        return None
    return {"p50": round(median(values), 2),
            "p90": round(quantiles(values, n=10, method="inclusive")[8] if len(values) > 1 else values[0], 2)}


def summary(configuration, rows, stop=None, *, suffix=""):
    successful = [row for row in rows if row["ok"]]
    return {
        "type": "summary", "configuration": configuration.name + suffix, "model": configuration.model,
        "attempts": len(rows), "successful": len(successful), "stop": stop,
        "failures": dict(Counter(
            str(row.get("http_status") or row.get("service_code") or row.get("error"))
            for row in rows if not row["ok"]
        )),
        "timings": {name: percentiles([row[name] for row in successful if row.get(name) is not None])
                    for name in ("ttft_ms", "wall_ttft_ms", "request_ms", "wall_ms", "loop_setup_ms", "sdk_setup_ms")},
        "tokens": {name: percentiles([row["usage"][name] for row in successful if name in row.get("usage", {})])
                   for name in USAGE_FIELDS},
    }


def run(report, key, tools, sample_count):
    marker = "HISTORY-" + token_hex(4).upper()
    seed_prompt = f"Remember the synthetic fixture marker {marker} for the next turn. Reply only READY now."
    budget = Budget()
    parents = {}
    baseline_successful = False
    for configuration in CONFIGURATIONS:
        model = configuration.model
        if configuration.stored and model not in parents:
            seed = Configuration("history_seed", model=model)
            for retry in (1, 2):
                result = attempted_row(report, budget, seed, tools, seed_prompt, "READY", None, retry, key)
                if result is None:
                    break
                row, interaction_id = result
                if row["ok"]:
                    parents[model] = interaction_id
                    break
            if model not in parents:
                budget.blocked.setdefault(model, "history_seed_failed")

        rows = []
        previous_id = parents.get(model) if configuration.stored else None
        target = marker if configuration.stored else "READY"
        schema = tools if configuration.full_tools else []

        def collect(shared=None):
            for sample in range(1, sample_count + 1):
                result = attempted_row(report, budget, configuration, schema, PROMPT, target, previous_id, sample, key, shared)
                if result is None:
                    break
                rows.append(result[0])

        if configuration.persistent and model not in budget.blocked and budget.attempts < ATTEMPT_CAP:
            with sdk_client(key) as shared:
                collect(shared)
        else:
            collect()
        stop = budget.blocked.get(model) or ("attempt_cap" if budget.attempts >= ATTEMPT_CAP else None)
        emit(report, summary(configuration, rows, stop))
        if configuration.name == "baseline":
            baseline_successful = any(row["ok"] for row in rows)
        if configuration.persistent:
            emit(report, summary(configuration, rows[1:], stop, suffix="_after_first"))
    emit(report, {"type": "finished", "attempts": budget.attempts, "blocked_models": budget.blocked})
    return baseline_successful


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, choices=range(1, 10), default=9, metavar="1–9")
    parser.add_argument("--describe", action="store_true", help="Show the matrix and offline tool count.")
    arguments = parser.parse_args()
    logging.disable(logging.CRITICAL)
    warnings.simplefilter("ignore")
    tools = CalendarTools(FakeCal(), new_state("Asia/Singapore", DEFAULT_CLOCK), DEFAULT_CLOCK).schemas()
    matrix = [{"name": item.name, "model": item.model, "thinking": THINKING[item.model],
               "tools": len(tools) if item.full_tools else 0, "stored": item.stored,
               "persistent": item.persistent} for item in CONFIGURATIONS]
    metadata = {"type": "run", "sdk": genai.__version__, "samples_per_configuration": arguments.samples,
                "pace_seconds": PACE_SECONDS, "quota_backoff_seconds": QUOTA_BACKOFF_SECONDS,
                "attempt_cap": ATTEMPT_CAP, "deadline_seconds": REQUEST_TIMEOUT_SECONDS, "matrix": matrix,
                "started_utc": datetime.now(UTC).isoformat()}
    if arguments.describe:
        print(json.dumps(metadata, sort_keys=True))
        return 0
    try:
        key = load_local_environment().get("GEMINI_API_KEY")
        if not key:
            print("Set an unbilled Gemini project key in .env.local.", flush=True)
            return 1
        with NamedTemporaryFile(mode="w", encoding="utf-8", prefix="livex-latency-", suffix=".jsonl", delete=False) as report:
            print(json.dumps({"type": "report", "path": report.name}), flush=True)
            emit(report, metadata)
            return 0 if run(report, key, tools, arguments.samples) else 1
    except Exception as error:
        print(json.dumps({"type": "probe_error", "error": type(error).__name__}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
