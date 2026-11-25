import { expect, it } from "vitest";

import { calendarFile, googleCalendarLink, safeMeetingLink } from "../app/calendar-export";
import { type BookingView, type ChatInput, type ProposalView, type ServerEvent, canRetryTurn, clearsProposalOnSuccess, controlsForTurn, listedBookingRefs, nextSlotDate, proposalDecision, proposalWasRetired, readChatStream, splitReferenceText, typedInput, unverifiedWrite, withoutRetiredRefs } from "../app/chat-protocol";

const encoder = new TextEncoder();

it("emits complete NDJSON lines immediately and handles split UTF-8 characters", async () => {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  const events: ServerEvent[] = [];
  const firstEvent = Promise.withResolvers<ServerEvent>();
  const finished = readChatStream(stream, (event) => {
    events.push(event);
    firstEvent.resolve(event);
  });

  controller.enqueue(encoder.encode('{"type":"status","text":"Checking availability"}\r\n'));
  await expect(firstEvent.promise).resolves.toEqual({ type: "status", text: "Checking availability" });

  const delta = encoder.encode('{"type":"text_delta","text":"Mañana a las 9"}\n');
  const cut = delta.indexOf(0xc3) + 1;
  controller.enqueue(delta.slice(0, cut));
  controller.enqueue(delta.slice(cut));
  controller.enqueue(encoder.encode('{"type":"turn_end","stateToken":"updated.signature","metrics":{"outcome":"ok"}}\n'));
  controller.close();

  await expect(finished).resolves.toMatchObject({ stateToken: "updated.signature" });
  expect(events).toEqual([
    { type: "status", text: "Checking availability" },
    { type: "text_delta", text: "Mañana a las 9" },
  ]);
});

it("rejects an interrupted response without a terminal state update", async () => {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode('{"type":"text_delta","text":"Partial response"}\n'));
      controller.close();
    },
  });

  await expect(readChatStream(stream, () => {})).rejects.toThrow("before saving the turn");
});

it("buffers incomplete reference syntax and preserves only complete chips while streaming", () => {
  expect(splitReferenceText("I can offer [[S2]] or [[S3", true)).toEqual([
    { text: "I can offer " }, { ref: "S2" }, { text: " or " },
  ]);
  expect(splitReferenceText("Try [", true)).toEqual([{ text: "Try " }]);
  expect(splitReferenceText("Then [[B2]].", false)).toEqual([{ text: "Then " }, { ref: "B2" }, { text: "." }]);
});

it("keeps explicit booking cards in order and removes retired references from the current view", async () => {
  const booking: BookingView = {
    ref: "B1", title: "Fixture booking", whenLabel: "Thu 26 Nov, 2:30 pm", status: "accepted", attendees: [],
    startUtc: "2026-11-26T14:30:00Z", endUtc: "2026-11-26T15:00:00Z",
  };
  const inventory = { B1: booking, B2: { ...booking, ref: "B2", title: "Another fixture booking" } };
  const events: ServerEvent[] = [
    { type: "refs", bookings: inventory },
    { type: "booking_cards", refs: ["B1", "B2", "B3"] },
    { type: "refs", retiredBookings: ["B1"] },
    { type: "turn_end", stateToken: "fixture.signature", metrics: { outcome: "ok" } },
  ];
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const event of events) controller.enqueue(encoder.encode(`${JSON.stringify(event)}\n`));
      controller.close();
    },
  });
  let refs: string[] | undefined;
  let current: Record<string, BookingView> = {};
  await readChatStream(body, (event) => {
    if (event.type === "refs") {
      current = withoutRetiredRefs({ ...current, ...event.bookings }, new Set(event.retiredBookings));
      if (!refs) expect(listedBookingRefs(refs, current)).toEqual([]);
    }
    if (event.type === "booking_cards") {
      refs = event.refs;
      expect(listedBookingRefs(refs, current)).toEqual(["B1", "B2"]);
    }
  });

  expect(listedBookingRefs(refs, current)).toEqual(["B2"]);
  expect(listedBookingRefs(refs, inventory)).toEqual(["B1", "B2"]);
  expect(listedBookingRefs([], inventory)).toEqual([]);
});

