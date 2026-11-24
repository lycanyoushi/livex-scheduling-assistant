export type UiAction =
  | { type: "pick_slot"; slotRef: string }
  | { type: "more_slots"; fromDate?: string }
  | { type: "confirm_proposal"; proposalId: string; name?: string; email?: string; notes?: string }
  | { type: "dismiss_proposal"; proposalId: string }
  | { type: "request_cancel"; bookingRef: string }
  | { type: "request_reschedule"; bookingRef: string };

export type ChatInput = { kind: "text"; text: string } | { kind: "ui"; action: UiAction };

export type SlotView = { dayLabel: string; timeLabel: string; startLocalIso: string };
export type BookingView = {
  ref: string;
  title: string;
  whenLabel: string;
  status: string;
  attendees: string[];
  meetingUrl?: string;
  startUtc: string;
  endUtc: string;
};
export type ProposalView = {
  id: string;
  kind: "book" | "cancel" | "reschedule";
  title: string;
  whenLabel: string;
  timeZone: string;
  durationMinutes: number;
  slotRef?: string;
  bookingRef?: string;
  name?: string;
  email?: string;
  notes?: string;
};
export type ReferenceViews = {
  slots: Record<string, SlotView>;
  bookings: Record<string, BookingView>;
};
export type BookingResult = {
  type: "booking_result";
  kind: "booked" | "cancelled" | "rescheduled";
  booking: BookingView;
};
type TurnEnd = {
  type: "turn_end";
  stateToken: string;
  metrics: { outcome: "ok" | "failed"; fallback?: "backup_model" | "code_only" };
};

export type ServerEvent =
  | { type: "status" | "text_delta" | "assistant_text"; text: string }
  | ({ type: "refs"; retiredBookings?: string[] } & Partial<ReferenceViews>)
  | { type: "proposal"; proposal: ProposalView }
  | BookingResult
  | { type: "booking_cards"; refs: string[] }
  | { type: "slot_chips"; refs: string[]; plainRefs?: string[]; note?: string }
  | { type: "error"; code: string; message: string; retryable: boolean; fallback?: "booker" }
  | TurnEnd;

export type TurnControls = "all" | "alternatives" | "none";

export function controlsForTurn(input: ChatInput, metrics: TurnEnd["metrics"], errorCode?: string): TurnControls {
  if (metrics.outcome === "ok" || metrics.fallback === "code_only") return "all";
  if (input.kind === "ui" && errorCode === "slot_taken" &&
      (input.action.type === "confirm_proposal" || input.action.type === "pick_slot")) {
    return "alternatives";
  }
  return "none";
}

export function unverifiedWrite(code?: string): boolean {
  return code === "write_unverified" || code === "reschedule_partial";
}

export function canRetryTurn(
  input: ChatInput,
  evidence: { kind: "terminal"; retryable: boolean } | { kind: "quota" } | { kind: "transport" },
): boolean {
  if (evidence.kind === "terminal") return evidence.retryable;
  if (evidence.kind === "quota") return true;
  return input.kind === "text" || input.action.type !== "confirm_proposal";
}

export type ReferencePart = { text: string } | { ref: string };

export function listedBookingRefs(
  list: string[] | undefined,
  bookings: ReferenceViews["bookings"],
): string[] {
  return list?.filter((ref) => Object.hasOwn(bookings, ref)) ?? [];
}

export function withoutRetiredBookings(
  bookings: ReferenceViews["bookings"],
  retired: ReadonlySet<string>,
): ReferenceViews["bookings"] {
  if (!retired.size) return bookings;
  const remaining = { ...bookings };
  for (const ref of retired) delete remaining[ref];
  return remaining;
}

export function splitReferenceText(text: string, streaming: boolean): ReferencePart[] {
  if (streaming) {
    const open = text.lastIndexOf("[[");
    if (open > text.lastIndexOf("]]")) text = text.slice(0, open);
    else if (text.endsWith("[")) text = text.slice(0, -1);
  }

  const parts: ReferencePart[] = [];
  let cursor = 0;
  for (const match of text.matchAll(/\[\[([SB][1-9][0-9]*)\]\]/g)) {
    if (match.index > cursor) parts.push({ text: text.slice(cursor, match.index) });
    parts.push({ ref: match[1] });
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor) });
  return parts;
}

export function nextSlotDate(slots: SlotView[]): string | undefined {
  const finalDay = slots.map((slot) => slot.startLocalIso.slice(0, 10)).sort().at(-1);
  if (!finalDay) return undefined;
  const next = new Date(`${finalDay}T00:00:00Z`);
  next.setUTCDate(next.getUTCDate() + 1);
  return Number.isFinite(next.getTime()) ? next.toISOString().slice(0, 10) : undefined;
}

export async function readChatStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: Exclude<ServerEvent, TurnEnd>) => void,
): Promise<TurnEnd> {
  const decoder = new TextDecoder("utf-8", { fatal: true });
  const reader = body.getReader();
  let pending = "";
  let terminal: TurnEnd | undefined;

  function dispatch(line: string) {
    if (!line.trim()) return;
    if (terminal) throw new Error("Unexpected event after the chat turn ended.");
    const event = JSON.parse(line) as ServerEvent;
    if (event.type === "turn_end") {
      if (typeof event.stateToken !== "string" || !event.stateToken ||
          !["ok", "failed"].includes(event.metrics?.outcome)) {
        throw new Error("Invalid chat state update.");
      }
      terminal = event;
    } else {
      onEvent(event);
    }
  }

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      const lines = (pending + decoder.decode(value, { stream: true })).split("\n");
      pending = lines.pop() ?? "";
      lines.forEach(dispatch);
    }
    dispatch(pending + decoder.decode());
    if (!terminal) throw new Error("The chat stream ended before saving the turn.");
    return terminal;
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
}
