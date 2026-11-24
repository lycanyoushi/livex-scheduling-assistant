import { expect, it } from "vitest";

import { calendarFile, googleCalendarLink, safeMeetingLink } from "../app/calendar-export";
import { type BookingView, type ChatInput, type ServerEvent, canRetryTurn, controlsForTurn, nextSlotDate, readChatStream, splitReferenceText, unverifiedWrite } from "../app/chat-protocol";

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

it("permits only terminal alternatives after a slot-taken UI action", () => {
  const failed = { outcome: "failed" } as const;
  expect(controlsForTurn({ kind: "text", text: "tomorrow" }, failed, "slot_taken")).toBe("none");
  expect(controlsForTurn({ kind: "ui", action: { type: "confirm_proposal", proposalId: "P1" } }, failed, "slot_taken")).toBe("alternatives");
  expect(controlsForTurn({ kind: "ui", action: { type: "pick_slot", slotRef: "S1" } }, failed, "slot_taken")).toBe("alternatives");
  expect(controlsForTurn({ kind: "ui", action: { type: "confirm_proposal", proposalId: "P1" } }, failed, "cal_down")).toBe("none");
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
