export type ChatInput =
  | { kind: "text"; text: string }
  | {
      kind: "ui";
      action:
        | { type: "confirm_proposal"; proposalId: string; name: string; email: string; notes?: string }
        | { type: "dismiss_proposal"; proposalId: string };
    };

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
  metrics: { outcome: "ok" | "failed" };
};

export type ServerEvent =
  | { type: "status" | "text_delta" | "assistant_text"; text: string }
  | ({ type: "refs" } & Partial<ReferenceViews>)
  | { type: "proposal"; proposal: ProposalView }
  | BookingResult
  | { type: "slot_chips"; refs: string[]; note?: string }
  | { type: "error"; code: string; message: string; retryable: boolean; fallback?: "booker" }
  | TurnEnd;

export function labelReference(ref: string, views: ReferenceViews): string {
  const slot = views.slots[ref];
  if (slot) return `${slot.dayLabel} at ${slot.timeLabel} (${ref})`;

  const booking = views.bookings[ref];
  if (booking) return `${booking.title}, ${booking.whenLabel} (${ref})`;

  return ref;
}

export function displayReferences(text: string, views: ReferenceViews): string {
  return text.replace(/\[\[([SB][1-9]\d*)\]\]/g, (_, ref: string) => labelReference(ref, views));
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
