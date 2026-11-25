"""Interactive terminal front end for the same scheduling turns as the website."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import aclosing
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import TextIO

from backend.cal_client import CalError
from backend.protocol import BookingView, ChatRequest, ProposalView, SlotView, TurnMetrics, validate_time_zone
from backend.reference_text import ReferenceLabel, ReferenceText, repeats_time
from backend.server import load_service
from backend.state import MAX_BOOKINGS, MAX_SLOTS
from backend.turn import TurnService, proposal_reply


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_COMMANDS = "/new  /tz <IANA time zone>  /debug  /quit"
_ACTIVE = {"accepted", "pending"}
_UNVERIFIED_WRITES = {"write_unverified", "reschedule_partial"}
_PROPOSAL_TITLES = {"book": "Book this meeting?", "cancel": "Cancel this meeting?", "reschedule": "Move this meeting?"}
_RESULT_LABELS = {"booked": "Meeting booked", "cancelled": "Meeting cancelled", "rescheduled": "Meeting moved"}
_CAL_BOOKINGS_URL = "https://app.cal.com/bookings/upcoming"


def _windows_zone() -> str | None:
    if os.name != "nt":
        return None
    import ctypes
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation") as key:
            name, _ = winreg.QueryValueEx(key, "TimeZoneKeyName")
        map_zone = ctypes.CDLL("icu.dll").ucal_getTimeZoneIDForWindowsID
        map_zone.argtypes = (
            ctypes.c_wchar_p, ctypes.c_int32, ctypes.c_char_p, ctypes.c_wchar_p,
            ctypes.c_int32, ctypes.POINTER(ctypes.c_int32),
        )
        map_zone.restype = ctypes.c_int32
        zone, status = ctypes.create_unicode_buffer(128), ctypes.c_int32()
        length = map_zone(name, len(name), b"001", zone, len(zone), ctypes.byref(status))
        return zone.value if status.value <= 0 and 0 < length < len(zone) else None
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def detect_time_zone(profile_zone: str | None = None) -> str:
    local = datetime.now().astimezone().tzinfo
    try:
        local_path = Path("/etc/localtime").resolve().as_posix().partition("/zoneinfo/")[2]
    except (OSError, RuntimeError):
        local_path = ""
    for candidate in (os.environ.get("TZ", "").removeprefix(":"), getattr(local, "key", None), local_path,
                      _windows_zone(), profile_zone):
        if candidate:
            try:
                return validate_time_zone(candidate)
            except ValueError:
                continue
    return "UTC"


def _ui(action: str, **fields: str) -> dict:
    return {"kind": "ui", "action": {"type": action, **fields}}


def _recent(views: dict, limit: int) -> dict:
    return dict(sorted(views.items(), key=lambda item: int(item[0][1:]))[-limit:])


def _decision(request: dict) -> str | None:
    if request["kind"] == "text":
        return proposal_reply(request["text"]) if request.get("proposalId") else None
    return {"confirm_proposal": "confirm", "dismiss_proposal": "dismiss"}.get(request["action"]["type"])


class _TurnDisplay:
    def __init__(self, chat: TerminalChat) -> None:
        self.chat = chat
        self.slots = dict(chat.slots)
        self.bookings = dict(chat.bookings)
        self.numbers = {ref: number for number, ref in chat.choices.items()}
        self.listed: list[str] = []
        self.retired_bookings: set[str] = set()
        self.recovery_refs: set[str] = set()
        self.proposal: ProposalView | None = None
        self.result: tuple[str, BookingView] | None = None
        self.error: str | None = None
        self.retryable = False
        self.booker = False
        self.text = ReferenceText(self.label)
        self.line_open = False

    def slot_number(self, ref: str) -> int:
        if ref not in self.numbers:
            self.numbers[ref] = self.chat.next_choice
            self.chat.next_choice += 1
        return self.numbers[ref]

    def slot_label(self, ref: str) -> str:
        slot = self.slots[ref]
        return f"[{self.slot_number(ref)}] {slot['dayLabel']} {slot['timeLabel']}"

    def label(self, ref: str, preceding: str) -> ReferenceLabel:
        if ref in self.slots:
            if repeats_time(preceding, self.slots[ref]["timeLabel"]):
                return ReferenceLabel(f"[{self.slot_number(ref)}]", compact=True)
            return ReferenceLabel(self.slot_label(ref))
        if ref in self.bookings:
            booking = self.bookings[ref]
            when = booking["whenLabel"]
            repeated = repeats_time(preceding, when)
            if ref in self.listed:
                return ReferenceLabel("", compact=True) if repeated else ReferenceLabel(f"[{ref}]")
            if repeated:
                when = when.rsplit(", ", 1)[0] if ", " in when else ""
            return ReferenceLabel(f"{booking['title']} · {when}" if when else booking["title"])
        return ReferenceLabel(ref)

    def delta(self, value: str, *, final: bool = False) -> None:
        text = self.text.feed(value, final=final)
        if text:
            prefix = "" if self.line_open else "Assistant: "
            self.chat.write(prefix + text, end="")
            self.line_open = True

    def finish_text(self) -> None:
        self.delta("", final=True)
        if self.line_open:
            self.chat.write("")
            self.line_open = False

    def show(self, event) -> None:
        kind = event["type"]
        if kind == "refs":
            self.slots.update(event.get("slots", {}))
            self.bookings.update(event.get("bookings", {}))
            self.retired_bookings.update(event.get("retiredBookings", ()))
            for ref in self.retired_bookings:
                self.bookings.pop(ref, None)
        elif kind == "booking_cards":
            self.listed.extend(ref for ref in event["refs"] if ref in self.bookings)
        elif kind == "text_delta":
            self.delta(event["text"])
        else:
            self.finish_text()
            if kind == "status":
                self.chat.write(event["text"], dim=True)
            elif kind == "assistant_text":
                self.delta(event["text"], final=True)
                self.finish_text()
            elif kind == "slot_chips":
                if event.get("note"):
                    self.chat.write(event["note"])
                for ref in event["refs"]:
                    if ref in self.slots:
                        self.recovery_refs.add(ref)
                        self.chat.write(self.slot_label(ref))
                for ref in event.get("plainRefs", ()):
                    slot = self.slots.get(ref)
                    if slot is not None:
                        self.chat.write(f"{slot['dayLabel']} {slot['timeLabel']}")
            elif kind == "proposal":
                self.proposal = event["proposal"].copy()
            elif kind == "booking_result":
                booking = event["booking"]
                self.bookings[booking["ref"]] = booking
                self.result = event["kind"], booking
            elif kind == "error":
                self.error = event["code"]
                self.retryable = event["retryable"]
                self.booker = event.get("fallback") == "booker"
                self.chat.write(event["message"])

    def commit(self, event, request: dict) -> None:
        chat = self.chat
        chat.token, chat.metrics = event["stateToken"], event["metrics"]
        for ref in self.retired_bookings:
            chat.bookings.pop(ref, None)
        if chat.latest_booking in self.retired_bookings:
            chat.latest_booking = None
        success = chat.metrics["outcome"] == "ok" or chat.metrics.get("fallback") == "code_only"
        slot_action = (
            request["kind"] == "ui" and request["action"]["type"] in {"pick_slot", "confirm_proposal"}
        ) or chat.metrics.get("path") == "ui_action"
        if self.error in {"session_expired", "interaction_expired"}:
            chat.clear_context()
        elif self.error in _UNVERIFIED_WRITES:
            chat.clear_uncertain_proposal()
        elif slot_action and self.error == "slot_taken":
            recovered = {ref: self.slots[ref] for ref in self.recovery_refs}
            chat.slots = _recent({**chat.slots, **recovered}, MAX_SLOTS)
            chat.choices = {number: ref for ref, number in self.numbers.items()
                            if ref in self.recovery_refs and ref in chat.slots}
            chat.proposal = None
        elif success:
            chat.slots = _recent(self.slots, MAX_SLOTS)
            chat.bookings = _recent(self.bookings, MAX_BOOKINGS)
            chat.choices = {number: ref for ref, number in self.numbers.items() if ref in chat.slots}
            for ref in dict.fromkeys(self.listed):
                booking = self.bookings.get(ref)
                if booking is not None and (self.result is None or ref != self.result[1]["ref"]):
                    guest = f" · {booking['guestName']}" if booking.get("guestName") else ""
                    chat.write(f"[{ref}] {booking['title']} · {booking['whenLabel']} · {booking['status']}{guest}")
            if self.result is not None:
                action, booking = self.result
                if booking["status"] in _ACTIVE:
                    chat.latest_booking = booking["ref"]
                elif chat.latest_booking == booking["ref"]:
                    chat.latest_booking = None
                chat.write(f"{_RESULT_LABELS[action]} · {booking['title']} · {booking['whenLabel']}")
                if chat.latest_booking:
                    chat.write("[r] Reschedule  [c] Cancel")
            if self.proposal is not None:
                chat.proposal = self.proposal
                proposal = self.proposal
                chat.write(_PROPOSAL_TITLES[proposal["kind"]])
                chat.write(f"{proposal['title']} · {proposal['whenLabel']} · {proposal['durationMinutes']} min · {proposal['timeZone']}")
                guest = proposal.get("name") or self.bookings.get(proposal.get("bookingRef", ""), {}).get("guestName")
                if guest:
                    chat.write(f"Guest: {guest}")
                chat.write("Type yes or no. [y/n]")
            elif self.result is not None or _decision(request) == "dismiss":
                chat.proposal = None
        if self.retryable and self.error not in _UNVERIFIED_WRITES and self.result is None:
            chat.offer_retry(request)


class TerminalChat:
    def __init__(self, service: TurnService, time_zone: str, *, read: Callable[[str], str], output: TextIO) -> None:
        self.service = service
        self.time_zone = time_zone
        self.read = read
        self.output = output
        self.color = (output.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
                      and (os.name != "nt" or bool(os.environ.get("WT_SESSION") or os.environ.get("ANSICON"))))
        self.token: str | None = None
        self.metrics: TurnMetrics | None = None
        self.next_choice = 1
        self.clear_context()

    def clear_context(self) -> None:
        self.slots: dict[str, SlotView] = {}
        self.bookings: dict[str, BookingView] = {}
        self.choices: dict[int, str] = {}
        self.proposal: ProposalView | None = None
        self.latest_booking: str | None = None
        self.retry_input: dict | None = None

    def clear_uncertain_proposal(self) -> None:
        if self.proposal is not None:
            slot_ref = self.proposal.get("slotRef")
            if slot_ref:
                self.slots.pop(slot_ref, None)
                self.choices = {number: ref for number, ref in self.choices.items() if ref != slot_ref}
            if self.latest_booking == self.proposal.get("bookingRef"):
                self.latest_booking = None
        self.proposal = None

    def offer_retry(self, request: dict) -> None:
        self.retry_input = request
        self.write("Type Retry to try again.")

    def write(self, text: str, *, end: str = "\n", dim: bool = False) -> None:
        text = _CONTROL.sub("", text)
        if dim and self.color:
            text = f"\x1b[2m{text}\x1b[0m"
        print(text, end=end, file=self.output, flush=True)

    async def send(self, source: dict) -> None:
        self.retry_input = None
        request = ChatRequest.model_validate({"stateToken": self.token, "timeZone": self.time_zone, "input": source})
        display = _TurnDisplay(self)
        terminal = None
        async with aclosing(self.service.events(request)) as events:
            async for event in events:
                if event["type"] == "turn_end":
                    terminal = event
                else:
                    display.show(event)
        display.finish_text()
        if terminal is None:
            if _decision(source) == "confirm":
                self.clear_uncertain_proposal()
                self.write("Check your calendar to see whether the change went through.")
            else:
                self.write("The chat ended before saving this turn.")
                self.offer_retry(source)
            return
        display.commit(terminal, source)
        if display.booker and display.error not in _UNVERIFIED_WRITES:
            self.write(f"Open Cal.com bookings: {_CAL_BOOKINGS_URL}")

    def _input(self, text: str) -> dict | None:
        if text.casefold() == "retry" and self.retry_input is not None:
            return self.retry_input
        if self.proposal is not None and text.casefold() in {"y", "n", "/cancel"}:
            action = "confirm_proposal" if text.casefold() == "y" else "dismiss_proposal"
            return _ui(action, proposalId=self.proposal["id"])
        if text.isdecimal():
            ref = self.choices.get(int(text)) if len(text) <= 8 else None
            if ref is not None:
                return _ui("pick_slot", slotRef=ref)
            self.write("Choose a displayed time option.")
            return None
        if text.casefold() in {"r", "c"}:
            if self.latest_booking is None:
                self.write("Use a chat message to identify the booking you want to change.")
                return None
            action = "request_reschedule" if text.casefold() == "r" else "request_cancel"
            return _ui(action, bookingRef=self.latest_booking)
        if len(text) > 2_000:
            self.write("Use a message of 2,000 characters or fewer.")
            return None
        source = {"kind": "text", "text": text}
        if self.proposal is not None:
            source["proposalId"] = self.proposal["id"]
        return source

    def _command(self, text: str) -> None:
        command, _, value = text.partition(" ")
        if command == "/new" and not value:
            self.token = self.metrics = None
            self.clear_context()
            self.write("New chat started.")
        elif command == "/tz" and value:
            try:
                self.time_zone = validate_time_zone(value.strip())
            except ValueError:
                self.write("Enter an IANA time zone, for example /tz Asia/Singapore.")
            else:
                self.proposal = None
                self.choices.clear()
                self.retry_input = None
                self.write(f"Time zone: {self.time_zone}")
        elif command == "/debug" and not value:
            self.write(json.dumps(self.metrics, indent=2) if self.metrics is not None else "No turn metrics yet.")
        else:
            self.write(_COMMANDS)

    def run(self, runner: asyncio.Runner) -> None:
        while True:
            choice = self.read("You: ").strip()
            if choice == "/quit":
                return
            if choice.startswith("/") and not (self.proposal is not None and choice.casefold() == "/cancel"):
                self._command(choice)
            elif choice and (source := self._input(choice)) is not None:
                runner.run(self.send(source))


def run_chat(service: TurnService, *, read: Callable[[str], str] = input, output: TextIO | None = None,
             time_zone: str | None = None) -> int:
    try:
        profile = service.calendar.me()
    except CalError:
        profile = {"username": "Scheduling Assistant"}
    chat = TerminalChat(service, time_zone or detect_time_zone(profile.get("timeZone")),
                        read=read, output=output if output is not None else sys.stdout)
    chat.write(f"{profile.get('name') or profile['username']} · {chat.time_zone}")
    chat.write(_COMMANDS)
    with asyncio.Runner() as runner:
        try:
            chat.run(runner)
        except (EOFError, KeyboardInterrupt):
            chat.write("")
        finally:
            try:
                close_model = getattr(service, "close_models", None) or getattr(service.model, "aclose", None)
                if close_model is not None:
                    runner.run(close_model())
            finally:
                service.close_prefetch()
    return 0


def main() -> int:
    try:
        service = load_service(persistent_model=True)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    return run_chat(service)


if __name__ == "__main__":
    raise SystemExit(main())
