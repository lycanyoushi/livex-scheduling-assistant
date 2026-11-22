import { type FormEvent, type KeyboardEvent, useEffect, useRef } from "react";

import { calendarFile, googleCalendarLink, safeMeetingLink } from "./calendar-export";
import {
  type BookingResult,
  type BookingView,
  type ProposalView,
  type ReferencePart,
  type ReferenceViews,
  type SlotView,
  type UiAction,
  nextSlotDate,
} from "./chat-protocol";

export const OUTLINE_BUTTON = "min-h-11 rounded-md px-4 text-sm font-semibold focus-visible:outline-2 focus-visible:outline-ring disabled:opacity-50";
export const PRIMARY_BUTTON = `${OUTLINE_BUTTON} bg-primary text-primary-foreground`;

const CONTACT_FIELD = "min-h-11 w-full rounded-md border border-border bg-background px-3 py-2 text-base focus-visible:outline-2 focus-visible:outline-ring";
const CHIP = "inline-flex min-h-10 items-center rounded-full border border-border bg-card px-3 text-sm leading-snug font-medium text-primary hover:border-primary focus-visible:outline-2 focus-visible:outline-ring disabled:text-muted-foreground disabled:opacity-55";
const RESULT_LABELS: Record<BookingResult["kind"], string> = {
  booked: "Meeting booked",
  cancelled: "Meeting cancelled",
  rescheduled: "Meeting moved",
};
const PROPOSAL_LABELS: Record<ProposalView["kind"], string> = {
  book: "Book this meeting?",
  cancel: "Cancel this meeting?",
  reschedule: "Move this meeting?",
};

type SendAction = (action: UiAction) => void;
type IsAvailable = (ref: string) => boolean;

export function ConfirmCard({ proposal, active, submitting, onAction }: {
  proposal: ProposalView;
  active: boolean;
  submitting: boolean;
  onAction: SendAction;
}) {
  function confirm(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!active || submitting) return;
    const action: UiAction = { type: "confirm_proposal", proposalId: proposal.id };
    if (proposal.kind === "book") {
      const fields = new FormData(event.currentTarget);
      action.name = String(fields.get("name") ?? "").trim();
      action.email = String(fields.get("email") ?? "").trim();
      action.notes = String(fields.get("notes") ?? "").trim();
    }
    onAction(action);
  }

  return (
    <form
      onSubmit={confirm}
      className={`rounded-xl border border-border bg-card p-5 shadow-xs ${active ? "" : "opacity-60"}`}
      aria-label={PROPOSAL_LABELS[proposal.kind]}
    >
      <h2 className="text-lg font-semibold">{PROPOSAL_LABELS[proposal.kind]}</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        {proposal.title} · {proposal.whenLabel} · {proposal.durationMinutes} min · {proposal.timeZone}
      </p>
      <fieldset className="mt-4 grid gap-3 sm:grid-cols-2" disabled={!active || submitting}>
        {proposal.kind === "book" && (
          <>
            <label className="grid gap-1 text-sm font-medium">
              Name
              <input className={CONTACT_FIELD} name="name" autoComplete="name" defaultValue={proposal.name} required maxLength={200} pattern=".*\S.*" />
            </label>
            <label className="grid gap-1 text-sm font-medium">
              Email
              <input className={CONTACT_FIELD} name="email" type="email" autoComplete="email" defaultValue={proposal.email} required maxLength={254} />
            </label>
            <label className="grid gap-1 text-sm font-medium sm:col-span-2">
              Note (optional)
              <textarea className={CONTACT_FIELD} name="notes" defaultValue={proposal.notes} rows={2} maxLength={2_000} />
            </label>
          </>
        )}
        <div className="flex gap-2 sm:col-span-2">
          <button className={PRIMARY_BUTTON} type="submit">
            {active && submitting ? "Working…" : "Confirm"}
          </button>
          <button
            className={`${OUTLINE_BUTTON} border border-border`}
            type="button"
            onClick={() => onAction({ type: "dismiss_proposal", proposalId: proposal.id })}
          >
            Change
          </button>
        </div>
      </fieldset>
    </form>
  );
}