it("keeps retired slot labels in history and only retires the named proposal", async () => {
  const slot = { dayLabel: "Thu 26 Nov", timeLabel: "2:30 pm", startLocalIso: "2026-11-26T14:30:00Z" };
  const history = { S1: slot, S2: { ...slot, timeLabel: "3:30 pm", startLocalIso: "2026-11-26T15:30:00Z" } };
  const proposal: ProposalView = { id: "P1", kind: "book", title: "Fixture meeting", whenLabel: "Thu 26 Nov, 2:30 pm",
    timeZone: "UTC", durationMinutes: 30, slotRef: "S1" };
  const unrelated = { ...proposal, id: "P2", slotRef: "S2" };
  const retirement: ServerEvent = { type: "refs", retiredSlots: ["S1"], retiredProposal: "P1" };
  const response = new Response([
    JSON.stringify(retirement),
    JSON.stringify({ type: "turn_end", stateToken: "fixture.signature", metrics: { outcome: "ok" } }),
  ].join("\n"));
  let received = false;
  await readChatStream(response.body!, (event) => {
    if (event.type !== "refs") return;
    received = true;
    expect(withoutRetiredRefs(history, new Set(event.retiredSlots))).toEqual({ S2: history.S2 });
    expect(history.S1.timeLabel).toBe("2:30 pm");
    const retiredIds = new Set(event.retiredProposal ? [event.retiredProposal] : []);
    expect(proposalWasRetired(proposal, retiredIds, new Set())).toBe(true);
    expect(proposalWasRetired(unrelated, retiredIds, new Set())).toBe(false);
  });
  expect(received).toBe(true);
});

it("permits only terminal alternatives after a slot-taken UI action", () => {
  const failed = { outcome: "failed" } as const;
  expect(controlsForTurn({ kind: "text", text: "tomorrow" }, failed, "slot_taken")).toBe("none");
  expect(controlsForTurn({ kind: "ui", action: { type: "confirm_proposal", proposalId: "P1" } }, failed, "slot_taken")).toBe("alternatives");
  expect(controlsForTurn({ kind: "ui", action: { type: "pick_slot", slotRef: "S1" } }, failed, "slot_taken")).toBe("alternatives");
  expect(controlsForTurn({ kind: "ui", action: { type: "confirm_proposal", proposalId: "P1" } }, failed, "cal_down")).toBe("none");
});

it("sends recognized replies as text with only the active proposal ID attached", () => {
  expect(JSON.stringify(typedInput("yes please", "P1"))).toBe('{"kind":"text","text":"yes please","proposalId":"P1"}');
  expect(JSON.stringify(typedInput("yes please"))).toBe('{"kind":"text","text":"yes please"}');
  for (const text of [
    " YES!. ", "yeah", "yep", "yup", "sure", "ok", "okay", "CONFIRM", "Book it.", "Sounds good",
    "Go ahead!", "that works", "perfect", "do it", "please do", "yes please", "confirm please", "go ahead please",
    "Yeah, thank you, confirm it, book it, go ahead!", "  sure,\tthanks, please.  ",
  ]) {
    expect(proposalDecision(typedInput(text, "P1")), text).toBe("confirm");
  }
  for (const text of ["No.", "nope", "no thanks", "No, thanks!", " Never mind! ", "Don’t!"]) {
    expect(proposalDecision(typedInput(text, "P1")), text).toBe("dismiss");
  }
  for (const text of [
    "yes?", "yes, thanks?", "confirm it", "sounds good, but Friday", "go ahead instead", "don't book it",
    "yes at 3pm", "yes, tomorrow", "ok 11/24", "sure Friday", "cancel that", "no thanks please", "surely",
    "yes" + " please".repeat(20),
  ]) {
    expect(typedInput(text, "P1"), text).toEqual({ kind: "text", text });
  }
});

it("keeps authoritative code-only fallback controls usable on a failed model turn", () => {
  const input: ChatInput = { kind: "text", text: "Find an opening tomorrow" };
  expect(controlsForTurn(input, { outcome: "failed", fallback: "code_only" }, "model_down")).toBe("all");
  expect(controlsForTurn(input, { outcome: "failed", fallback: "backup_model" }, "model_down")).toBe("none");
});

it("retries verified rejections and never replays an interrupted confirmation", () => {
  const confirm: ChatInput = { kind: "ui", action: { type: "confirm_proposal", proposalId: "P1" } };
  expect(canRetryTurn(confirm, { kind: "quota" })).toBe(true);
  expect(canRetryTurn(confirm, { kind: "terminal", retryable: true })).toBe(true);
  expect(canRetryTurn(confirm, { kind: "terminal", retryable: false })).toBe(false);
  expect(canRetryTurn(confirm, { kind: "transport" })).toBe(false);
  expect(canRetryTurn({ kind: "ui", action: { type: "pick_slot", slotRef: "S1" } }, { kind: "transport" })).toBe(true);
  expect(canRetryTurn({ kind: "text", text: "Tomorrow" }, { kind: "transport" })).toBe(true);
  expect(unverifiedWrite("write_unverified")).toBe(true);
  expect(unverifiedWrite("reschedule_partial")).toBe(true);
  expect(unverifiedWrite("write_not_applied")).toBe(false);
});

