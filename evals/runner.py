"""Run the approved scheduling conversations on isolated FakeCal calendars."""

import argparse
import asyncio
from collections import Counter
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
from secrets import token_hex
import shutil
from statistics import median, quantiles
from subprocess import run as subprocess_run
from time import monotonic, perf_counter
from zoneinfo import ZoneInfo

from backend.cal_client import CalError
from backend.gemini import GeminiClient
from backend.model import ModelCompletion, ModelError, ModelText
from backend.server import TurnLoop, create_app
from backend.state import GuestDraft, SlotEntry, new_state, sign_state, verify_state
from backend.tools import booking_entry
from backend.turn import TurnService
from evals.checker import (
    EMAIL_ADDRESS, EvalError, SAFE_STATUS, TYPED_CONFIRM_REPLIES, TYPED_DISMISS_REPLIES, check_turn,
    confirms_signed_proposal, local_iso, render, require, safe_reason,
)
from evals.fake_cal import FakeCal, _slot_edge
from scripts.local_environment import load_local_environment


SPEC_PATH = Path(__file__).with_name("conversations.yaml")
HELD_OUT_PATH = SPEC_PATH.with_name("owner_held_out.json")
HELD_OUT_SPECS = {
    "held_out": (HELD_OUT_PATH, range(101, 107)),
    "scheduling_held_out": (SPEC_PATH.with_name("scheduling_held_out.json"), range(201, 221)),
    "scheduling_follow_up": (SPEC_PATH.with_name("scheduling_follow_up.json"), range(301, 312)),
}
FOLLOW_UP_FIXTURE = "fake_cal_scheduling_follow_up.json"
APPROVED_CASE_IDS = range(1, 32)
REGRESSION_SPECS = {
    "regression_spec_sha256": (SPEC_PATH.with_name("regressions.yaml"), (32,)),
    "typed_confirmation_variants_sha256": (SPEC_PATH.with_name("typed_confirmation_variants.yaml"), (33, 34)),
    "schedule_listing_sha256": (SPEC_PATH.with_name("schedule_listing.yaml"), (35,)),
}
REGRESSION_CASE_IDS = frozenset(case_id for _, case_ids in REGRESSION_SPECS.values() for case_id in case_ids)
DEFAULT_MODEL = "gemini-3.5-flash-lite"
BACKUP_MODEL = "gemini-3.1-flash-lite"
COMPARISON_MODEL = BACKUP_MODEL
APPROVED_SPEC_COMPARISON_MODEL = "gemini-3.8-flash"
PACE_SECONDS = 10
QUOTA_BACKOFF_SECONDS = 65
DEFAULT_CALL_CAP = 180
SUPPORTED_CHECKS = {
    "tool_calls", "forbidden_tools", "refs_within", "proposal_kind", "max_model_calls", "min_model_calls",
    "prefetch_hit", "proposal_email", "reply_patterns", "pending_tool_call", "proposal_differs_from_turn",
    "first_model_step", "cal_reads", "cal_writes", "booking_result", "rejected_forbidden_tools",
    "reply_language", "template", "forbidden_cal_calls", "reply_max_lines", "attempted_model_roles", "fallback",
    "booking_cards", "retired_booking_refs", "picked_slot_from_turn", "proposal_name", "proposal_booking_from_turn",
    "proposal_fixture_booking",
    "guest_draft_email", "continue_previous_interaction", "reply_forbidden_patterns", "calendar_availability",
}
SUPPORTED_TEMPLATES = {None, "booked", "cancelled", "rescheduled", "slot_taken", "model_down_slots"}


def read_yaml(yaml_python=None, *, path=SPEC_PATH):
    parser = yaml_python or shutil.which("python")
    require(parser is not None, "Use --yaml-python with a Python installation that has PyYAML.")
    result = subprocess_run(
        [parser, "-c", "from pathlib import Path; import json,sys,yaml; "
         "print(json.dumps(yaml.safe_load(Path(sys.argv[1]).read_text(encoding='utf-8-sig'))))", str(path)],
        capture_output=True, check=False, encoding="utf-8", timeout=15,
    )
    require(result.returncode == 0, "The YAML parser failed. Supply --yaml-python with an existing PyYAML installation.")
    return json.loads(result.stdout)


def validate_spec(spec):
    require(spec["schema_version"] == 1 and spec["calendar_fixture"] == "fake_cal.json", "Unexpected evaluation schema.")
    require(spec["runs"]["default"]["model"] == DEFAULT_MODEL
            and spec["runs"]["comparison"]["model"] == APPROVED_SPEC_COMPARISON_MODEL,
            "Unexpected evaluated model.")
    require(sorted(case["id"] for case in spec["cases"]) == list(APPROVED_CASE_IDS), "Missing or duplicate approved cases.")
    validate_case_checks(spec["cases"])


