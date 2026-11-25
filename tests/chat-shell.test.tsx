import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";

import { BookerFallback, calEmbedLink, safePublicBookingUrl } from "../app/booker-fallback";
import { BookingCard, ConfirmCard, RefText, SlotChips } from "../app/chat-cards";
import ChatWorkspace from "../app/chat-workspace";
import Home from "../app/page";

it("renders the chat with an enabled, labeled composer and an empty draft", () => {
  const html = renderToStaticMarkup(createElement(ChatWorkspace));

  expect(html).toContain("<main ");
  expect(html).toContain('id="chat-title"');
  expect(html).toContain('for="chat-message"');
  expect(html).toContain('role="log"');
  expect(html).toMatch(/<textarea\b[^>]*\bid="chat-message"[^>]*>/);
  expect(html).not.toMatch(/<textarea\b[^>]*\bdisabled=""/);
  expect(html).toMatch(/<button\b(?=[^>]*\btype="submit")(?=[^>]*\baria-label="Send")(?=[^>]*\bdisabled="")[^>]*>/);
});

it("shows only the access gate while checking for a signed-in owner", () => {
  const html = renderToStaticMarkup(createElement(Home));
  expect(html).toContain("Owner access</h1>");
  expect(html).toMatch(/role="status"[^>]*>Checking access…/);
  expect(html).not.toContain('role="log"');
  expect(html).not.toContain("<textarea");
});

it("renders grouped date-labeled chips and greys out unavailable references", () => {
  const html = renderToStaticMarkup(createElement(SlotChips, {
    refs: ["S1", "S2"],
    plainRefs: ["S3"],
    slots: {
      S1: { dayLabel: "Thu 26 Nov", timeLabel: "2:30 pm", startLocalIso: "2026-11-26T14:30:00Z" },
      S2: { dayLabel: "Fri 27 Nov", timeLabel: "9:00 am", startLocalIso: "2026-11-27T09:00:00Z" },
      S3: { dayLabel: "Sat 28 Nov", timeLabel: "11:00 am", startLocalIso: "2026-11-28T11:00:00Z" },
    },
    available: (ref) => ref === "S1",
    allowMore: true,
    onAction: () => {},
  }));

  expect(html).toContain('aria-label="Thu 26 Nov at 2:30 pm"');
  expect(html).not.toMatch(/aria-label="Thu 26 Nov at 2:30 pm"[^>]*disabled/);
  expect(html).toMatch(/aria-label="Fri 27 Nov at 9:00 am"[^>]*disabled=""/);
  expect(html).toContain("Sat 28 Nov");
  expect(html).toContain(">11:00 am</span>");
  expect(html).not.toContain('aria-label="Sat 28 Nov at 11:00 am"');
  expect(html).toContain("More times");
});

it("marks the selected opening while leaving other available times usable", () => {
  const props = {
    refs: ["S1", "S2"], selectedRef: "S1", note: "Openings across the requested dates",
    slots: {
      S1: { dayLabel: "Thu 26 Nov", timeLabel: "2:30 pm", startLocalIso: "2026-11-26T14:30:00-08:00" },
      S2: { dayLabel: "Thu 26 Nov", timeLabel: "3:30 pm", startLocalIso: "2026-11-26T15:30:00-08:00" },
    },
    available: () => true, allowMore: true, onAction: () => {},
  };
  const html = renderToStaticMarkup(createElement(SlotChips, { ...props, timeZone: "America/Los_Angeles" }));

  expect(html).toContain("Available times</h2>");
  expect(html).toContain("America/Los_Angeles</p>");
  expect(html).not.toContain("Openings across the requested dates");
  expect(html).toMatch(/aria-label="Thu 26 Nov at 2:30 pm" aria-pressed="true"/);
  expect(html).toMatch(/aria-label="Thu 26 Nov at 3:30 pm" aria-pressed="false"/);
  expect(html).not.toMatch(/aria-label="Thu 26 Nov at 3:30 pm"[^>]*disabled=""/);
  expect(html).toContain("text-xs tabular-nums");
  expect(html).toContain("lucide-check");
  expect(html).toContain("lucide-chevron-right");
  const historical = renderToStaticMarkup(createElement(SlotChips, { ...props, timeZone: "Asia/Singapore" }));
  expect(historical).toContain("UTC-08:00</p>");
  expect(historical).not.toContain("Asia/Singapore");
});

