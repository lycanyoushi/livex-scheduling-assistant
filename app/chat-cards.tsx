import { type KeyboardEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";
import {
  ArrowRight, CalendarCheck, CalendarClock, CalendarDays, CalendarPlus, Check, ChevronDown, ChevronRight,
  CircleCheck, Clock3, Globe, UserRound, Video, X, type LucideIcon,
} from "lucide-react";

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

const SLOT_TARGET = "min-h-11 min-w-11 font-medium leading-snug focus-visible:outline-2 focus-visible:outline-ring disabled:text-muted-foreground disabled:opacity-55";
const CARD = "rounded-2xl border border-border bg-card p-4 shadow-xs sm:p-5";
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
const PROPOSAL_STYLE = {
  book: { title: "Confirm booking", icon: CalendarCheck },
  cancel: { title: "Cancel meeting?", icon: X },
  reschedule: { title: "Move meeting", icon: CalendarClock },
};

type SendAction = (action: UiAction) => void;
type IsAvailable = (ref: string) => boolean;

function whenParts(label: string) {
  const split = label.indexOf(", ");
  return split < 0 ? { dayLabel: label, timeLabel: "" } : { dayLabel: label.slice(0, split), timeLabel: label.slice(split + 2) };
}

function bookingTimeInZone(booking: BookingView, timeZone: string) {
  const start = new Date(booking.startUtc);
  const end = new Date(booking.endUtc);
  if (!(start < end)) return undefined;

  try {
    const date = new Intl.DateTimeFormat("en-GB", {
      timeZone, weekday: "short", day: "numeric", month: "short", year: "numeric",
    });
    const clock = new Intl.DateTimeFormat("en-GB", {
      timeZone, hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZoneName: "longOffset",
    });
    const firstDay = date.format(start).replace(",", "");
    const lastDay = date.format(end).replace(",", "");
    const [firstTime, firstOffset = ""] = clock.format(start).split(" GMT");
    const [lastTime, lastOffset = ""] = clock.format(end).split(" GMT");
    return {
      dayLabel: firstDay === lastDay ? firstDay : `${firstDay} – ${lastDay}`,
      timeLabel: firstOffset === lastOffset ? `${firstTime}–${lastTime}` :
        `${firstTime} UTC${firstOffset}–${lastTime} UTC${lastOffset}`,
    };
  } catch {
    return undefined;
  }
}

function Detail({ icon: Icon, children }: { icon: LucideIcon; children: ReactNode }) {
  return (
    <p className="flex items-start gap-2 text-sm text-muted-foreground wrap-anywhere">
      <Icon size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
      <span>{children}</span>
    </p>
  );
}

export function ConfirmCard({ proposal, originalBooking, active, submitting, onAction }: {
  proposal: ProposalView;
  originalBooking?: BookingView;
  active: boolean;
  submitting: boolean;
  onAction: SendAction;
}) {
  const headingId = useId();
  const { title, icon: TitleIcon } = PROPOSAL_STYLE[proposal.kind];
  const { dayLabel, timeLabel } = whenParts(proposal.whenLabel);
  const duration = `${proposal.durationMinutes} min`;
  const guestName = proposal.kind === "book" ? proposal.name : originalBooking?.guestName ?? proposal.name;
  const guestEmail = proposal.kind === "book" ? proposal.email : originalBooking?.attendees.join(", ") || proposal.email;
  const guest = [guestName, guestEmail].filter(Boolean).join(" · ");
  const move = proposal.kind === "reschedule" && originalBooking
    ? bookingTimeInZone(originalBooking, proposal.timeZone) : undefined;
  const working = active && submitting;
  const disabled = !active || submitting;
  const hasGuestDetails = proposal.kind !== "book" || Boolean(proposal.name && proposal.email);

  return (
    <section className={`${CARD} ${active ? "" : "opacity-65"}`} aria-labelledby={headingId}>
      <div className="flex items-center gap-2.5">
        <div className={`grid size-9 shrink-0 place-items-center rounded-xl ${proposal.kind === "cancel"
          ? "bg-red-500/10 text-red-700 dark:text-red-300" : "bg-primary/10 text-ring"}`}>
          <TitleIcon size={18} aria-hidden="true" />
        </div>
        <h2 id={headingId} className="text-lg font-semibold" aria-label={PROPOSAL_LABELS[proposal.kind]}>{title}</h2>
      </div>
      <p className="mt-3 font-medium">{proposal.title}</p>
      {move && (
        <div className="mt-3 grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-2 rounded-xl bg-primary/5 p-3 text-sm sm:gap-4">
          <div>
            <p className="font-medium text-muted-foreground">From</p>
            <p>{move.dayLabel}</p>
            {move.timeLabel && <p className="text-muted-foreground">{move.timeLabel}</p>}
          </div>
          <ArrowRight size={17} className="text-muted-foreground" aria-hidden="true" />
          <div>
            <p className="font-medium text-ring">To</p>
            <p>{dayLabel}</p>
            <p className="text-muted-foreground">{timeLabel ? `${timeLabel} · ${duration}` : duration}</p>
          </div>
        </div>
      )}
      <div className="mt-2 grid gap-1.5">
        {!move && <>
          <Detail icon={CalendarDays}>{dayLabel}</Detail>
          <Detail icon={Clock3}>{timeLabel ? `${timeLabel} · ${duration}` : duration}</Detail>
        </>}
        <Detail icon={Globe}>{proposal.timeZone}</Detail>
        {guest && <Detail icon={UserRound}>{guest}</Detail>}
      </div>
      {proposal.notes && <p className="mt-2 text-sm text-muted-foreground wrap-anywhere">{proposal.notes}</p>}
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <button
          className={proposal.kind === "cancel" ? `${OUTLINE_BUTTON} bg-red-700 text-white hover:bg-red-800` : PRIMARY_BUTTON}
          type="button"
          disabled={disabled}
          onClick={() => onAction({ type: "confirm_proposal", proposalId: proposal.id })}
        >
          {working ? "Working…" : "Confirm"}
        </button>
        <button
          className={`${OUTLINE_BUTTON} border border-border`}
          type="button"
          disabled={disabled}
          onClick={() => onAction({ type: "dismiss_proposal", proposalId: proposal.id })}
        >
          Not now
        </button>
      </div>
      {active && hasGuestDetails && <p className="mt-2 text-xs text-muted-foreground">You can also type yes or no in chat.</p>}
    </section>
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

function CalendarDropdown({ booking, google, button }: { booking: BookingView; google: string; button: string }) {
  const id = useId();
  const menu = useRef<HTMLDivElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  const [panel, setPanel] = useState<{ above: boolean; height: number } | null>(null);
  const open = panel !== null;

  useEffect(() => {
    if (!open) return;
    function dismissOutside(event: PointerEvent) {
      if (event.target instanceof Node && !menu.current?.contains(event.target)) setPanel(null);
    }
    document.addEventListener("pointerdown", dismissOutside);
    return () => document.removeEventListener("pointerdown", dismissOutside);
  }, [open]);

  function close() {
    setPanel(null);
    toggle.current?.focus();
  }

  function togglePanel() {
    if (open) {
      close();
      return;
    }
    const bounds = toggle.current?.getBoundingClientRect();
    if (!bounds) return;
    const viewport = menu.current?.closest("section")?.getBoundingClientRect();
    const below = Math.min(window.innerHeight, viewport?.bottom ?? window.innerHeight) - bounds.bottom;
    const above = bounds.top - Math.max(0, viewport?.top ?? 0);
    // Two 44 px actions plus the menu's spacing and border.
    const opensAbove = below < 110 && above > below;
    setPanel({ above: opensAbove, height: Math.max(0, (opensAbove ? above : below) - 6) });
  }

  return (
    <div ref={menu} className="relative" onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) setPanel(null);
    }} onKeyDown={(event) => {
      if (open && event.key === "Escape") {
        event.stopPropagation();
        close();
      }
    }}>
      <button ref={toggle} className={`${button} gap-1.5`} type="button" aria-expanded={open} aria-controls={id}
        onClick={togglePanel}>
        <CalendarPlus size={15} aria-hidden="true" />
        Add to calendar
        <ChevronDown size={14} aria-hidden="true" />
      </button>
      <div id={id} hidden={!open} style={panel ? { maxHeight: panel.height } : undefined}
        className={panel ? `absolute left-0 z-10 grid min-w-40 gap-1 overflow-y-auto rounded-lg border border-border bg-card p-1 shadow-md ${panel.above
          ? "bottom-full mb-1" : "top-full mt-1"}` : undefined}>
        <a className={button} href={google} target="_blank" rel="noreferrer" onClick={close}>Google Calendar</a>
        <button className={button} type="button" onClick={() => { downloadCalendarFile(booking); close(); }}>Download .ics</button>
      </div>
    </div>
  );
}