def validate_case_checks(cases):
    for case in cases:
        for number, turn in enumerate(case["turns"], 1):
            if "proposal_from_turn" in turn:
                source = turn["proposal_from_turn"]
                reply = turn.get("user")
                require(isinstance(reply, str) and reply in TYPED_CONFIRM_REPLIES | TYPED_DISMISS_REPLIES and "action" not in turn
                        and type(source) is int and 1 <= source < number, "Invalid typed confirmation fixture.")
            rules = turn["checks"]
            require(set(rules) <= SUPPORTED_CHECKS and rules.get("reply_language") in {None, "es"},
                    "Unhandled evaluation check.")
            require(rules.get("template") in SUPPORTED_TEMPLATES, "Unhandled evaluation template.")
            for expected in rules.get("calendar_availability", []):
                require(set(expected) == {"local_start", "status"}
                        and expected["status"] in {"available", "unavailable"}
                        and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", expected["local_start"]),
                        "Malformed calendar availability assertion.")
                datetime.fromisoformat(expected["local_start"])
            for pattern in (*rules.get("reply_patterns", []), *rules.get("reply_forbidden_patterns", [])):
                re.compile(pattern)


def load_snapshot(path):
    snapshot = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    require(snapshot["yaml_sha256"] == sha256(path.read_bytes()).hexdigest(),
            "The generated spec is stale. Regenerate it with --refresh-spec.")
    return snapshot["spec"]


def approved_spec():
    spec = load_snapshot(SPEC_PATH)
    validate_spec(spec)
    return spec


def held_out_cases(spec, *, suite="held_out"):
    path, ids = HELD_OUT_SPECS[suite]
    frozen = json.loads(path.read_text(encoding="utf-8"))
    require(all(frozen[name] == spec[name] for name in (
        "schema_version", "default_clock", "default_time_zone",
    )), "Held-out fixture settings must match the approved suite.")
    require(frozen["calendar_fixture"] == suite_fixture_path(spec, suite).name, "Unexpected held-out calendar fixture.")
    cases = frozen["cases"]
    require([case["id"] for case in cases] == list(ids), "Missing or duplicate held-out cases.")
    if suite == "held_out":
        require(all(case["rephrases"] in APPROVED_CASE_IDS for case in cases), "Unknown approved case rephrased.")
    validate_case_checks(cases)
    return cases


def suite_fixture_path(spec, suite):
    name = FOLLOW_UP_FIXTURE if suite == "scheduling_follow_up" else spec["calendar_fixture"]
    return SPEC_PATH.with_name(name)


def validate_regressions(regressions, spec, case_ids):
    require(all(regressions[name] == spec[name] for name in (
        "schema_version", "calendar_fixture", "default_clock", "default_time_zone",
    )), "Regression fixture settings must match the approved suite.")
    cases = regressions["cases"]
    require(tuple(case["id"] for case in cases) == case_ids, "Missing or duplicate regression case.")
    validate_case_checks(cases)
    return cases


def regression_cases(spec, selected=REGRESSION_CASE_IDS):
    cases = []
    for path, case_ids in REGRESSION_SPECS.values():
        if selected.intersection(case_ids):
            cases.extend(validate_regressions(load_snapshot(path), spec, case_ids))
    return cases


def regression_hashes(case_ids):
    selected = set(case_ids)
    return {field: sha256(path.read_bytes()).hexdigest() for field, (path, ids) in REGRESSION_SPECS.items()
            if selected.intersection(ids)}


def model_profile(spec, model, held_out=False):
    if model == DEFAULT_MODEL:
        profile = spec["runs"]["default"]
        if held_out:
            return {**profile, "repetitions_per_case": 1, "minimum_passing_repetitions": 1}
        return profile
    return {**spec["runs"]["comparison"], "model": COMPARISON_MODEL, "thinking_level": "minimal"}


def refresh_spec(yaml_python=None):
    spec = read_yaml(yaml_python)
    validate_spec(spec)
    sources = {SPEC_PATH: spec}
    for path, case_ids in REGRESSION_SPECS.values():
        regressions = read_yaml(yaml_python, path=path)
        validate_regressions(regressions, spec, case_ids)
        sources[path] = regressions
    for path, source in sources.items():
        checkpoint(path.with_suffix(".json"), {"yaml_sha256": sha256(path.read_bytes()).hexdigest(), "spec": source})


def fixture_state(case, fixture, now, zone):
    overrides = case.get("fixture_overrides", {})
    state = new_state(zone, now)
    require("user" not in overrides, "Use the owner guest_draft fixture.")
    if "guest_draft" in overrides:
        state.guest_draft = GuestDraft.model_validate(overrides["guest_draft"])
    for ref, label in overrides.get("booking_refs", {}).items():
        require(label in overrides.get("active_bookings", []), "A seeded booking must be activated in the fixture.")
        state.bookings[ref] = booking_entry(fixture["bookings"][label])
        state.counters.b = max(state.counters.b, int(ref[1:]))
    availability = fixture["availability"]
    for ref, value in overrides.get("slot_refs", {}).items():
        start = datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(zone)).astimezone(ZoneInfo("UTC"))
        end = start + timedelta(minutes=availability["duration_minutes"])
        state.slots[ref] = SlotEntry(start_utc=start.isoformat(), end_utc=end.isoformat(),
                                    event_type_id=availability["event_type_id"], fetched_at=now.timestamp())
        state.counters.s = max(state.counters.s, int(ref[1:]))
    return state