it("leaves explicit booking mentions as text and disables controls on historical cards", () => {
  const view = {
    ref: "B2", title: "Synthetic meeting", whenLabel: "Fri 27 Nov, 9:00 am", status: "accepted", attendees: [],
    meetingUrl: "https://meet.example.com/fixture",
    startUtc: "2026-11-27T09:00:00Z", endUtc: "2026-11-27T09:30:00Z",
  };
  const inline = {
    parts: [{ text: "See " }, { ref: "B2" }],
    views: { slots: {}, bookings: { B2: view } },
    slotAvailable: () => false,
    showMore: false,
    onAction: () => {},
  };
  const explicit = renderToStaticMarkup(createElement(RefText, {
    ...inline, currentBookings: { B2: view }, listedBookings: ["B1", "B2"], bookingAvailable: () => true,
  }));
  expect(explicit).toContain(">B2</span>");
  expect(explicit).not.toContain("<article");

  const historical = renderToStaticMarkup(createElement(RefText, {
    ...inline, currentBookings: {}, listedBookings: [], bookingAvailable: () => false,
  }));
  expect(historical).toContain("Synthetic meeting");
  expect(historical).toMatch(/<button\b[^>]*disabled=""[^>]*>Reschedule<\/button>/);
  expect(historical).toMatch(/<button\b[^>]*disabled=""[^>]*>Cancel<\/button>/);
  expect(historical).not.toContain("meet.example.com");
  expect(historical).not.toContain("Add to calendar");
});

it("summarizes the guest and both times on an inactive move proposal", () => {
  const html = renderToStaticMarkup(createElement(ConfirmCard, {
    proposal: {
      id: "P2", kind: "reschedule", title: "Synthetic meeting", whenLabel: "Fri 27 Nov, 9:00 am",
      timeZone: "America/Los_Angeles", durationMinutes: 30, bookingRef: "B1", slotRef: "S2",
    },
    originalBooking: {
      ref: "B1", title: "Synthetic meeting", whenLabel: "Thu 26 Nov, 8:00 am", status: "accepted",
      guestName: "Nova Fixture", attendees: ["fixture@example.com"],
      startUtc: "2026-11-26T16:00:00Z", endUtc: "2026-11-26T16:30:00Z",
    },
    selectedSlot: { dayLabel: "Fri 27 Nov", timeLabel: "9:00 am", startLocalIso: "2026-11-27T17:00:00Z" },
    active: false,
    submitting: false,
    onAction: () => {},
  }));

  expect(html).toContain("Move this meeting?");
  expect(html).toContain(">Move meeting</h2>");
  expect(html).toContain(">From</p>");
  expect(html).toContain(">To</p>");
  expect(html).toContain("8:00 am");
  expect(html).toContain("9:00 am · 30 min");
  expect(html).toContain("Nova Fixture · fixture@example.com");
  expect(html).toMatch(/<button\b[^>]*disabled=""[^>]*>Confirm<\/button>/);
  expect(html).toMatch(/<button\b[^>]*disabled=""[^>]*>Not now<\/button>/);
  expect(html).not.toMatch(/<(form|input|textarea)\b/);
});

it("shows a booking summary with the guest details collected in chat", () => {
  const proposal = { id: "P2", kind: "book" as const, title: "Synthetic meeting", whenLabel: "Fri 27 Nov, 9:00 am",
    timeZone: "UTC", durationMinutes: 30, slotRef: "S2" };
  const render = (details: { name?: string; email?: string; notes?: string }) => renderToStaticMarkup(createElement(ConfirmCard, {
    proposal: { ...proposal, ...details },
    active: true, submitting: false, onAction: () => {},
  }));
  const html = render({ name: "Nova Fixture", email: "nova@example.com", notes: "Candidate intro" });

  expect(html).toMatch(/^<section\b[^>]*aria-labelledby="[^"]+"/);
  expect(html).toContain('aria-label="Book this meeting?">Confirm booking</h2>');
  expect(html).toContain("Nova Fixture · nova@example.com");
  expect(html).toContain("Candidate intro");
  expect(html).toContain(">Confirm</button>");
  expect(html).toContain(">Not now</button>");
  expect(html).toContain("You can also type yes or no in chat.");
  expect(html).not.toMatch(/<(form|input|textarea)\b/);

  const incomplete = render({ name: "Nova Fixture" });
  expect(incomplete).toContain("Nova Fixture");
  expect(incomplete).not.toMatch(/<(form|input|textarea)\b/);
  expect(incomplete).not.toContain("You can also type yes or no in chat.");
});