const BOOKING_STATUS: Record<string, string> = {
  accepted: "bg-emerald-100 text-emerald-800 dark:bg-emerald-400/15 dark:text-emerald-300",
  pending: "bg-amber-100 text-amber-800 dark:bg-amber-400/15 dark:text-amber-300",
  cancelled: "bg-red-100/75 text-red-800 dark:bg-red-400/10 dark:text-red-300",
};
const BOOKING_BUTTON = "inline-flex min-h-11 items-center rounded-md border border-border px-3 text-sm font-medium text-muted-foreground hover:bg-primary/5 hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring disabled:opacity-50";

function manageableBooking(status: string) {
  return status === "accepted" || status === "pending";
}

export function BookingCard({ booking, result, available, linksAvailable, onAction }: {
  booking: BookingView;
  result?: BookingResult["kind"];
  available: boolean;
  linksAvailable: boolean;
  onAction: SendAction;
}) {
  const article = useRef<HTMLElement>(null);
  useEffect(() => {
    if (result) article.current?.focus();
  }, [result]);

  const status = booking.status.toLowerCase();
  const canManage = manageableBooking(status);
  const join = canManage && linksAvailable ? safeMeetingLink(booking.meetingUrl) : undefined;
  const google = canManage && linksAvailable ? googleCalendarLink(booking) : undefined;
  const statusLabel = status === "accepted" ? "Confirmed" : status.charAt(0).toUpperCase() + status.slice(1).replaceAll("_", " ");
  const { dayLabel, timeLabel } = whenParts(booking.whenLabel);
  const ResultIcon = result === "booked" ? CircleCheck : result === "rescheduled" ? CalendarClock : X;

  return (
    <article ref={article} tabIndex={result ? -1 : undefined} className={`${CARD} my-2 whitespace-normal focus-visible:outline-2 focus-visible:outline-ring`}>
      {result && (
        <div className={`mb-3 flex items-center gap-2 rounded-lg px-3 py-2 font-semibold ${result === "booked"
          ? BOOKING_STATUS.accepted : result === "cancelled" ? BOOKING_STATUS.cancelled : "bg-primary/10 text-ring"}`}>
          <ResultIcon size={17} aria-hidden="true" />
          <h2>{RESULT_LABELS[result]}</h2>
        </div>
      )}
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="font-semibold wrap-anywhere">{booking.guestName || booking.title}</p>
          {booking.guestName && booking.title !== booking.guestName && (
            <p className="mt-0.5 text-sm text-muted-foreground wrap-anywhere">{booking.title}</p>
          )}
        </div>
        <span className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ${BOOKING_STATUS[status] ?? "bg-primary/10 text-ring"}`}>{statusLabel}</span>
      </div>
      <div className="mt-2 grid gap-1.5">
        <Detail icon={CalendarDays}>{dayLabel}</Detail>
        {timeLabel && <Detail icon={Clock3}>{timeLabel}</Detail>}
        {booking.attendees.length > 0 && <Detail icon={UserRound}>{booking.attendees.join(", ")}</Detail>}
      </div>
      {canManage && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {join && <a className={`${BOOKING_BUTTON} gap-1.5`} href={join} target="_blank" rel="noreferrer"><Video size={15} aria-hidden="true" />Join</a>}
          {google && <CalendarDropdown booking={booking} google={google} button={BOOKING_BUTTON} />}
          <button className={BOOKING_BUTTON} type="button" disabled={!available} onClick={() => onAction({ type: "request_reschedule", bookingRef: booking.ref })}>
            Reschedule
          </button>
          <button className={BOOKING_BUTTON} type="button" disabled={!available} onClick={() => onAction({ type: "request_cancel", bookingRef: booking.ref })}>
            Cancel
          </button>
        </div>
      )}
    </article>
  );
}

export function BookingAgenda({ bookings, available, onAction }: {
  bookings: BookingView[];
  available: IsAvailable;
  onAction: SendAction;
}) {
  const id = useId();
  const days = new Map<string, { booking: BookingView; timeLabel: string }[]>();
  for (const booking of bookings) {
    const { dayLabel, timeLabel } = whenParts(booking.whenLabel);
    const startDay = dayLabel.split(" – ", 1)[0];
    const rows = days.get(startDay) ?? [];
    rows.push({ booking, timeLabel });
    days.set(startDay, rows);
  }

  return (
    <ul aria-label="Meetings" className="divide-y divide-border overflow-hidden rounded-2xl border border-border bg-card shadow-xs">
      {[...days].map(([day, rows]) => (
        <li key={day}>
          <h2 className="bg-primary/5 px-3.5 py-2.5 text-sm font-semibold sm:px-4">{day}</h2>
          <ul className="divide-y divide-border">
            {rows.map(({ booking, timeLabel }) => {
              const timeId = `${id}-${booking.ref}-time`;
              const titleId = `${id}-${booking.ref}-title`;
              const disabled = !available(booking.ref) || !manageableBooking(booking.status.toLowerCase());
              const detailIds = `${titleId} ${timeId}`;
              return (
                <li key={booking.ref} className="grid min-w-0 grid-cols-[max-content_minmax(0,1fr)] items-center gap-x-3 gap-y-2 px-3.5 py-2.5 sm:px-4 lg:grid-cols-[max-content_minmax(0,1fr)_auto]">
                  <time id={timeId} dateTime={booking.startUtc} className="self-start text-xs leading-5 whitespace-nowrap text-muted-foreground tabular-nums sm:text-sm">
                    {timeLabel || booking.whenLabel}
                  </time>
                  <div id={titleId} className="min-w-0 wrap-anywhere">
                    <p className="text-sm leading-5 font-semibold">{booking.title}</p>
                    {booking.guestName && <p className="text-xs text-muted-foreground sm:text-sm">{booking.guestName}</p>}
                  </div>
                  <div className="col-start-2 flex flex-wrap gap-1.5 lg:col-start-3">
                    <button type="button" className={BOOKING_BUTTON} aria-describedby={detailIds} disabled={disabled}
                      onClick={() => onAction({ type: "request_reschedule", bookingRef: booking.ref })}>Reschedule</button>
                    <button type="button" className={BOOKING_BUTTON} aria-describedby={detailIds} disabled={disabled}
                      onClick={() => onAction({ type: "request_cancel", bookingRef: booking.ref })}>Cancel</button>
                  </div>
                </li>
              );
            })}
          </ul>
        </li>
      ))}
    </ul>
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

function TimeChip({ slot, refId, includeDay, selected = false, dimmed = false, available, onAction }: {
  slot: SlotView;
  refId: string;
  includeDay?: boolean;
  selected?: boolean;
  dimmed?: boolean;
  available: boolean;
  onAction: SendAction;
}) {
  const label = `${slot.dayLabel} at ${slot.timeLabel}`;
  const appearance = selected ? "border-ring bg-primary/10 text-ring" : "border-border bg-background text-foreground";
  const outline = `border ${appearance} group-hover:border-ring/45 group-hover:bg-primary/10`;
  return (
    <button
      className={`${SLOT_TARGET} group inline-flex items-center justify-center gap-1 text-xs tabular-nums ${includeDay
        ? "max-w-full rounded-full align-middle" : `w-full rounded-lg px-1 py-2 ${outline}`} ${dimmed ? "opacity-65 hover:opacity-100" : ""}`}
      type="button"
      aria-label={label}
      aria-pressed={selected || dimmed ? selected : undefined}
      disabled={!available}
      data-slot-key
      onClick={() => onAction({ type: "pick_slot", slotRef: refId })}
    >
      {includeDay ? (
        <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs ${outline}`}>
          {selected && <Check size={13} className="shrink-0" aria-hidden="true" />}
          {label}
        </span>
      ) : <>
        {selected && <Check size={13} className="shrink-0" aria-hidden="true" />}
        {slot.timeLabel}
      </>}
    </button>
  );
}

