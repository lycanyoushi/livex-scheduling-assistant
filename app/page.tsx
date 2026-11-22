"use client";

import { type FormEvent, useEffect, useRef, useState } from "react";

import {
  type BookingResult,
  type ChatInput,
  type ProposalView,
  type ReferenceViews,
  type TurnControls,
  type UiAction,
  controlsForTurn,
  readChatStream,
  splitReferenceText,
} from "./chat-protocol";
import { BookingCard, ConfirmCard, OUTLINE_BUTTON, PRIMARY_BUTTON, RefText, SlotChips } from "./chat-cards";

type TurnError = { message: string; retryable: boolean; code?: string };
type SlotGroup = { refs: string[]; note?: string };

type TurnRecord = {
  id: number;
  session: number;
  input: ChatInput;
  text: string;
  views: ReferenceViews;
  controls: TurnControls;
  groups: SlotGroup[];
  results: BookingResult[];
  listedBookings?: string[];
  proposal?: ProposalView;
  error?: TurnError;
};

const EMPTY_VIEWS: ReferenceViews = { slots: {}, bookings: {} };

function browserTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

function keepLatestReferences<T>(views: Record<string, T>, limit: number): Record<string, T> {
  const entries = Object.entries(views).sort(([a], [b]) => Number(a.slice(1)) - Number(b.slice(1)));
  return Object.fromEntries(entries.slice(-limit));
}