function downloadCalendarFile(booking: BookingView) {
  const contents = calendarFile(booking, new Date());
  if (!contents) return;
  const url = URL.createObjectURL(new Blob([contents], { type: "text/calendar;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "meeting.ics";
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
}

export function BookingCard({ booking, result, available, onAction }: {
  booking: BookingView;
  result?: BookingResult["kind"];
  available: boolean;
  onAction: SendAction;
}) {
  const article = useRef<HTMLElement>(null);
  useEffect(() => {
    if (result) article.current?.focus();
  }, [result]);

  const status = booking.status.toLowerCase();
  const canManage = status === "accepted" || status === "pending";
  const join = canManage ? safeMeetingLink(booking.meetingUrl) : undefined;
  const google = canManage ? googleCalendarLink(booking) : undefined;
  const statusLabel = status === "accepted" ? "Confirmed" : status[0]?.toUpperCase() + status.slice(1).replaceAll("_", " ");
  const button = `${OUTLINE_BUTTON} inline-flex items-center border border-border`;

  return (
    <article ref={article} tabIndex={result ? -1 : undefined} className="my-2 rounded-xl border border-border bg-card px-4 py-3 whitespace-normal focus-visible:outline-2 focus-visible:outline-ring">
      {result && <h2 className="font-semibold text-primary">{RESULT_LABELS[result]}</h2>}
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className={result ? "" : "font-semibold"}>{booking.title}</p>
        <span className="rounded-full bg-primary/10 px-2.5 py-0.5 text-sm text-primary">{statusLabel}</span>
      </div>
      <p className="mt-1 text-sm text-muted-foreground">{booking.whenLabel}</p>
      {booking.attendees.length > 0 && (
        <p className="mt-1 text-sm wrap-anywhere text-muted-foreground">{booking.attendees.join(", ")}</p>
      )}
      {canManage && (
        <div className="mt-3 flex flex-wrap items-start gap-2">
          {join && <a className={button} href={join} target="_blank" rel="noreferrer">Join</a>}
          {google && (
            <details className="max-w-full">
              <summary className={`${OUTLINE_BUTTON} flex cursor-pointer list-none items-center border border-border`}>
                Add to calendar
              </summary>
              <div className="mt-2 grid gap-1 text-sm">
                <a className={button} href={google} target="_blank" rel="noreferrer">Google Calendar</a>
                <button className={button} type="button" onClick={() => downloadCalendarFile(booking)}>Download .ics</button>
              </div>
            </details>
          )}
          <button className={button} type="button" disabled={!available} onClick={() => onAction({ type: "request_reschedule", bookingRef: booking.ref })}>
            Reschedule
          </button>
          <button className={button} type="button" disabled={!available} onClick={() => onAction({ type: "request_cancel", bookingRef: booking.ref })}>
            Cancel
          </button>
        </div>
      )}
    </article>
  );
}

function navigateSlots(event: KeyboardEvent<HTMLElement>) {
  if (!(event.target instanceof HTMLButtonElement)) return;
  const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>("button[data-slot-key]:not(:disabled)")];
  const index = buttons.indexOf(event.target);
  if (index < 0) return;
  const nextIndex = {
    ArrowLeft: (index - 1 + buttons.length) % buttons.length,
    ArrowUp: (index - 1 + buttons.length) % buttons.length,
    ArrowRight: (index + 1) % buttons.length,
    ArrowDown: (index + 1) % buttons.length,
    Home: 0,
    End: buttons.length - 1,
  }[event.key];
  if (nextIndex === undefined) return;
  event.preventDefault();
  buttons[nextIndex]?.focus();
}

function TimeChip({ slot, refId, includeDay, available, onAction }: {
  slot: SlotView;
  refId: string;
  includeDay?: boolean;
  available: boolean;
  onAction: SendAction;
}) {
  return (
    <button
      className={CHIP}
      type="button"
      aria-label={`${slot.dayLabel} at ${slot.timeLabel}`}
      disabled={!available}
      data-slot-key
      onClick={() => onAction({ type: "pick_slot", slotRef: refId })}
    >
      {includeDay ? `${slot.dayLabel} at ${slot.timeLabel}` : slot.timeLabel}
    </button>
  );
}

function MoreTimes({ slots, available, onAction }: { slots: SlotView[]; available: boolean; onAction: SendAction }) {
  const fromDate = nextSlotDate(slots);
  return (
    <button className={CHIP} type="button" disabled={!available} data-slot-key
      onClick={() => onAction(fromDate ? { type: "more_slots", fromDate } : { type: "more_slots" })}>
      More times
    </button>
  );
}

export function SlotChips({ refs, slots, note, available, allowMore, onAction }: {
  refs: string[];
  slots: ReferenceViews["slots"];
  note?: string;
  available: IsAvailable;
  allowMore: boolean;
  onAction: SendAction;
}) {
  const days = new Map<string, { refId: string; slot: SlotView }[]>();
  const visible: SlotView[] = [];
  for (const refId of new Set(refs)) {
    const slot = slots[refId];
    if (!slot) continue;
    visible.push(slot);
    const day = days.get(slot.dayLabel) ?? [];
    day.push({ refId, slot });
    days.set(slot.dayLabel, day);
  }

  return (
    <div role="group" aria-label="Available times" className="grid gap-2" onKeyDown={navigateSlots}>
      {note && <p>{note}</p>}
      {[...days].map(([day, entries]) => (
        <div key={day}>
          <p className="mb-1 text-sm font-medium text-muted-foreground">{day}</p>
          <div className="flex flex-wrap gap-2">
            {entries.map(({ refId, slot }) => (
              <TimeChip key={refId} refId={refId} slot={slot} available={available(refId)} onAction={onAction} />
            ))}
          </div>
        </div>
      ))}
      {allowMore && visible.length > 0 && (
        <div><MoreTimes slots={visible} available={refs.some(available)} onAction={onAction} /></div>
      )}
    </div>
  );
}

export function RefText({ parts, views, currentBookings, slotAvailable, bookingAvailable, showMore, onAction }: {
  parts: ReferencePart[];
  views: ReferenceViews;
  currentBookings: ReferenceViews["bookings"];
  slotAvailable: IsAvailable;
  bookingAvailable: IsAvailable;
  showMore: boolean;
  onAction: SendAction;
}) {
  const slotRefs = parts.flatMap((part) => "ref" in part && views.slots[part.ref] ? [part.ref] : []);
  return (
    <div className="leading-10 whitespace-pre-wrap wrap-break-word" onKeyDown={navigateSlots}>
      <span className="sr-only">Assistant: </span>
      {parts.map((part, index) => {
        if ("text" in part) return <span key={index}>{part.text}</span>;
        const slot = views.slots[part.ref];
        if (slot) return <TimeChip key={index} refId={part.ref} slot={slot} includeDay available={slotAvailable(part.ref)} onAction={onAction} />;
        const booking = views.bookings[part.ref];
        if (booking) {
          return <BookingCard key={index} booking={currentBookings[part.ref] ?? booking} available={bookingAvailable(part.ref)} onAction={onAction} />;
        }
        return <span key={index} className="text-muted-foreground">{part.ref}</span>;
      })}
      {showMore && slotRefs.length > 0 && (
        <div><MoreTimes slots={slotRefs.map((ref) => views.slots[ref])} available={slotRefs.some(slotAvailable)} onAction={onAction} /></div>
      )}
    </div>
  );
}