function MoreTimes({ slots, available, onAction }: { slots: SlotView[]; available: boolean; onAction: SendAction }) {
  const fromDate = nextSlotDate(slots);
  return (
    <button className={`${SLOT_TARGET} inline-flex items-center gap-1 rounded-lg px-2.5 text-sm text-ring hover:bg-primary/5`}
      type="button" disabled={!available} data-slot-key
      onClick={() => onAction(fromDate ? { type: "more_slots", fromDate } : { type: "more_slots" })}>
      More times
      <ChevronRight size={16} aria-hidden="true" />
    </button>
  );
}

function slotZoneLabel(timeZone?: string, slot?: SlotView) {
  const recordedOffset = slot?.startLocalIso.match(/(?:Z|[+-]\d{2}:\d{2})$/)?.[0];
  if (!timeZone || !slot || !recordedOffset) return timeZone;

  const recorded = /^(?:Z|[+-]00:00)$/.test(recordedOffset) ? "UTC" : `UTC${recordedOffset}`;
  try {
    const current = new Intl.DateTimeFormat("en-US", { timeZone, timeZoneName: "longOffset" })
      .formatToParts(new Date(slot.startLocalIso)).find((part) => part.type === "timeZoneName")?.value.replace(/^GMT/, "UTC");
    if (current && current !== recorded) return recorded;
  } catch {
    return recorded;
  }
  return timeZone;
}