it("gives a typed confirmation the same transport and taken-slot safety as the button", () => {
  const confirm = typedInput("that works, please!", "P1");
  const dismiss = typedInput("never mind", "P1");
  const failed = { outcome: "failed", path: "ui_action" } as const;
  expect(canRetryTurn(confirm, { kind: "transport" })).toBe(false);
  expect(canRetryTurn(confirm, { kind: "quota" })).toBe(true);
  expect(canRetryTurn(confirm, { kind: "terminal", retryable: false })).toBe(false);
  expect(canRetryTurn(dismiss, { kind: "transport" })).toBe(true);
  expect(canRetryTurn(typedInput("yes"), { kind: "transport" })).toBe(true);
  expect(controlsForTurn(confirm, failed, "slot_taken")).toBe("alternatives");
  expect(controlsForTurn(dismiss, failed, "slot_taken")).toBe("none");
  expect(controlsForTurn(confirm, failed, "write_unverified")).toBe("none");
});

it("sends guest details and slot picks as ordinary chat text", () => {
  for (const text of ["Nova Fixture, nova@example.com", "the second one", "2:30 works"]) {
    expect(typedInput(text, "P1")).toEqual({ kind: "text", text });
  }
  expect(controlsForTurn(typedInput("the second one"), { outcome: "failed", path: "ui_action" }, "slot_taken"))
    .toBe("alternatives");
  expect(controlsForTurn(typedInput("the second one"), { outcome: "failed", path: "model" }, "slot_taken"))
    .toBe("none");
});

it("keeps the original card active when the server asks for missing guest details", async () => {
  const response = new Response([
    JSON.stringify({ type: "assistant_text", text: "What is the guest's email address?" }),
    JSON.stringify({ type: "turn_end", stateToken: "fixture.signature", metrics: { path: "ui_action", outcome: "ok" } }),
    "",
  ].join("\n"));
  const terminal = await readChatStream(response.body!, () => {});
  expect(clearsProposalOnSuccess(typedInput("yes", "P1"), terminal.metrics.path)).toBe(false);
  expect(clearsProposalOnSuccess(
    { kind: "ui", action: { type: "confirm_proposal", proposalId: "P1" } }, terminal.metrics.path,
  )).toBe(false);
  expect(clearsProposalOnSuccess(typedInput("no", "P1"), terminal.metrics.path)).toBe(true);
  expect(clearsProposalOnSuccess(typedInput("yes?", "P1"), "model")).toBe(false);
});

it("starts the next availability window after the final local date", () => {
  expect(nextSlotDate([
    { dayLabel: "Fri 27 Nov", timeLabel: "11:30 pm", startLocalIso: "2026-11-27T23:30:00-08:00" },
    { dayLabel: "Thu 26 Nov", timeLabel: "2:30 pm", startLocalIso: "2026-11-26T14:30:00-08:00" },
  ])).toBe("2026-11-28");
});

it("exports valid UTC calendar times, escapes event text and excludes unsafe join links", () => {
  const booking: BookingView = {
    ref: "B1",
    title: `Prep, Q&A; follow-up\n${"会".repeat(38)}`,
    whenLabel: "Thu 26 Nov, 9:30 am",
    status: "accepted",
    attendees: ["sam@example.com"],
    startUtc: "2026-11-26T17:30:00Z",
    endUtc: "2026-11-26T18:00:00Z",
    meetingUrl: "https://meet.example.com/synthetic",
  };
  const google = new URL(googleCalendarLink(booking)!);
  expect(google.searchParams.get("dates")).toBe("20261126T173000Z/20261126T180000Z");
  expect(google.searchParams.get("text")).toBe(booking.title);
  const ics = calendarFile(booking, new Date("2026-11-22T00:00:00Z"))!;
  expect(ics.replace(/\r\n /g, "")).toContain("SUMMARY:Prep\\, Q&A\\; follow-up\\n");
  expect(ics).toContain("\r\nDTSTAMP:20261122T000000Z\r\nDTSTART:20261126T173000Z\r\nDTEND:20261126T180000Z\r\n");
  expect(ics.split("\r\n").every((line) => encoder.encode(line).length <= 75)).toBe(true);
  expect(safeMeetingLink("javascript:alert(1)")).toBeUndefined();
});