class TracedCalendar(FakeCal):
    def __init__(self, case, now, zone, fixture, fixture_path=None):
        super().__init__(clock=lambda: now, fixture_overrides=case.get("fixture_overrides", {}), time_zone=zone,
                         fixture_path=fixture_path)
        self.zone = zone
        self.fixture_names = {booking["uid"]: name for name, booking in fixture["bookings"].items()}
        self.reads, self.writes = [], []
        self.allow_confirm = False

    def fixture_name(self, uid):
        return self.fixture_names.get(uid)

    def slots(self, event_type_id, start, end, time_zone="UTC", *, booking_uid_to_reschedule=None, fresh=False):
        uid = booking_uid_to_reschedule
        read = {"operation": "slots", "reschedule_uid": uid, "booking_uid_to_reschedule": self.fixture_name(uid)}
        self.reads.append(read)
        result = super().slots(event_type_id, start, end, time_zone, booking_uid_to_reschedule=uid, fresh=fresh)
        read["availability"] = {
            "lower": _slot_edge(start), "upper": _slot_edge(end, end=True),
            "starts": {local_iso(slot["start"], self.zone) for day in result.values() for slot in day},
        }
        return result

    def list_bookings(self, **kwargs):
        self.reads.append({"operation": "list_bookings", "status": kwargs.get("status", "upcoming"),
                           "attendee_email": kwargs.get("attendee_email"),
                           **{key: local_iso(kwargs[key], self.zone)
                              for key in ("after_start", "before_end") if kwargs.get(key) is not None}})
        return super().list_bookings(**kwargs)

    def booking(self, uid):
        self.reads.append({"operation": "booking", "uid": uid, "fixture_booking_id": self.fixture_name(uid)})
        return super().booking(uid)

    def booking_status(self, uid):
        try:
            return super().booking(uid)["status"]
        except CalError:
            return None

    def _write(self, operation, invoke, *, uid=None, start=None, **arguments):
        record = {"operation": operation, "uid": uid, "fixture_booking_id": self.fixture_name(uid), **arguments}
        if start is not None:
            record["local_start"] = local_iso(start, self.zone)
        self.writes.append(record)
        if not self.allow_confirm:
            record["outcome"] = "outside_confirmation"
            raise CalError("invalid", "Fixture mutation refused outside confirmation.")
        try:
            result = invoke()
        except CalError as error:
            record["outcome"] = error.code
            raise
        record["outcome"] = "success"
        return result

    def create_booking(self, **kwargs):
        create = super().create_booking
        return self._write("create_booking", lambda: create(**kwargs), start=kwargs["start"],
                           name=kwargs["name"], email=kwargs["email"], notes=kwargs.get("notes"),
                           event_type_id=kwargs["event_type_id"])

    def cancel_booking(self, uid, *args, **kwargs):
        cancel = super().cancel_booking
        return self._write("cancel_booking", lambda: cancel(uid, *args, **kwargs), uid=uid)

    def reschedule_booking(self, uid, start, *args, **kwargs):
        reschedule = super().reschedule_booking
        return self._write("reschedule_booking", lambda: reschedule(uid, start, *args, **kwargs), uid=uid, start=start)


@dataclass
class Trace:
    calls: list = field(default_factory=list)
    inputs: list = field(default_factory=list)
    provider_errors: list = field(default_factory=list)

    def clear(self):
        self.calls.clear()
        self.inputs.clear()
        self.provider_errors.clear()


class Pacing:
    def __init__(self, cap):
        self.cap = cap
        self.provider_calls = Counter()
        self.http_errors = Counter()
        self.quota_streak = Counter()
        self.next_start = 0.0
        self.stop = None

    async def start(self, model):
        if sum(self.provider_calls.values()) >= self.cap:
            self.stop = "call_cap"
        if self.stop:
            raise ModelError("rate_limited")
        began = monotonic()
        await asyncio.sleep(max(0, self.next_start - began))
        started = monotonic()
        self.next_start = started + PACE_SECONDS
        self.provider_calls[model] += 1
        return (started - began) * 1000

    def outcome(self, model, error=None):
        if error is not None and error.http_status is not None:
            self.http_errors[model, error.http_status] += 1
        if error is not None and (error.http_status == 429 or error.code == "rate_limited"):
            self.next_start = max(self.next_start, monotonic() + QUOTA_BACKOFF_SECONDS)
            self.quota_streak[model] += 1
            if self.quota_streak[model] >= 2:
                self.stop = "repeated_provider_quota"
        else:
            self.quota_streak[model] = 0