export function SlotChips({ refs, plainRefs = [], slots, note, timeZone, selectedRef, available, allowMore, onAction }: {
  refs: string[];
  plainRefs?: string[];
  slots: ReferenceViews["slots"];
  note?: string;
  timeZone?: string;
  selectedRef?: string;
  available: IsAvailable;
  allowMore: boolean;
  onAction: SendAction;
}) {
  const days = new Map<string, { refId: string; slot: SlotView }[]>();
  const visible: SlotView[] = [];
  const choices = new Set(refs);
  const hasSelection = selectedRef !== undefined && choices.has(selectedRef);
  for (const refId of new Set([...refs, ...plainRefs])) {
    const slot = slots[refId];
    if (!slot) continue;
    visible.push(slot);
    const day = days.get(slot.dayLabel) ?? [];
    day.push({ refId, slot });
    days.set(slot.dayLabel, day);
  }
  const zoneLabel = slotZoneLabel(timeZone, visible[0]);

  return (
    <div role="group" aria-label="Available times" className={`${CARD} grid gap-3`} onKeyDown={navigateSlots}>
      <div className="flex items-center gap-2.5">
        <div className="grid size-9 shrink-0 place-items-center rounded-xl bg-primary/10 text-ring">
          <CalendarDays size={18} aria-hidden="true" />
        </div>
        <div>
          <h2 className="font-semibold">Available times</h2>
          {zoneLabel && <p className="text-xs text-muted-foreground">{zoneLabel}</p>}
        </div>
      </div>
      {note && !/^Openings across the requested dates[.!]?$/.test(note.trim()) && <p className="text-sm text-muted-foreground">{note}</p>}
      {[...days].map(([day, entries]) => (
        <div key={day}>
          <p className="mb-1.5 text-sm font-medium text-muted-foreground">{day}</p>
          <div className="grid grid-cols-3 gap-1.5 sm:grid-cols-4 sm:gap-2">
            {entries.map(({ refId, slot }) => choices.has(refId)
              ? <TimeChip key={refId} refId={refId} slot={slot} selected={refId === selectedRef}
                  dimmed={hasSelection && refId !== selectedRef} available={available(refId)} onAction={onAction} />
              : <span key={refId} className="inline-flex items-center justify-center text-center text-sm text-muted-foreground">{slot.timeLabel}</span>)}
          </div>
        </div>
      ))}
      {allowMore && refs.some((ref) => slots[ref]) && (
        <div><MoreTimes slots={visible} available={refs.some(available)} onAction={onAction} /></div>
      )}
    </div>
  );
}

