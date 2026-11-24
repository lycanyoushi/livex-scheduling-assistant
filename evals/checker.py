"""Check approved scheduling assertions against an observed chat turn."""

from collections import Counter
from datetime import datetime
import re
from zoneinfo import ZoneInfo


SAFE_TOOLS = frozenset({"find_slots", "list_bookings", "propose_booking", "propose_cancel", "propose_reschedule"})
SAFE_STATUS = frozenset({
    "completed", "requires_action", "rate_limited", "model_down", "timeout", "refusal", "interaction_expired",
    "slot_taken", "invalid_action", "cal_down", "loop_limit", "empty", "session_expired", "stream_error", "incomplete",
})
SAFE_REASONS = frozenset({
    "provider_model_down", "provider_stream_error", "duplicate_call_step", "missing_call_id_or_name",
    "arguments_without_call_step", "incomplete_without_text", "missing_interaction_id", "unclosed_stream_step",
    "invalid_arguments_json", "arguments_not_an_object", "duplicate_call_id", "requires_action_without_calls",
    "stream_ended_without_completion", "missing_model_completion", "unexpected_completion_status",
    "terminal_status_cancelled", "terminal_status_failed", "terminal_status_incomplete", "terminal_status_in_progress",
    "terminal_status_queued", "terminal_status_requires_continuation", "terminal_status_other", "fixture_fault",
    "first_event_timeout", "stream_idle_timeout", "overall_timeout", "provider_timeout",
})
REFERENCE = re.compile(r"\[\[([SB][1-9][0-9]*)\]\]")
SPANISH = re.compile(
    r"\b(?:hay|puedo|puedes|tengo|tenemos|jueves|disponible[sn]?|horario[s]?|opci[oó]n(?:es)?|"
    r"para|elegir|prefieres|puede|reservar|por favor|elige|cu[aá]l)\b", re.IGNORECASE,
)


class EvalError(RuntimeError):
    """An invalid fixture or an incomplete evaluation observation."""


def require(condition, label):
    if not condition:
        raise EvalError(label)


def local_iso(value, zone):
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    return instant.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%dT%H:%M")


def safe_reason(value):
    if isinstance(value, str) and (value in SAFE_REASONS or re.fullmatch(
        r"provider_http_[1-5][0-9]{2}|unexpected_exception:[A-Za-z][A-Za-z0-9_]{0,39}", value,
    )):
        return value
    return None


def render(events):
    """Collect user-visible references from text and explicit card events."""
    message = ""
    slots, bookings, inline_bookings = [], [], []
    notes, error_messages = [], []
    visible_markers = 0
    for event in events:
        if event["type"] == "text_delta":
            message += event["text"]
        elif event["type"] == "assistant_text":
            message = "\n\n".join(value for value in (message, event["text"]) if value)
        elif event["type"] == "slot_chips":
            slots.extend(event["refs"])
            if event.get("note"):
                notes.append(event["note"])
        elif event["type"] == "booking_cards":
            bookings.extend(event["refs"])
        elif event["type"] == "error":
            error_messages.append(event["message"])
        if event["type"] in {"text_delta", "assistant_text"}:
            markers = REFERENCE.findall(message)
            for ref in markers[visible_markers:]:
                if ref.startswith("S"):
                    slots.append(ref)
                else:
                    bookings.append(ref)
                    inline_bookings.append(ref)
            visible_markers = len(markers)
    return (message, "\n".join([message, *notes, *error_messages]), list(dict.fromkeys(slots)),
            list(dict.fromkeys(bookings)), list(dict.fromkeys(inline_bookings)))