class TracingModel:
    def __init__(self, adapter, name, role, trace, pacing, faults=()):
        self.adapter, self.name, self.role = adapter, name, role
        self.trace, self.pacing = trace, pacing
        self.faults = [entry["status"] for entry in faults if entry["role"] == role for _ in range(entry["attempts"])]

    async def stream(self, **request):
        wait_ms = 0.0
        simulated = bool(self.faults)
        steps = request["input_steps"]
        contains_email = bool(EMAIL_ADDRESS.search(json.dumps(steps, ensure_ascii=False)))
        if not simulated and not contains_email and self.pacing is not None:
            wait_ms = await self.pacing.start(self.name)
        self.trace.inputs.append({
            "types": [step["type"] for step in steps],
            "first_call_id": steps[0].get("call_id"),
            "first_outcome": (steps[0].get("result") or {}).get("outcome"),
            "previous_id": request["previous_interaction_id"], "pace_wait_ms": wait_ms,
            "contains_email": contains_email,
        })
        saw_text = False
        try:
            if simulated:
                status = self.faults.pop(0)
                raise ModelError("rate_limited" if status == 429 else "model_down", status, reason="fixture_fault")
            if contains_email:
                raise ModelError("model_down", reason="fixture_contact_leak")
            require(self.adapter is not None, "Unexpected model call in offline fixture.")
            async with aclosing(self.adapter.stream(**request)) as stream:
                async for item in stream:
                    if isinstance(item, ModelCompletion):
                        self.trace.calls.extend(item.tool_calls)
                    saw_text |= isinstance(item, ModelText) and bool(item.text)
                    yield item
        except ModelError as error:
            self.trace.provider_errors.append({"code": error.code if error.code in SAFE_STATUS else "other",
                                               "model": self.name, "role": self.role,
                                               "http_status": error.http_status, "simulated": simulated,
                                               "before_text": not saw_text, "reason": safe_reason(error.reason)})
            if self.pacing is not None and not simulated and not contains_email:
                self.pacing.outcome(self.name, error)
            raise
        else:
            if self.pacing is not None and not simulated and not contains_email:
                self.pacing.outcome(self.name)


def turn_payload(turn, prior):
    if "user" in turn:
        payload = {"kind": "text", "text": turn["user"]}
        if "proposal_from_turn" in turn:
            proposal = prior.get(turn["proposal_from_turn"], {}).get("proposal")
            if not proposal:
                return None
            payload["proposalId"] = proposal["id"]
        return payload
    action = dict(turn["action"])
    if "proposal_from_turn" in action:
        old = prior.get(action.pop("proposal_from_turn"), {}).get("proposal")
        if not old:
            return None
        action["proposalId"] = old["id"]
    if "booking_from_turn" in action:
        old = prior.get(action.pop("booking_from_turn"), {}).get("first_booking")
        if not old:
            return None
        action["bookingRef"] = old["ref"]
    if "slot_from_turn" in action:
        offered = prior.get(action.pop("slot_from_turn"), {}).get("slots_by_ref", {})
        if "local_start" in action:
            start = action.pop("local_start")
            refs = [ref for ref, actual in offered.items() if actual == start]
            if len(refs) != 1:
                return None
            action["slotRef"] = refs[0]
        else:
            ordinal = action.pop("ordinal")
            if not 1 <= ordinal <= len(offered):
                return None
            action["slotRef"] = list(offered)[ordinal - 1]
    return {"kind": "ui", "action": action}


def has_visible_content(event):
    if event["type"] in {"text_delta", "assistant_text"}:
        return bool(event["text"].strip())
    if event["type"] == "slot_chips":
        return bool(event.get("refs") or event.get("plainRefs"))
    return event["type"] in {"booking_cards", "proposal", "booking_result", "error"}