it("uses status colors, a booking banner and a calendar dropdown", () => {
  const booking = { ref: "B1", title: "Fixture meeting", whenLabel: "Fri 27 Nov, 9:00 am", status: "accepted",
    attendees: [], startUtc: "2026-11-27T09:00:00Z", endUtc: "2026-11-27T09:30:00Z" };
  const render = (status: string) => renderToStaticMarkup(createElement(BookingCard, {
    booking: { ...booking, status }, available: true, linksAvailable: true, onAction: () => {},
    result: status === "accepted" ? "booked" : undefined,
  }));

  const accepted = render("accepted");
  expect(accepted).toContain("Meeting booked</h2>");
  expect(accepted).toContain('text-emerald-300">Confirmed</span>');
  expect(accepted).toContain("lucide-calendar-days");
  expect(accepted).toContain(">9:00 am</span>");
  expect(accepted).toContain('aria-expanded="false"');
  expect(accepted).toContain("Add to calendar");
  expect(accepted).toContain("Google Calendar</a>");
  expect(accepted).toContain("Download .ics</button>");
  expect(accepted).not.toContain("<details");
  expect(render("pending")).toContain('text-amber-300">Pending</span>');
  expect(render("cancelled")).toContain('text-red-300">Cancelled</span>');
  expect(render("cancelled")).not.toContain("Add to calendar");
});

it("renders safe bullets, bold and line breaks around clickable slot references", () => {
  const html = renderToStaticMarkup(createElement(RefText, {
    parts: [{ text: "**Choose a time:**\n- **Morning: " }, { ref: "S1" },
      { text: "**\n- <img src=x onerror=alert(1)>\n\nLast line." }],
    views: { slots: { S1: { dayLabel: "Thu 26 Nov", timeLabel: "9:00 am", startLocalIso: "2026-11-26T09:00:00Z" } }, bookings: {} },
    currentBookings: {}, listedBookings: [], slotAvailable: () => true, bookingAvailable: () => false,
    showMore: false, onAction: () => {},
  }));

  expect(html).toContain("<strong>Choose a time:</strong>");
  expect(html).toContain("<li><strong>Morning: </strong><button");
  expect(html).toContain('aria-label="Thu 26 Nov at 9:00 am"');
  expect(html).toContain("min-h-11");
  expect(html).toContain("&lt;img src=x onerror=alert(1)&gt;");
  expect(html).not.toContain("<img");
  expect(html).not.toContain("leading-10");
  expect(html).toContain("<div><span>Last line.</span></div>");
});

it("provides a regular safe booking link and leaves the Cal.com script unloaded on the server", () => {
  const url = "https://cal.com/fixture-host/intro?email=nova@example.com#private";
  expect(safePublicBookingUrl(url)).toBe("https://cal.com/fixture-host/intro");
  expect(calEmbedLink(url)).toBe("fixture-host/intro");
  const html = renderToStaticMarkup(createElement(BookerFallback, {
    bookingUrl: url, warming: false, open: true, onClose: () => {},
  }));
  expect(html).toContain('aria-label="Book directly"');
  expect(html).toContain('href="https://cal.com/fixture-host/intro"');
  expect(html).not.toContain("nova@example.com");
  expect(html).not.toContain("<script");
  expect(calEmbedLink("https://booking.example.com/fixture/intro")).toBeNull();
  expect(safePublicBookingUrl("javascript:alert(1)")).toBeNull();
  expect(safePublicBookingUrl("https://fixture@cal.com/fixture/intro")).toBeNull();
});
