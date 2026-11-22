import { expect, it } from "vitest";

import { type ServerEvent, displayReferences, readChatStream } from "../app/chat-protocol";

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

it("displays slots using their explicit date and keeps the reference for text selection", () => {
  expect(displayReferences("I can offer [[S2]] or [[S3]].", {
    bookings: {},
    slots: {
      S2: { dayLabel: "Thu 26 Nov", timeLabel: "2:30 pm", startLocalIso: "2026-11-26T14:30:00Z" },
    },
  })).toBe("I can offer Thu 26 Nov at 2:30 pm (S2) or S3.");
});