type MessageBlock = { kind: "line" | "list"; lines: ReferencePart[][] };

function messageBlocks(parts: ReferencePart[]): MessageBlock[] {
  let line: ReferencePart[] = [];
  const lines = [line];
  for (const part of parts) {
    if ("ref" in part) {
      line.push(part);
      continue;
    }
    part.text.split(/\r\n|\r|\n/).forEach((text, index) => {
      if (index > 0) {
        line = [];
        lines.push(line);
      }
      if (text) line.push({ text });
    });
  }

  const blocks: MessageBlock[] = [];
  for (const row of lines) {
    const first = row[0];
    const bullet = first && "text" in first ? /^\s*[-*+•][ \t]+/.exec(first.text) : null;
    if (bullet && "text" in first) {
      const item = [{ text: first.text.slice(bullet[0].length) }, ...row.slice(1)];
      const previous = blocks.at(-1);
      if (previous?.kind === "list") previous.lines.push(item);
      else blocks.push({ kind: "list", lines: [item] });
    } else {
      blocks.push({ kind: "line", lines: [row] });
    }
  }
  return blocks;
}

export function RefText({ parts, views, currentBookings, listedBookings, slotAvailable, bookingAvailable, selectedRef, showMore, onAction }: {
  parts: ReferencePart[];
  views: ReferenceViews;
  currentBookings: ReferenceViews["bookings"];
  listedBookings: readonly string[];
  slotAvailable: IsAvailable;
  bookingAvailable: IsAvailable;
  selectedRef?: string;
  showMore: boolean;
  onAction: SendAction;
}) {
  const slotRefs = parts.flatMap((part) => "ref" in part && views.slots[part.ref] ? [part.ref] : []);
  const hasSelection = selectedRef !== undefined && slotRefs.includes(selectedRef);

  function renderLine(line: ReferencePart[]) {
    const segments = line.flatMap<ReferencePart>((part) => "text" in part
      ? part.text.split(/(\*\*)/).filter(Boolean).map((text) => ({ text })) : [part]);
    const markers = segments.filter((part) => "text" in part && part.text === "**").length;
    let pairedMarkers = markers - markers % 2;
    let bold = false;
    return segments.map((part, index) => {
      if ("text" in part) {
        if (part.text === "**" && pairedMarkers > 0) {
          pairedMarkers--;
          bold = !bold;
          return null;
        }
        return bold ? <strong key={index}>{part.text}</strong> : <span key={index}>{part.text}</span>;
      }
      const slot = views.slots[part.ref];
      if (slot) return <TimeChip key={index} refId={part.ref} slot={slot} includeDay selected={part.ref === selectedRef}
        dimmed={hasSelection && part.ref !== selectedRef} available={slotAvailable(part.ref)} onAction={onAction} />;
      const booking = views.bookings[part.ref];
      if (booking && !listedBookings.includes(part.ref)) {
        return (
          <BookingCard
            key={index}
            booking={currentBookings[part.ref] ?? booking}
            available={bookingAvailable(part.ref)}
            linksAvailable={Boolean(currentBookings[part.ref])}
            onAction={onAction}
          />
        );
      }
      return <span key={index} className="text-muted-foreground">{part.ref}</span>;
    });
  }

  return (
    <div className="grid gap-1 text-[15px] leading-relaxed whitespace-pre-wrap wrap-break-word" onKeyDown={navigateSlots}>
      <span className="sr-only">Assistant: </span>
      {messageBlocks(parts).map(({ kind, lines }, index) => kind === "list" ? (
        <ul key={index} className="list-disc space-y-1 pl-5">
          {lines.map((line, row) => <li key={row}>{renderLine(line)}</li>)}
        </ul>
      ) : <div key={index} className={lines[0].length ? undefined : "min-h-2"}>{renderLine(lines[0])}</div>)}
      {showMore && slotRefs.length > 0 && (
        <div><MoreTimes slots={slotRefs.map((ref) => views.slots[ref])} available={slotRefs.some(slotAvailable)} onAction={onAction} /></div>
      )}
    </div>
  );
}
