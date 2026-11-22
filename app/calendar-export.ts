import type { BookingView } from "./chat-protocol";

function calendarTime(date: Date): string {
  return date.toISOString().replace(/[-:]|\.\d{3}/g, "");
}

function bookingRange(booking: BookingView): [string, string] | null {
  const start = new Date(booking.startUtc);
  const end = new Date(booking.endUtc);
  return start < end ? [calendarTime(start), calendarTime(end)] : null;
}

export function safeMeetingLink(value?: string): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : undefined;
  } catch {
    return undefined;
  }
}

export function googleCalendarLink(booking: BookingView): string | undefined {
  const range = bookingRange(booking);
  if (!range) return undefined;
  const params = new URLSearchParams({
    action: "TEMPLATE",
    text: booking.title,
    dates: range.join("/"),
  });
  const meeting = safeMeetingLink(booking.meetingUrl);
  if (meeting) params.set("location", meeting);
  return `https://calendar.google.com/calendar/render?${params}`;
}

function escapeCalendarText(value: string): string {
  return value.replace(/\\/g, "\\\\").replace(/\r\n|\r|\n/g, "\\n").replace(/[,;]/g, "\\$&");
}

function foldCalendarLine(value: string): string {
  const encoder = new TextEncoder();
  const folded: string[] = [];
  let line = "";
  let octets = 0;
  for (const character of value) {
    const size = encoder.encode(character).length;
    if (octets + size > 75) {
      folded.push(line);
      line = " ";
      octets = 1;
    }
    line += character;
    octets += size;
  }
  return [...folded, line].join("\r\n");
}

export function calendarFile(booking: BookingView, exportedAt: Date): string | undefined {
  const range = bookingRange(booking);
  if (!range) return undefined;
  const [start, end] = range;
  const meeting = safeMeetingLink(booking.meetingUrl);
  const lines = [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//LiveX Scheduling Assistant//EN",
    "CALSCALE:GREGORIAN",
    "BEGIN:VEVENT",
    `UID:${escapeCalendarText(`${booking.ref}-${start}-${end}@livex-scheduling`)}`,
    `DTSTAMP:${calendarTime(exportedAt)}`,
    `DTSTART:${start}`,
    `DTEND:${end}`,
    `SUMMARY:${escapeCalendarText(booking.title)}`,
    ...(meeting ? [`URL:${meeting}`] : []),
    "END:VEVENT",
    "END:VCALENDAR",
  ];
  return `${lines.map(foldCalendarLine).join("\r\n")}\r\n`;
}