export default function Home() {
  const [draft, setDraft] = useState("");
  const [turns, setTurns] = useState<TurnRecord[]>([]);
  const [activeProposal, setActiveProposal] = useState<{ turnId: number; value: ProposalView } | null>(null);
  const [currentViews, setCurrentViews] = useState(EMPTY_VIEWS);
  const [session, setSession] = useState(0);
  const [activeKind, setActiveKind] = useState<ChatInput["kind"] | null>(null);
  const [status, setStatus] = useState("");
  const token = useRef<string | null>(null);
  const savedViews = useRef(EMPTY_VIEWS);
  const nextTurnId = useRef(1);
  const request = useRef<AbortController | null>(null);
  const transcript = useRef<HTMLElement>(null);
  const followsTranscript = useRef(true);
  const sending = activeKind !== null;

  useEffect(() => () => request.current?.abort(), []);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      const query = new URLSearchParams({ timeZone: browserTimeZone() });
      // Warming up the calendar is independent of the chat turn.
      void fetch(`/api/warm?${query}`, { cache: "no-store", signal: controller.signal }).catch(() => {});
    }, 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, []);

  useEffect(() => {
    const log = transcript.current;
    if (log && followsTranscript.current) log.scrollTop = log.scrollHeight;
  }, [turns, status]);

  async function sendTurn(input: ChatInput, retryId?: number) {
    if (request.current) return;

    const controller = new AbortController();
    request.current = controller;
    followsTranscript.current = true;
    const id = retryId ?? nextTurnId.current++;
    const record: TurnRecord = {
      id, session, input, text: "", results: [], groups: [], views: savedViews.current, controls: "none",
    };
    setTurns((previous) => retryId === undefined
      ? [...previous, record]
      : previous.map((turn) => turn.id === id ? record : turn));
    setActiveKind(input.kind);
    setStatus("Sending…");

    function updateTurn(update: (turn: TurnRecord) => TurnRecord) {
      setTurns((previous) => previous.map((turn) => turn.id === id ? update(turn) : turn));
    }

    let views = savedViews.current;
    let nextProposal: ProposalView | undefined;
    let failure: TurnError | undefined;
    let resetSession = false;
    let bookingResultReceived = false;
    let rejectedBeforeConfirm = false;
    const recoveryRefs = new Set<string>();
    const confirmsProposal = input.kind === "ui" && input.action.type === "confirm_proposal";
    const originalBookingRef = confirmsProposal ? activeProposal?.value.bookingRef : undefined;

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
        signal: controller.signal,
        body: JSON.stringify({
          stateToken: token.current,
          timeZone: browserTimeZone(),
          input,
        }),
      });
      if (!response.ok || !response.body) {
        rejectedBeforeConfirm = response.status >= 400 && response.status < 500;
        throw new Error("The chat endpoint did not accept the request.");
      }

      const terminal = await readChatStream(response.body, (event) => {
        switch (event.type) {
          case "status":
            setStatus(event.text);
            break;
          case "text_delta":
            setStatus("");
            updateTurn((turn) => ({ ...turn, text: turn.text + event.text }));
            break;
          case "assistant_text":
            setStatus("");
            updateTurn((turn) => ({ ...turn, text: [turn.text, event.text].filter(Boolean).join("\n\n") }));
            break;
          case "refs": {
            const nextViews = {
              slots: { ...views.slots, ...event.slots },
              bookings: { ...views.bookings, ...event.bookings },
            };
            views = nextViews;
            const listedRefs = input.kind === "text" && event.bookings && Object.keys(event.bookings);
            updateTurn((turn) => ({
              ...turn,
              views: nextViews,
              ...(listedRefs ? { listedBookings: [...new Set([...(turn.listedBookings ?? []), ...listedRefs])] } : {}),
            }));
            break;
          }
          case "slot_chips": {
            setStatus("");
            for (const ref of event.refs) recoveryRefs.add(ref);
            updateTurn((turn) => ({
              ...turn,
              groups: [...turn.groups, { refs: event.refs, note: event.note }],
            }));
            break;
          }
          case "proposal":
            nextProposal = event.proposal;
            break;
          case "booking_result": {
            bookingResultReceived = true;
            const bookings = { ...views.bookings, [event.booking.ref]: event.booking };
            if (originalBookingRef && originalBookingRef !== event.booking.ref && bookings[originalBookingRef]) {
              bookings[originalBookingRef] = { ...bookings[originalBookingRef], status: event.kind };
            }
            const nextViews = { ...views, bookings };
            views = nextViews;
            updateTurn((turn) => ({ ...turn, results: [...turn.results, event], views: nextViews }));
            break;
          }
          case "error":
            failure = { code: event.code, message: event.message, retryable: event.retryable && input.kind === "text" };
            resetSession = event.code === "session_expired" || event.code === "interaction_expired";
            break;
        }
      });

      token.current = terminal.stateToken;
      const controls = controlsForTurn(input, terminal.metrics.outcome, failure?.code);
      if (resetSession) {
        savedViews.current = EMPTY_VIEWS;
        setSession((previous) => previous + 1);
      } else if (controls === "all") {
        savedViews.current = { slots: keepLatestReferences(views.slots, 40), bookings: keepLatestReferences(views.bookings, 20) };
      } else if (controls === "alternatives") {
        const alternatives = Object.fromEntries([...recoveryRefs].flatMap((ref) => views.slots[ref] ? [[ref, views.slots[ref]]] : []));
        savedViews.current = {
          ...savedViews.current,
          slots: keepLatestReferences({ ...savedViews.current.slots, ...alternatives }, 40),
        };
      }
      setCurrentViews(savedViews.current);

      const proposalLostItsSlot = controls === "alternatives" && confirmsProposal;
      if (proposalLostItsSlot && activeProposal) {
        setTurns((previous) => previous.map((turn) => turn.id === activeProposal.turnId ? { ...turn, proposal: undefined } : turn));
      }
      if (resetSession || bookingResultReceived || proposalLostItsSlot) {
        setActiveProposal(null);
      } else if (controls === "all") {
        if (nextProposal) setActiveProposal({ turnId: id, value: nextProposal });
        else if (input.kind === "ui") setActiveProposal(null);
      }
      const committedProposal = controls === "all" ? nextProposal : undefined;
      updateTurn((turn) => ({ ...turn, controls, proposal: committedProposal }));

      if (failure) {
        const error = failure;
        updateTurn((turn) => ({ ...turn, error }));
      } else if (terminal.metrics.outcome === "failed") {
        updateTurn((turn) => ({
          ...turn,
          error: { message: "The request could not be completed.", retryable: input.kind === "text" },
        }));
      }
    } catch {
      let message = controller.signal.aborted ? "Response stopped." : "The chat connection closed.";
      if (confirmsProposal && !rejectedBeforeConfirm) {
        setActiveProposal(null);
        if (!bookingResultReceived) message = "Check your calendar to see whether the change went through.";
      }
      updateTurn((turn) => ({ ...turn, error: { message, retryable: input.kind === "text" } }));
    } finally {
      request.current = null;
      setActiveKind(null);
      setStatus("");
    }
  }

  function takeAction(action: UiAction) {
    void sendTurn({ kind: "ui", action });
  }

  function sendDraft(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || request.current) return;
    setDraft("");
    void sendTurn({ kind: "text", text });
  }

  return (
    <main className="flex h-dvh flex-col">
      <header className="border-b border-border bg-card">
        <div className="mx-auto flex max-w-4xl items-center gap-3 px-4 py-4 sm:px-8">
          <span className="calendar-mark" aria-hidden="true" />
          <p className="text-base font-semibold tracking-tight">Scheduling Assistant</p>
        </div>
      </header>

      <section
        ref={transcript}
        aria-labelledby="chat-title"
        className="min-h-0 flex-1 overflow-y-auto"
        onScroll={(event) => {
          const { scrollHeight, scrollTop, clientHeight } = event.currentTarget;
          followsTranscript.current = scrollHeight - scrollTop - clientHeight < 48;
        }}
      >
        <div className="mx-auto flex min-h-full max-w-3xl flex-col px-4 py-6 sm:px-8">
          <h1 id="chat-title" className={turns.length ? "sr-only" : "my-auto py-6 text-center text-3xl font-semibold tracking-tight"}>
            What would you like to schedule?
          </h1>

          <div role="log" aria-label="Messages" className="grid gap-5">
            {turns.map((turn) => {
              const isLatest = turn.id === turns.at(-1)?.id;
              const sameSession = turn.session === session;
              const live = sameSession && !sending;
              const currentBookings = sameSession ? currentViews.bookings : EMPTY_VIEWS.bookings;
              const parts = splitReferenceText(turn.text, sending && isLatest);
              const referredBookings = new Set(parts.flatMap((part) => "ref" in part ? [part.ref] : []));
              const listed = turn.listedBookings?.filter((ref) => !referredBookings.has(ref)) ?? [];
              const slotAvailable = (ref: string) => live && Boolean(currentViews.slots[ref]);
              const bookingAvailable = (ref: string) => live && turn.controls === "all" && Boolean(currentBookings[ref]);
              const slotGroups = turn.groups.map((group, index) => (
                <SlotChips
                  key={index}
                  refs={group.refs}
                  slots={turn.views.slots}
                  note={group.note === turn.error?.message ? undefined : group.note}
                  available={(ref) => turn.controls !== "none" && slotAvailable(ref)}
                  allowMore={turn.controls !== "alternatives"}
                  onAction={takeAction}
                />
              ));

              return (
                <div key={turn.id} className="grid gap-3">
                  {turn.input.kind === "text" && (
                    <p className="max-w-[90%] justify-self-end rounded-2xl bg-primary/10 px-4 py-3 whitespace-pre-wrap wrap-break-word">
                      <span className="sr-only">You: </span>{turn.input.text}
                    </p>
                  )}
                  {turn.input.kind === "text" && slotGroups}
                  {parts.length > 0 && (
                    <RefText
                      parts={parts}
                      views={turn.views}
                      currentBookings={currentBookings}
                      slotAvailable={(ref) => turn.controls === "all" && slotAvailable(ref)}
                      bookingAvailable={bookingAvailable}
                      showMore={turn.groups.length === 0 && turn.controls !== "alternatives"}
                      onAction={takeAction}
                    />
                  )}
                  {listed.length > 0 && (
                    <ul aria-label="Meetings" className="grid gap-1">
                      {listed.map((ref) => (
                        <li key={ref}>
                          <BookingCard booking={currentBookings[ref] ?? turn.views.bookings[ref]} available={bookingAvailable(ref)} onAction={takeAction} />
                        </li>
                      ))}
                    </ul>
                  )}
                  {turn.listedBookings?.length === 0 && turn.controls === "all" && (
                    <div>
                      {!turn.text && <p className="text-muted-foreground">No meetings to show.</p>}
                      <button type="button" className={`${OUTLINE_BUTTON} border border-border`}
                        disabled={!isLatest || !live} onClick={() => takeAction({ type: "more_slots" })}>
                        Book a meeting
                      </button>
                    </div>
                  )}
                  {turn.results.map((result) => (
                    <BookingCard
                      key={result.booking.ref}
                      booking={currentBookings[result.booking.ref] ?? result.booking}
                      result={result.kind}
                      available={bookingAvailable(result.booking.ref)}
                      onAction={takeAction}
                    />
                  ))}
                  {turn.error && (
                    <div className="rounded-xl border border-border bg-card px-4 py-3">
                      <p>{turn.error.message}</p>
                      {turn.error.retryable && isLatest && (
                        <button
                          type="button"
                          disabled={sending}
                          className={`${OUTLINE_BUTTON} mt-2 border border-border`}
                          onClick={() => void sendTurn(turn.input, turn.id)}
                        >
                          Retry
                        </button>
                      )}
                    </div>
                  )}
                  {turn.input.kind === "ui" && slotGroups}
                  {turn.proposal && (
                    <ConfirmCard
                      key={turn.proposal.id}
                      proposal={turn.proposal}
                      active={sameSession && activeProposal?.turnId === turn.id}
                      submitting={sending}
                      onAction={takeAction}
                    />
                  )}
                </div>
              );
            })}
          </div>

          <div role="status" className={status ? "mt-4 flex items-center gap-2 text-sm text-muted-foreground" : "sr-only"}>
            {status && <span aria-hidden="true" className="size-3.5 shrink-0 animate-spin rounded-full border-2 border-current border-t-transparent motion-reduce:animate-none" />}
            {status}
          </div>
        </div>
      </section>

      <form aria-label="Message composer" onSubmit={sendDraft} className="mx-auto w-full max-w-3xl px-4 pb-4 sm:px-8 sm:pb-8">
        <div className="flex items-end gap-2 rounded-xl border border-border bg-card p-2 shadow-xs">
          <label htmlFor="chat-message" className="sr-only">Message</label>
          <textarea
            id="chat-message"
            className="min-h-11 min-w-0 flex-1 resize-none rounded-md px-3 py-2.5 text-base placeholder:text-muted-foreground focus-visible:outline-2 focus-visible:outline-ring"
            placeholder="Type a message"
            rows={draft.includes("\n") ? 3 : 1}
            value={draft}
            maxLength={2_000}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
            disabled={sending}
          />
          {activeKind === "text" ? (
            <button type="button" className={`${OUTLINE_BUTTON} border border-border`} onClick={() => request.current?.abort()}>
              Stop
            </button>
          ) : (
            <button type="submit" className={PRIMARY_BUTTON} disabled={sending || !draft.trim()}>Send</button>
          )}
        </div>
      </form>
    </main>
  );
}