def run_case(spec, case, fixture, primary, backup, loop, *, model_name, pacing=None, on_turn=None, capture_dialogue=False,
             fixture_path=None):
    zone = case.get("time_zone", spec["default_time_zone"])
    now = datetime.fromisoformat(case.get("clock", spec["default_clock"]))
    calendar = TracedCalendar(case, now, zone, fixture, fixture_path)
    state = fixture_state(case, fixture, now, zone)
    secret = token_hex(32)
    token = sign_state(state, secret)
    turns = {}
    trace = Trace()
    faults = case.get("fixture_overrides", {}).get("model_faults", ())
    model = TracingModel(primary, model_name, "primary", trace, pacing, faults)
    fallback = TracingModel(backup, BACKUP_MODEL, "backup", trace, pacing, faults) if backup is not None or faults else None
    service = TurnService(model=model, backup_model=fallback, calendar=calendar, secret=secret, clock=lambda: now,
                          backup_continuations=model_name == DEFAULT_MODEL)
    try:
        client = create_app(service, turn_loop=loop).test_client()
        for number, turn in enumerate(case["turns"], 1):
            if pacing is not None and pacing.stop:
                break
            payload = turn_payload(turn, turns)
            if payload is None:
                turns[number] = {"checks": [{"check": "dependent_reference_available", "result": "failure", "observed": False}],
                                 "skipped": "prior_result_missing"}
                if on_turn is not None:
                    on_turn(turns)
                continue
            calendar.allow_confirm = confirms_signed_proposal(turn, payload, turns, state)
            calendar.writes.clear()
            calendar.reads.clear()
            trace.clear()
            began = perf_counter()
            first_content = None
            with client.post("/api/chat", json={"stateToken": token, "timeZone": zone, "input": payload}, buffered=False) as response:
                require(response.status_code == 200 and response.mimetype == "application/x-ndjson", "Invalid Flask eval response.")
                events = []
                for chunk in response.response:
                    for line in chunk.splitlines():
                        if line.strip():
                            event = json.loads(line)
                            if not events:
                                first_feedback = (perf_counter() - began) * 1000
                            if first_content is None and has_visible_content(event):
                                first_content = (perf_counter() - began) * 1000
                            events.append(event)
            elapsed = (perf_counter() - began) * 1000
            require(events and events[-1]["type"] == "turn_end", "Missing terminal eval event.")
            token = events[-1]["stateToken"]
            after = verify_state(token, secret, now)
            checked = check_turn(case["id"], turn, payload, events, turns,
                                 state, after, now, zone, calendar, trace, rephrases=case.get("rephrases"))
            if capture_dialogue and payload["kind"] == "text":
                _, visible_text, *_ = render(events)
                checked["dialogue"] = {
                    "user": EMAIL_ADDRESS.sub("[email redacted]", payload["text"])[:1000],
                    "assistant": EMAIL_ADDRESS.sub("[email redacted]", visible_text.strip())[:1000],
                    "proposal_kind": checked["proposal"].get("kind"),
                }
            turns[number] = {
                **checked, "first_feedback_ms": first_feedback, "first_visible_content_ms": first_content,
                "end_to_end_ms": elapsed,
            }
            if on_turn is not None:
                on_turn(turns)
            state = after
    finally:
        service.close_prefetch()
    return turns


def case_report(case, repetition, turns):
    private = {"booking_refs", "slots_by_ref", "first_booking", "proposal", "pending"}
    rows = [{"turn": number, **{key: value for key, value in turn.items() if key not in private}}
            for number, turn in turns.items()]
    counts = Counter(check["result"] for row in rows for check in row["checks"])
    if len(rows) != len(case["turns"]):
        status = "incomplete"
    elif counts["failure"]:
        status = "structural_failure"
    elif counts["wording_failure"]:
        status = "wording_only_failure"
    else:
        status = "pass"
    category = {"category": case["category"]} if "category" in case else {}
    return {"case": case["id"], "repetition": repetition, **category, "status": status,
            "checks": dict(counts), "turns": rows}


def percentiles(samples):
    if not samples:
        return None
    return {"n": len(samples), "p50": round(median(samples), 3),
            "p90": round(quantiles(samples, n=10, method="inclusive")[8] if len(samples) > 1 else samples[0], 3)}


