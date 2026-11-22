"""Measure the same ten chat turns over the local Next.js proxy with a read-only calendar."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import aclosing
from contextvars import ContextVar
from datetime import date, datetime
import json
from pathlib import Path
import re
from secrets import token_hex
from statistics import median, quantiles
from time import perf_counter, sleep
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from backend.cal_client import CalError
from backend.gemini import GeminiClient
from backend.model import ModelCompletion, ToolCall
from backend.protocol import ChatRequest
from backend.server import TurnLoop, create_app
from backend.turn import TurnService
from evals.fake_cal import FIXTURE_PATH, FakeCal
from scripts.local_environment import load_local_environment


MODEL = "gemini-3.5-flash-lite"
THINKING = "minimal"
ZONE = "Asia/Singapore"
FIXTURE_CLOCK = datetime.fromisoformat("2026-11-22T21:00:00+08:00")
PACE_SECONDS = 10
QUOTA_BACKOFF_SECONDS = 65
FIXTURE_HEADER = "X-LiveX-Chat-Fixture"
FIXTURE_ID = "m5b-closed-thu-afternoon-v1"
PROMPTS = (
    "What can you help me schedule?",
    "Can I grab thirty minutes Thursday afternoon?",
    "What about Friday morning?",
    "How long is the meeting?",
    "My fictional name is Nova Fixture. Call me Nova.",
    "What name did I ask you to use?",
    "Can you show me some times next week?",
    "Does any day have a morning option?",
    "Which time zone are you showing?",
    "Have I confirmed a booking in this conversation?",
)
_REF = re.compile(r"[SB][1-9][0-9]{0,23}")
_MARKER = re.compile(r"\[\[([SB][1-9][0-9]{0,23})\]\]")
_TOOL_FIELDS = {
    "find_slots": ("from_date", "to_date", "part_of_day", "for_booking_ref", "event_type"),
    "list_bookings": ("email", "which"),
    "propose_booking": ("slot_ref", "name", "email", "notes"),
    "propose_cancel": ("booking_ref", "reason"),
    "propose_reschedule": ("booking_ref", "slot_ref", "reason"),
}
_TRACE: ContextVar[list[dict] | None] = ContextVar("live_chat_driver_trace", default=None)
_ERROR_NAMES = frozenset({
    "rate_limited", "model_down", "timeout", "cal_down", "interaction_expired", "invalid_request", "empty",
    "loop_limit", "refusal", "stream_error", "session_limit", "slot_taken",
})


def safe_argument(name: str, value: object) -> str:
    if not isinstance(value, str):
        return "[invalid type]"
    if value == "":
        return value
    if name.endswith("_ref") and _REF.fullmatch(value):
        return value
    if name in {"from_date", "to_date"} and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            date.fromisoformat(value)
        except ValueError:
            return "[invalid date]"
        return value
    if value in {
        "morning", "afternoon", "evening", "any", "upcoming", "past", "30-minute-meeting", "Nova", "Nova Fixture",
    }:
        return value
    return "[redacted]"


def safe_tool_call(call: ToolCall) -> dict:
    fields = _TOOL_FIELDS.get(call.name, ())
    arguments = {name: safe_argument(name, call.arguments[name]) for name in fields if name in call.arguments}
    return {
        "name": call.name if fields else "unknown",
        "arguments": arguments,
        "omittedArgumentCount": len(call.arguments) - len(arguments),
    }


class TracingModel:
    def __init__(self, client: GeminiClient) -> None:
        self.client = client

    async def stream(self, **request):
        async with aclosing(self.client.stream(**request)) as events:
            async for event in events:
                trace = _TRACE.get()
                if trace is not None and isinstance(event, ModelCompletion):
                    trace.append({"calls": [safe_tool_call(call) for call in event.tool_calls]})
                yield event


class TracingService(TurnService):
    async def events(self, request: ChatRequest):
        trace: list[dict] = []
        reset = _TRACE.set(trace)
        try:
            async with aclosing(super().events(request)) as events:
                async for event in events:
                    if event["type"] == "turn_end":
                        event = {**event, "metrics": {**event["metrics"], "driverToolTrace": trace}}
                    yield event
        finally:
            _TRACE.reset(reset)


class ReadOnlyCal(FakeCal):
    def _deny_write(self, *args, **kwargs):
        raise CalError("invalid", "The measurement calendar is read-only.", status=403)

    create_booking = cancel_booking = reschedule_booking = _deny_write


def fixture_calendar() -> ReadOnlyCal:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    starts = fixture["availability"]["local_starts_by_date"]
    # Keep the fixture's published openings except Thursday 26 November from 12:00 to 17:00 local.
    open_starts = [
        f"{day}T{time}" for day, times in starts.items() for time in times
        if not (day == "2026-11-26" and "12:00" <= time < "17:00")
    ]
    return ReadOnlyCal(clock=lambda: FIXTURE_CLOCK, fixture_overrides={"open_local_starts": open_starts}, time_zone=ZONE)


def fixture_app(service: TracingService, loop: TurnLoop):
    app = create_app(service, turn_loop=loop)

    @app.after_request
    def mark_fixture(response):
        response.headers[FIXTURE_HEADER] = FIXTURE_ID
        return response

    return app


def serve(port: int) -> int:
    key = load_local_environment().get("GEMINI_API_KEY")
    if not key:
        print("Set the unbilled Gemini API key in .env.local.", flush=True)
        return 1
    adapter = GeminiClient(key, model=MODEL, thinking_level=THINKING, persistent=True)
    service = TracingService(
        model=TracingModel(adapter), calendar=fixture_calendar(), secret=token_hex(32), clock=lambda: FIXTURE_CLOCK,
    )
    loop = TurnLoop(adapter.aclose)
    try:
        fixture_app(service, loop).run(host="127.0.0.1", port=port, use_reloader=False)
    finally:
        try:
            loop.close()
        finally:
            service.close_prefetch()
    return 0


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def loopback_origin(value: str) -> str:
    url = urlsplit(value)
    if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.username is not None
            or url.path not in {"", "/"} or url.query or url.fragment):
        raise ValueError("Use an explicit http://127.0.0.1:<port> origin without a path.")
    return f"http://127.0.0.1:{url.port}"


def _open_local(request: Request):
    return build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=120)


def _elapsed(started: float) -> float:
    return round((perf_counter() - started) * 1_000, 2)


def warm(origin: str) -> dict:
    started = perf_counter()
    query = urlencode({"timeZone": ZONE})
    try:
        with _open_local(Request(f"{origin}/api/warm?{query}", headers={"Accept": "application/json"})) as response:
            metadata = json.load(response)
            fixture = response.headers.get(FIXTURE_HEADER) == FIXTURE_ID
            return {"type": "warm", "ok": fixture and any(event.get("id") == 9001 for event in metadata["eventTypes"]),
                    "httpStatus": response.status, "elapsedMs": _elapsed(started)}
    except HTTPError as error:
        error.close()
        return {"type": "warm", "ok": False, "httpStatus": error.code, "elapsedMs": _elapsed(started)}
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {"type": "warm", "ok": False, "error": type(error).__name__, "elapsedMs": _elapsed(started)}


def observe_turn(origin: str, prompt: str, state_token: str | None, session_slots: set[str]) -> tuple[dict, str | None]:
    started = perf_counter()
    request = Request(f"{origin}/api/chat", method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/x-ndjson",
    }, data=json.dumps({
        "stateToken": state_token, "timeZone": ZONE, "input": {"kind": "text", "text": prompt},
    }).encode("utf-8"))
    row: dict = {"prompt": prompt, "continuedSession": state_token is not None, "outcome": "failed"}
    next_token = state_token
    known_slots = set(session_slots)
    offered: set[str] = set()
    seen_refs: set[str] = set()
    partial = ""
    try:
        with _open_local(request) as response:
            row["httpStatus"] = response.status
            if response.headers.get(FIXTURE_HEADER) != FIXTURE_ID:
                raise ValueError("The chat response did not come from the read-only fixture.")
            for line in response:
                received = _elapsed(started)
                if not line.strip():
                    continue
                event = json.loads(line)
                kind = event["type"]
                if kind == "status":
                    row.setdefault("firstStatusMs", received)
                elif kind == "refs":
                    known_slots.update(ref for ref in event.get("slots", {}) if _REF.fullmatch(ref))
                elif kind in {"text_delta", "assistant_text"} and event["text"].strip():
                    row.setdefault("firstTextMs", received)
                    row.setdefault("firstUsefulMs", received)
                    partial += event["text"]
                    seen_refs.update(_MARKER.findall(partial))
                    # Keep only the suffix that can end in a reference split across later chunks.
                    partial = partial[-30:]
                elif kind == "slot_chips":
                    seen_refs.update(ref for ref in event["refs"] if _REF.fullmatch(ref))
                elif kind == "proposal":
                    proposal_kind = event["proposal"]["kind"]
                    row["proposalKind"] = proposal_kind if proposal_kind in {"book", "cancel", "reschedule"} else "unknown"
                elif kind == "error":
                    row["error"] = event["code"] if event["code"] in _ERROR_NAMES else "unknown"
                elif kind == "turn_end":
                    next_token = event["stateToken"]
                    metrics = event["metrics"]
                    row.update(
                        outcome=metrics["outcome"], serverMs=metrics["totalMs"], prescan=metrics["prescan"],
                        prefetch=metrics["prefetch"], modelCalls=metrics["modelCalls"],
                        toolCalls=[{**call, "name": call["name"] if call["name"] in _TOOL_FIELDS else "unknown"}
                                   for call in metrics["toolCalls"]],
                        toolTrace=metrics["driverToolTrace"], calCalls=metrics["calCalls"],
                        unknownRefs=metrics["unknownRefs"],
                    )
                offered.update(known_slots & seen_refs)
                if offered:
                    row.setdefault("firstChipsMs", received)
                    row.setdefault("firstUsefulMs", received)
            if "serverMs" not in row:
                row.setdefault("error", "incomplete_stream")
            elif row.get("error") == "interaction_expired":
                session_slots.clear()
            elif row["outcome"] == "ok":
                session_slots.update(known_slots)
    except HTTPError as error:
        row["httpStatus"] = error.code
        row["error"] = "rate_limited" if error.code == 429 else "http_error"
        error.close()
    except (OSError, ValueError, KeyError, TypeError) as error:
        row["error"] = type(error).__name__
        row["outcome"] = "failed"
        next_token = state_token
    row["offeredSlotCount"] = len(offered)
    row["elapsedMs"] = _elapsed(started)
    return row, next_token


def _percentiles(values: list[float]) -> dict | None:
    if not values:
        return None
    return {
        "p50": round(median(values), 2),
        "p90": round(quantiles(values, n=10, method="inclusive")[8] if len(values) > 1 else values[0], 2),
    }


def run(origin: str, label: str, report_path: Path) -> int:
    with report_path.open("x", encoding="utf-8") as report:
        def emit(item: dict) -> None:
            print(json.dumps(item, ensure_ascii=True, separators=(",", ":")), file=report, flush=True)

        emit({
            "type": "run", "label": label, "origin": origin, "model": MODEL, "thinking": THINKING,
            "clock": FIXTURE_CLOCK.isoformat(), "zone": ZONE, "fixture": FIXTURE_ID,
            "paceSeconds": PACE_SECONDS, "quotaBackoffSeconds": QUOTA_BACKOFF_SECONDS,
            "firstUsefulDefinition": "Earlier of the first nonempty chat text event and the first displayed chip with a known slot ref.",
        })
        warmed = warm(origin)
        emit(warmed)
        if not warmed["ok"]:
            print("Warm-up did not reach the read-only fixture. See the report.", flush=True)
            return 1

        rows = []
        token = None
        session_slots: set[str] = set()
        for number, prompt in enumerate(PROMPTS, start=1):
            if rows:
                sleep(QUOTA_BACKOFF_SECONDS if rows[-1].get("error") == "rate_limited" else PACE_SECONDS)
            row, token = observe_turn(origin, prompt, token, session_slots)
            rows.append(row)
            emit({"type": "turn", "number": number, **row})
            print(f"{label} {number}/{len(PROMPTS)} {row['outcome']} ({row['elapsedMs']:.0f} ms)", flush=True)

        accepted = [row for row in rows if row["outcome"] == "ok"]
        timings = ("firstStatusMs", "firstTextMs", "firstChipsMs", "firstUsefulMs", "elapsedMs", "serverMs")
        emit({
            "type": "summary", "label": label, "attempts": len(rows), "successful": len(accepted),
            "failed": dict(Counter(row.get("error", "unknown") for row in rows if row["outcome"] != "ok")),
            "oneModelCallTurns": sum(len(row.get("modelCalls", [])) == 1 for row in accepted),
            "timings": {name: _percentiles([row[name] for row in accepted if name in row]) for name in timings},
        })
        return 0 if len(accepted) == len(PROMPTS) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve", help="Run the live model with a local read-only calendar.")
    server.add_argument("--port", type=int, default=5000)
    runner = commands.add_parser("run", help="Drive the local Next.js warm and chat endpoints once.")
    runner.add_argument("--origin", default="http://127.0.0.1:3000")
    runner.add_argument("--label", required=True, choices=("before", "after"))
    runner.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "serve":
            if not 1 <= args.port <= 65535:
                parser.error("Port must be between 1 and 65535.")
            return serve(args.port)
        return run(loopback_origin(args.origin), args.label, args.report)
    except (OSError, ValueError) as error:
        print(f"Live chat driver could not start ({type(error).__name__}).", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
