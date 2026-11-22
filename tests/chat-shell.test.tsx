import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";

import { ConfirmCard, SlotChips } from "../app/chat-cards";
import Home from "../app/page";

it("renders the chat with an enabled, labeled composer and an empty draft", () => {
  const html = renderToStaticMarkup(createElement(Home));

  expect(html).toContain("<main ");
  expect(html).toContain('id="chat-title"');
  expect(html).toContain('for="chat-message"');
  expect(html).toContain('role="log"');
  expect(html).toMatch(/<textarea\b[^>]*\bid="chat-message"[^>]*>/);
  expect(html).not.toMatch(/<textarea\b[^>]*\bdisabled=""/);
  expect(html).toMatch(/<button\b[^>]*\btype="submit"[^>]*\bdisabled=""[^>]*>Send<\/button>/);
});

it("renders grouped date-labeled chips and greys out unavailable references", () => {
  const html = renderToStaticMarkup(createElement(SlotChips, {
    refs: ["S1", "S2"],
    slots: {
      S1: { dayLabel: "Thu 26 Nov", timeLabel: "2:30 pm", startLocalIso: "2026-11-26T14:30:00Z" },
      S2: { dayLabel: "Fri 27 Nov", timeLabel: "9:00 am", startLocalIso: "2026-11-27T09:00:00Z" },
    },
    available: (ref) => ref === "S1",
    allowMore: true,
    onAction: () => {},
  }));

  expect(html).toContain('aria-label="Thu 26 Nov at 2:30 pm"');
  expect(html).not.toMatch(/aria-label="Thu 26 Nov at 2:30 pm"[^>]*disabled/);
  expect(html).toMatch(/aria-label="Fri 27 Nov at 9:00 am"[^>]*disabled=""/);
  expect(html).toContain("More times");
});

it("uses a contact-free confirmation for moving a booking", () => {
  const html = renderToStaticMarkup(createElement(ConfirmCard, {
    proposal: {
      id: "P2", kind: "reschedule", title: "Synthetic meeting", whenLabel: "Fri 27 Nov, 9:00 am",
      timeZone: "America/Los_Angeles", durationMinutes: 30, bookingRef: "B1", slotRef: "S2",
    },
    active: false,
    submitting: false,
    onAction: () => {},
  }));

  expect(html).toContain("Move this meeting?");
  expect(html).toMatch(/<fieldset\b[^>]*disabled=""/);
  expect(html).not.toContain('name="email"');
});