def aggregate(runs, cases, repeats, minimum, mandatory, targets, *, expected_text_turns=None):
    per_case = {}
    safety_checks = {"mutations_require_confirmation", "mutations_resolve_from_signed_state",
                     "model_email_redacted", "booking_proposal_has_guest"}
    for case in cases:
        rows = [item for item in runs if item["case"] == case]
        count = sum(item["status"] == "pass" for item in rows)
        needed = repeats if case in mandatory else minimum
        safety_failures = sum(
            check["result"] == "failure" and check["check"] in safety_checks
            for row in rows for turn in row["turns"] for check in turn["checks"]
        )
        per_case[str(case)] = {"executed": len(rows), "passing_runs": count, "required": needed,
                               "safety_failures": safety_failures,
                               "passed": len(rows) == repeats and count >= needed and safety_failures == 0}
    turns = [turn for row in runs for turn in row["turns"] if "turn_ms" in turn]
    calls = [call for turn in turns for call in turn["models"]]
    paired = [(call, wait) for turn in turns for call, wait in zip(turn["models"], turn["model_pace_wait_ms"])]
    text_turns = [turn for turn in turns if turn["kind"] == "text"]
    first_text = percentiles([max(0, call["ttftMs"] - wait) for call, wait in paired if call["ttftMs"] > 0])
    text_latency = percentiles([max(0, turn["turn_ms"] - sum(turn["model_pace_wait_ms"])) for turn in text_turns])
    ui_overhead = percentiles([max(0, turn["turn_ms"] - turn["cal_ms"]) for turn in turns if turn["kind"] == "ui"])
    eligible = len(text_turns) if expected_text_turns is None else expected_text_turns
    distribution = {
        "eligible": eligible,
        "zero": sum(turn["model_calls"] == 0 for turn in text_turns),
        "one": sum(turn["model_calls"] == 1 for turn in text_turns),
        "two_or_more": sum(turn["model_calls"] >= 2 for turn in text_turns),
        "not_observed": eligible - len(text_turns),
    }
    one_call = distribution["one"] / eligible if eligible else None
    feedback = percentiles([turn["first_feedback_ms"] for turn in turns])
    content = percentiles([turn["first_visible_content_ms"] for turn in turns if turn.get("first_visible_content_ms") is not None])
    end_to_end = percentiles([
        max(0, turn["end_to_end_ms"] - sum(turn["model_pace_wait_ms"]))
        for turn in text_turns if "end_to_end_ms" in turn
    ])
    passed = sum(row["passed"] for row in per_case.values())
    return {
        "cases": per_case, "cases_passed": passed, "case_pass_rate": passed / len(cases),
        "run_status": dict(Counter(row["status"] for row in runs)),
        "checks": dict(sum((Counter(row["checks"]) for row in runs), Counter())),
        "turn_latency_ms_excluding_pace": percentiles([max(0, row["turn_ms"] - sum(row["model_pace_wait_ms"])) for row in turns]),
        "text_turn_latency_ms_excluding_pace": text_latency, "first_model_text_ms_excluding_pace": first_text,
        "text_end_to_end_ms_excluding_pace": end_to_end, "first_visible_content_ms": content,
        "first_visible_feedback_ms": feedback, "ui_overhead_without_reported_cal_ms": ui_overhead,
        "model_calls_per_turn": percentiles([row["model_calls"] for row in turns]),
        "one_model_call_fraction": one_call,
        "text_model_call_distribution": distribution,
        "input_tokens": percentiles([call["inputTokens"] for call in calls]),
        "output_tokens": percentiles([call["outputTokens"] for call in calls]),
        "input_tokens_per_turn": percentiles([sum(call["inputTokens"] for call in turn["models"]) for turn in turns]),
        "output_tokens_per_turn": percentiles([sum(call["outputTokens"] for call in turn["models"]) for turn in turns]),
        "cached_tokens_per_call": percentiles([call["cachedTokens"] for call in calls]),
        "thought_tokens_per_call": percentiles([call.get("thoughtTokens", 0) for call in calls]),
        "performance_observations": {
            "one_call_fraction": None if one_call is None else one_call >= targets["text_turns_with_one_model_call_fraction_min"],
            "first_model_text_p50": None if first_text is None else first_text["p50"] <= targets["first_model_text_ms_p50_max"],
            "text_turn_p50": None if text_latency is None else text_latency["p50"] <= targets["text_turn_ms_p50_max"],
            "text_turn_p90": None if text_latency is None else text_latency["p90"] <= targets["text_turn_ms_p90_max"],
            "first_feedback_p50": None if feedback is None else feedback["p50"] <= targets["first_visible_feedback_ms_max"],
            "ui_overhead_p90": None if ui_overhead is None else ui_overhead["p90"] <= targets["ui_server_overhead_excluding_cal_ms_max"],
        },
    }


def print_summary(summary):
    count = summary["cases_passed"]
    total = len(summary["cases"])
    print(f"\nCases passed: {count}/{total} ({summary['case_pass_rate']:.1%})")
    print(f"{'Metric':<35} {'p50':>10} {'p90':>10} {'Samples':>8}")
    fields = (
        ("Turn latency (ms)", "turn_latency_ms_excluding_pace"),
        ("Text turn latency (ms)", "text_turn_latency_ms_excluding_pace"),
        ("Text end-to-end latency (ms)", "text_end_to_end_ms_excluding_pace"),
        ("First model text (ms)", "first_model_text_ms_excluding_pace"),
        ("First visible feedback (ms)", "first_visible_feedback_ms"),
        ("First visible content (ms, paced)", "first_visible_content_ms"),
        ("Model calls per turn", "model_calls_per_turn"),
        ("Input tokens per turn", "input_tokens_per_turn"),
        ("Output tokens per turn", "output_tokens_per_turn"),
        ("Cached tokens per call", "cached_tokens_per_call"),
    )
    for label, key in fields:
        sample = summary[key]
        if sample is None:
            print(f"{label:<35} {'—':>10} {'—':>10} {0:>8}")
        else:
            print(f"{label:<35} {sample['p50']:>10.2f} {sample['p90']:>10.2f} {sample['n']:>8}")
    counts = summary["text_model_call_distribution"]
    if counts["eligible"]:
        print(f"Text turns: {counts['zero']} zero calls, {counts['one']} one call, "
              f"{counts['two_or_more']} two or more, {counts['not_observed']} not observed. "
              f"Single call: {summary['one_model_call_fraction']:.1%} of {counts['eligible']}.")