def check_turn(case_id, turn_spec, payload, events, prior, before, after, now, zone, calendar, model):
    """Evaluate the approved assertions against the captured application turn."""
    plan = turn_spec["checks"]
    check_rows = []

    def check(label, matches, observed=None, *, wording=False, soft=False):
        check_rows.append({"check": label, "result": "pass" if matches else
                           "soft" if soft else "wording_failure" if wording else "failure", "observed": observed})

    def slot(ref):
        if not isinstance(ref, str):
            return None
        entry = after.slots.get(ref) or before.slots.get(ref)
        return local_iso(entry.start_utc, zone) if entry else None

    def booking(ref):
        if not isinstance(ref, str):
            return None
        entry = after.bookings.get(ref) or before.bookings.get(ref)
        return calendar.fixture_name(entry.uid) if entry else None

    def argument_matches(key, value, expected):
        if not isinstance(expected, dict):
            return value == expected
        if key == "slot_ref":
            return slot(value) == expected["local_start"]
        if key == "booking_ref":
            matches = booking(value) == expected["fixture_booking_id"]
            if "shown_on_turn" in expected:
                shown = prior.get(expected["shown_on_turn"], {}).get("booking_refs", [])
                ordinal = expected["ordinal"]
                matches &= len(shown) >= ordinal and shown[ordinal - 1] == value
            return bool(matches)
        raise EvalError("Unhandled tool argument selector.")

    text_input = payload["kind"] == "text"
    reply, visible_reply, slot_refs, booking_refs, inline_bookings = render(events)
    final = [event for event in events if event["type"] == "turn_end"]
    require(len(final) == 1 and events[-1] is final[0], "invalid_terminal_event")
    metrics = final[0]["metrics"]
    provider_error = metrics.get("errorCode")
    intended_refusal = case_id in {16, 18}
    template = plan.get("template")
    expected_failure = (
        (intended_refusal and provider_error == "refusal")
        or (template == "slot_taken" and provider_error == "slot_taken")
        or (plan.get("fallback") == "code_only" and metrics.get("fallback") == "code_only")
    )
    check("turn_outcome", metrics["outcome"] == "ok" or expected_failure,
          {"outcome": metrics["outcome"], "code": provider_error if provider_error in SAFE_STATUS else None})
    if plan.get("attempted_model_roles"):
        roles = [item.get("modelRole") for item in metrics["modelCalls"]]
        check("attempted_model_roles", roles == plan["attempted_model_roles"], roles)
    if plan.get("fallback"):
        check("fallback", metrics.get("fallback") == plan["fallback"], metrics.get("fallback"))
    n_model = len(metrics["modelCalls"])
    check("model_call_trace_agrees", n_model == len(model.inputs), len(model.inputs))
    check("unknown_refs", metrics["unknownRefs"] == 0, metrics["unknownRefs"])
    check("model_call_bounds", plan.get("min_model_calls", 0) <= n_model <= min(plan["max_model_calls"], 3 if text_input else 0), n_model)

    proposal_events = [event["proposal"] for event in events if event["type"] == "proposal"]
    proposals = [proposal["kind"] for proposal in proposal_events]
    expected = plan["proposal_kind"]
    allowed = expected if isinstance(expected, list) else [expected]
    check("proposal_kind", any(proposals == ([] if kind is None else [kind]) for kind in allowed), proposals)
    if proposal_events:
        check("proposal_from_signed_state", len(proposal_events) == 1 and after.proposal is not None
              and after.proposal.id == proposal_events[0]["id"], bool(after.proposal))
    proposal_event = proposal_events[0] if proposal_events else {}
    if plan.get("proposal_email"):
        email = (after.proposal.email if after.proposal else None) or proposal_event.get("email")
        check("resolved_proposal_email", email == plan["proposal_email"], {"present": email is not None})
    if plan.get("pending_tool_call"):
        check("pending_tool_call", after.pending_call is not None and after.pending_call.name == plan["pending_tool_call"],
              after.pending_call.name if after.pending_call else None)
    if plan.get("proposal_differs_from_turn"):
        previous = prior[plan["proposal_differs_from_turn"]]["proposal"]
        differs = bool(proposal_event and previous and proposal_event["id"] != previous["id"])
        check("distinct_replacement_proposal", differs, differs)
    if plan.get("first_model_step"):
        description = plan["first_model_step"]
        previous = prior[description["call_id_from_turn"]]["pending"]
        data = model.inputs[0] if model.inputs else {}
        tracked_id = bool(previous and data.get("first_call_id") == previous.id)
        tracked_branch = bool(before.interaction_id and data.get("previous_id") == before.interaction_id)
        check("pending_result_then_user_input", tracked_id and tracked_branch and
              data.get("types") == [description["type"], description["followed_by"]] and
              data.get("first_outcome") == description["outcome"],
              {"types": data.get("types"), "outcome": data.get("first_outcome"), "matched_prior_call": tracked_id,
               "matched_prior_interaction": tracked_branch})

    observed_tools = Counter(call.name for call in model.calls)
    metric_tools = Counter(item["name"] for item in metrics["toolCalls"])
    check("tool_trace_agrees", Counter(name if name in SAFE_TOOLS else "unknown" for name in observed_tools.elements()) == metric_tools,
          dict(metric_tools))
    rejected = Counter(item["name"] for item in metrics["toolCalls"] if not item["ok"])
    for expected_call in plan.get("tool_calls", []):
        required = [
            call for call in model.calls if call.name == expected_call["name"] and all(
                key in call.arguments and argument_matches(key, call.arguments[key], value)
                for key, value in expected_call.get("args", {}).items()
            )
        ]
        name = expected_call["name"]
        check("required_tool:" + name, bool(required),
              {"matching_calls": len(required), "tool_name_attempts": observed_tools[name]})
    forbidden = set(plan.get("forbidden_tools", []))
    bad = {name: count for name, count in observed_tools.items() if name in forbidden or "*" in forbidden}
    hard_bad, soft_bad = [], []
    for name, count in bad.items():
        if name in SAFE_TOOLS and plan.get("rejected_forbidden_tools") == "report_only" and rejected[name] == count:
            soft_bad.append(name)
        else:
            hard_bad.append(name if name in SAFE_TOOLS else "unknown")
    check("forbidden_tools", not hard_bad, sorted(hard_bad))
    if soft_bad:
        check("rejected_forbidden_tools", False, sorted(soft_bad), soft=True)

    refs_condition = plan["refs_within"]
    if refs_condition is not None:
        if refs_condition["kind"] == "slot":
            refs = list(slot_refs)
            if refs_condition.get("source") == "offers_or_proposal" and proposal_event.get("slotRef"):
                refs.append(proposal_event["slotRef"])
            refs = list(dict.fromkeys(refs))
            values = [slot(ref) for ref in refs]
            check("slot_offer_refs_resolve", all(values), values)
            if "local_dates" in refs_condition:
                check("slot_offer_local_dates", all(value and value[:10] in refs_condition["local_dates"] for value in values), values)
            if "local_starts" in refs_condition:
                check("slot_offer_allowed_starts", all(value in refs_condition["local_starts"] for value in values), values)
            if "includes_local_starts" in refs_condition:
                check("slot_offer_required_starts", set(refs_condition["includes_local_starts"]).issubset(values), values)
            if refs_condition.get("after_clock"):
                check("slot_offers_in_future", all(
                    value and datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(zone)) > now
                    for value in values
                ), values)
            for field, op in (("local_time_start_inclusive", lambda a, b: a >= b), ("local_time_end_exclusive", lambda a, b: a < b)):
                if field in refs_condition:
                    check(field, all(value and op(value[11:], refs_condition[field]) for value in values), values)
        else:
            require(refs_condition["kind"] == "booking", "unexpected_reference_kind")
            refs = booking_refs
            values = [booking(ref) for ref in refs]
            if "booking_ids_in_order" in refs_condition:
                check("booking_offer_order", values == refs_condition["booking_ids_in_order"], values)
        if "min" in refs_condition or "max" in refs_condition:
            check("distinct_offer_count", refs_condition.get("min", 0) <= len(refs) <= refs_condition.get("max", len(refs)), len(refs))
    if plan.get("prefetch_hit"):
        focus = plan["prefetch_hit"]
        hit = metrics["prefetch"].get(focus, 0) > 0 and not metrics["prefetch"]["timedOut"]
        check("prefetch_hit:" + focus, hit, metrics["prefetch"].get(focus, 0))

    def masked_record(operation):
        return {key: value for key, value in operation.items() if key not in {"uid", "email", "attendee_email", "reschedule_uid"}}

    def expected_record(wanted, actual):
        for key, value in wanted.items():
            if key == "booking_from_turn":
                previous_result = prior.get(value, {}).get("first_booking")
                if not previous_result or previous_result["uid"] != actual.get("uid"):
                    return False
            elif key == "uid_differs_from_turn":
                previous_result = prior.get(value, {}).get("first_booking")
                if not previous_result or not actual.get("uid") or previous_result["uid"] == actual["uid"]:
                    return False
            elif key == "booking_uid_to_reschedule":
                if "fixture_booking_id" in value:
                    if actual.get("booking_uid_to_reschedule") != value["fixture_booking_id"]:
                        return False
                else:
                    previous_result = prior.get(value["booking_from_turn"], {}).get("first_booking")
                    if not previous_result or previous_result["uid"] != actual.get("reschedule_uid"):
                        return False
            elif actual.get(key) != value:
                return False
        return True

    for item in plan.get("cal_reads", []):
        observed = [read for read in calendar.reads if read["operation"] == item["operation"]]
        check("required_calendar_read:" + item["operation"], any(expected_record(item, read) for read in observed),
              [masked_record(read) for read in observed])
    if plan.get("forbidden_cal_calls"):
        forbidden_reads = [read["operation"] for read in calendar.reads if read["operation"] in plan["forbidden_cal_calls"]]
        check("forbidden_calendar_reads", not forbidden_reads, forbidden_reads)

    writes = list(calendar.writes)
    expected_writes = plan.get("cal_writes", [])
    check("exact_calendar_writes", len(writes) == len(expected_writes) and all(
        expected_record(wanted, actual) for wanted, actual in zip(expected_writes, writes)
    ), [masked_record(write) for write in writes])
    check("mutations_require_confirmation", not writes or payload.get("action", {}).get("type") == "confirm_proposal",
          len(writes))
    if writes:
        saved_booking = before.bookings.get(before.proposal.booking_ref) if before.proposal else None
        saved_slot = before.slots.get(before.proposal.slot_ref) if before.proposal else None
        confirmed_proposal = bool(before.proposal and payload.get("action", {}).get("proposalId") == before.proposal.id)
        valid_targets = confirmed_proposal and all(
            (write["operation"] == "create_booking" or (saved_booking and write["uid"] == saved_booking.uid))
            and (write["operation"] == "cancel_booking" or (saved_slot and
                 write["local_start"] == local_iso(saved_slot.start_utc, zone)))
            and (write["operation"] != "create_booking" or (saved_slot and write["event_type_id"] == saved_slot.event_type_id))
            for write in writes
        )
        check("mutations_resolve_from_signed_state", bool(valid_targets), bool(valid_targets))
    results = []
    for event in events:
        if event["type"] != "booking_result":
            continue
        view = event["booking"]
        entry = after.bookings.get(view["ref"])
        results.append({
            "kind": event["kind"], "fixture_booking_id": booking(view["ref"]),
            "local_start": local_iso(view["startUtc"], zone), "uid": entry.uid if entry else None, "ref": view["ref"],
        })
    wanted = plan.get("booking_result")
    check("booking_result", not results if wanted is None else len(results) == 1 and expected_record(wanted, results[0]),
          [masked_record(item) for item in results])
    if wanted and wanted["kind"] == "cancelled":
        target = wanted["fixture_booking_id"]
        target_uid = next(uid for uid, key in calendar.fixture_names.items() if key == target)
        status = calendar.booking_status(target_uid)
        check("fixture_cancellation_effect", status == "cancelled", status)
    if template in {"booked", "cancelled", "rescheduled"}:
        check("template:" + template, len(results) == 1 and results[0]["kind"] == template and any(
            event["type"] == "assistant_text" for event in events
        ), [item["kind"] for item in results])
    elif template == "slot_taken":
        check("template:slot_taken", provider_error == "slot_taken" and bool(slot_refs), provider_error)
    elif template == "model_down_slots":
        check("template:model_down_slots", metrics.get("fallback") == "code_only" and bool(slot_refs), metrics.get("fallback"))

    if plan.get("reply_max_lines"):
        line_count = len([line for line in reply.splitlines() if line.strip()])
        check("reply_max_lines", 0 < line_count <= plan["reply_max_lines"], line_count, wording=True)
    if plan.get("reply_language") == "es":
        words = SPANISH.findall(reply)
        check("reply_language:es", len(set(word.casefold() for word in words)) >= 2, len(set(words)), wording=True)
    pattern_results = [bool(re.search(pattern, visible_reply)) for pattern in plan.get("reply_patterns", [])]
    for index, matched in enumerate(pattern_results, 1):
        check(f"reply_pattern:{index}", matched, matched, wording=True)
    if intended_refusal:
        check("refusal_is_visible", provider_error == "refusal" or (bool(pattern_results) and all(pattern_results)),
              {"error_refusal": provider_error == "refusal", "pattern_matches": pattern_results})

    safe_calls = []
    for call in model.calls:
        summary = {"name": call.name if call.name in SAFE_TOOLS else "unknown"}
        if isinstance(call.arguments.get("slot_ref"), str):
            summary["slot_local_start"] = slot(call.arguments["slot_ref"])
        if isinstance(call.arguments.get("booking_ref"), str):
            summary["fixture_booking"] = booking(call.arguments["booking_ref"])
        if isinstance(call.arguments.get("for_booking_ref"), str):
            summary["for_fixture_booking"] = booking(call.arguments["for_booking_ref"])
        safe_calls.append(summary)
    return {
        "checks": check_rows,
        "model_calls": n_model,
        "models": [{**{key: value for key, value in row.items() if key in {
                        "firstEventMs", "ttftMs", "totalMs", "inputTokens", "outputTokens", "cachedTokens", "thoughtTokens",
                        "status", "modelRole",
                        "httpStatus",
                    }}, "reason": safe_reason(row.get("reason"))} for row in metrics["modelCalls"]],
        "provider_errors": list(model.provider_errors),
        "model_pace_wait_ms": [round(item["pace_wait_ms"], 2) for item in model.inputs],
        "turn_ms": metrics["totalMs"],
        "cal_ms": sum(item["ms"] for item in metrics["calCalls"]),
        "kind": payload["kind"],
        "tool_calls": safe_calls,
        "slot_offer_starts": [slot(ref) for ref in slot_refs],
        "booking_offer_order": [booking(ref) for ref in booking_refs],
        "inline_booking_order": [booking(ref) for ref in inline_bookings],
        "proposal_slot": slot(proposal_event.get("slotRef")),
        "proposal_fixture_booking": booking(proposal_event.get("bookingRef")),
        "booking_refs": booking_refs,
        "slots_by_ref": {ref: slot(ref) for ref in slot_refs},
        "first_booking": next(iter(results), None),
        "proposal": proposal_event,
        "pending": after.pending_call,
    }