def checkpoint(output, document):
    temporary = output.with_name(f".{output.name}.{token_hex(4)}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as file:
        json.dump(document, file, indent=2, sort_keys=True)
        file.write("\n")
    temporary.replace(output)


def compare(document, reference):
    baseline = json.loads(reference.read_text(encoding="utf-8"))
    primary = document["model"]
    candidate_complete = (document["model"] == COMPARISON_MODEL and document["qualifying_schedule"] and document["complete"])
    baseline_complete = (baseline["model"] == DEFAULT_MODEL and baseline["qualifying_schedule"] and baseline["complete"])
    same_spec = baseline["spec_sha256"] == document["spec_sha256"] and baseline["fixture_sha256"] == document["fixture_sha256"]
    no_503 = document["provider_http_errors"].get(primary, {}).get("503", 0) == 0
    more = document["summary"]["cases_passed"] > baseline["summary"]["cases_passed"]
    return {"candidate_complete": candidate_complete, "baseline_complete": baseline_complete,
            "same_spec_and_fixture": same_spec, "no_candidate_http_503": no_503, "passes_more_cases": more,
            "gate_passed": all((candidate_complete, baseline_complete, same_spec, no_503, more))}


def run(spec, cases, repeats, profile, output, call_cap, reference=None, *, suite="approved"):
    model_name = profile["model"]
    key = os.environ.get("GEMINI_API_KEY") or load_local_environment().get("GEMINI_API_KEY")
    require(bool(key), "Set an unbilled Gemini project key in .env.local.")
    comparison = model_name == COMPARISON_MODEL
    primary = GeminiClient(key, model=model_name, thinking_level=profile["thinking_level"], persistent=True)
    backup = None if comparison else GeminiClient(
        key, model=BACKUP_MODEL, thinking_level="minimal", persistent=True, first_event_timeout=None,
    )

    async def close():
        try:
            await primary.aclose()
        finally:
            if backup is not None:
                await backup.aclose()

    loop = TurnLoop(close)
    pacing = Pacing(call_cap)
    case_ids = [case["id"] for case in cases]
    mandatory = set(spec["runs"]["default"]["must_pass_every_repetition"])
    minimum = min(repeats, profile["minimum_passing_repetitions"])
    fixture_path = suite_fixture_path(spec, suite)
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    expected_text_turns = repeats * sum("user" in turn for case in cases for turn in case["turns"])
    document = {
        "schema_version": 1, "calendar_mode": "owner", "model": model_name, "thinking": profile["thinking_level"],
        "suite": suite,
        "backup_model": None if comparison else BACKUP_MODEL, "case_ids": case_ids, "repeats": repeats,
        "spec_sha256": sha256(SPEC_PATH.read_bytes()).hexdigest(), "fixture_sha256": sha256(fixture_path.read_bytes()).hexdigest(),
        "qualifying_schedule": case_ids == list(APPROVED_CASE_IDS) and repeats == profile["repetitions_per_case"],
        "complete": False, "stop": None, "runs": [], "in_progress": None, "pace_seconds": PACE_SECONDS,
        "expected_text_turns": expected_text_turns,
        "quota_backoff_seconds": QUOTA_BACKOFF_SECONDS, "provider_call_cap": call_cap,
    }
    if suite in HELD_OUT_SPECS:
        document["held_out_sha256"] = sha256(HELD_OUT_SPECS[suite][0].read_bytes()).hexdigest()
    document.update(regression_hashes(case_ids))

    def save():
        http = {}
        for (provider, status), count in pacing.http_errors.items():
            http.setdefault(provider, {})[str(status)] = count
        document.update(stop=pacing.stop, provider_calls=dict(pacing.provider_calls), provider_http_errors=http)
        document["summary"] = aggregate(
            document["runs"], case_ids, repeats, minimum, mandatory, spec["performance_targets"],
            expected_text_turns=expected_text_turns,
        )
        checkpoint(output, document)

    save()
    interrupted = False
    try:
        for repetition in range(1, repeats + 1):
            for case in cases:
                if pacing.stop:
                    break
                def save_turn(turns):
                    document["in_progress"] = case_report(case, repetition, turns)
                    save()
                try:
                    turns = run_case(spec, case, fixture, primary, backup, loop, model_name=model_name,
                                     pacing=pacing, on_turn=save_turn, fixture_path=fixture_path, capture_dialogue=(
                                         suite in {"scheduling_held_out", "scheduling_follow_up"}
                                         or repetition == 1 and case["id"] in {10, 24, 25}
                                     ))
                    result = case_report(case, repetition, turns)
                except Exception as error:
                    result = {"case": case["id"], "repetition": repetition, "status": "harness_error", "turns": [],
                              "checks": {"failure": 1}, "error_type": type(error).__name__}
                document["runs"].append(result)
                document["in_progress"] = None
                save()
                print(f"Case {case['id']:02}, run {repetition}: {result['status']}", flush=True)
            if pacing.stop:
                break
    except KeyboardInterrupt:
        interrupted = True
        pacing.stop = "interrupted"
    finally:
        loop.close()
        document["complete"] = not interrupted and not pacing.stop and len(document["runs"]) == len(cases) * repeats
        save()
    if reference is not None:
        document["comparison_gate"] = compare(document, reference)
        checkpoint(output, document)
    print_summary(document["summary"])
    print(f"Results: {output}", flush=True)
    return 0 if document["complete"] and document["summary"]["cases_passed"] == len(cases) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=(DEFAULT_MODEL, COMPARISON_MODEL))
    parser.add_argument("--cases", type=int, nargs="+", help="Run specified approved or regression cases.")
    frozen = parser.add_mutually_exclusive_group()
    frozen.add_argument("--held-out", action="store_const", dest="suite", const="held_out",
                        help="Run the six frozen owner rewordings once on the default model.")
    frozen.add_argument("--scheduling-held-out", action="store_const", dest="suite", const="scheduling_held_out",
                        help="Run the 20 frozen scheduling cases once on the default model.")
    frozen.add_argument("--scheduling-follow-up", action="store_const", dest="suite", const="scheduling_follow_up",
                        help="Run the 11 frozen scheduling follow-up cases once on the default model.")
    parser.add_argument("--repeats", choices=range(1, 4), type=int, help="Override the model's reviewed repetition count.")
    parser.add_argument("--refresh-spec", action="store_true", help="Regenerate the JSON snapshots from YAML offline.")
    parser.add_argument("--yaml-python", help="Existing Python interpreter with PyYAML for --refresh-spec only.")
    parser.add_argument("--output", type=Path, help="Use a new JSON path for a selected or repeated run.")
    parser.add_argument("--report", type=Path, help="Print the metrics table from an existing JSON report offline.")
    parser.add_argument("--against", type=Path, help="Compare a 3.1 Flash-Lite result with the complete default-model JSON.")
    parser.add_argument("--max-calls", type=int, default=DEFAULT_CALL_CAP, help="Conservative provider call cap for this run.")
    parser.add_argument("--describe", action="store_true", help="Validate and show the selection offline.")
    args = parser.parse_args()
    if args.refresh_spec:
        refresh_spec(args.yaml_python)
        print("Regenerated the approved and supplemental JSON snapshots.")
        return 0
    if args.report is not None:
        spec = approved_spec()
        document = json.loads(args.report.read_text(encoding="utf-8"))
        require(document["spec_sha256"] == sha256(SPEC_PATH.read_bytes()).hexdigest(), "The report uses a different spec.")
        suite = document.get("suite")
        held_out = suite in HELD_OUT_SPECS
        if held_out:
            require(document["held_out_sha256"] == sha256(HELD_OUT_SPECS[suite][0].read_bytes()).hexdigest(),
                    "The held-out spec differs.")
        if suite == "scheduling_follow_up":
            require(document["fixture_sha256"] == sha256(suite_fixture_path(spec, suite).read_bytes()).hexdigest(),
                    "The follow-up calendar fixture differs.")
        for field, digest in regression_hashes(document["case_ids"]).items():
            require(document.get(field) == digest, "The regression spec differs.")
        profile = model_profile(spec, document["model"], held_out)
        repeats = document["repeats"]
        summary = aggregate(document["runs"], document["case_ids"], repeats,
                            min(repeats, profile["minimum_passing_repetitions"]),
                            set(spec["runs"]["default"]["must_pass_every_repetition"]), spec["performance_targets"],
                            expected_text_turns=document.get("expected_text_turns"))
        print_summary(summary)
        return 0
    require(args.model is not None, "--model is required for an evaluation.")
    require(args.yaml_python is None, "--yaml-python is only used with --refresh-spec.")
    require(1 <= args.max_calls <= DEFAULT_CALL_CAP, "Invalid provider call cap.")
    require(args.against is None or args.model == COMPARISON_MODEL, "--against requires the comparison model.")
    suite = args.suite or "approved"
    held_out = suite in HELD_OUT_SPECS
    if held_out:
        require(args.model == DEFAULT_MODEL and args.cases is None and args.repeats in {None, 1},
                "Use all frozen held-out cases exactly once on the default model.")
    logging.disable(logging.CRITICAL)
    spec = approved_spec()
    regression_selected = bool(REGRESSION_CASE_IDS.intersection(args.cases or ()))
    if held_out:
        cases = held_out_cases(spec, suite=suite)
    else:
        selected = set(args.cases or APPROVED_CASE_IDS)
        require(selected <= {*APPROVED_CASE_IDS, *REGRESSION_CASE_IDS}, "Unknown evaluation case.")
        candidates = [*spec["cases"], *regression_cases(spec, selected)]
        cases = [case for case in candidates if case["id"] in selected]
    profile = model_profile(spec, args.model, held_out)
    repeats = args.repeats or profile["repetitions_per_case"]
    if args.describe:
        print(json.dumps({"model": args.model, "thinking": profile["thinking_level"],
                          "cases": [case["id"] for case in cases], "repeats": repeats,
                          "pace_seconds": PACE_SECONDS, "backoff_seconds": QUOTA_BACKOFF_SECONDS, "call_cap": args.max_calls}))
        return 0
    date = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    suffix = "-" + suite.replace("_", "-") if held_out else "-regressions" if regression_selected else ""
    output = args.output or Path(spec["runs"]["results_path"].format(model=args.model, date=date + suffix))
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.touch(exist_ok=False)
    except FileExistsError as error:
        raise EvalError("The report already exists. Select a new --output path.") from error
    return run(spec, cases, repeats, profile, output, args.max_calls, args.against, suite=suite)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvalError as error:
        print(str(error))
        raise SystemExit(1) from None
