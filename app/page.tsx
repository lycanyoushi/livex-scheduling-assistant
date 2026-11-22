"use client";

import { type FormEvent, useEffect, useRef, useState } from "react";

import {
  type BookingResult,
  type ChatInput,
  type ProposalView,
  type ReferenceViews,
  displayReferences,
  readChatStream,
} from "./chat-protocol";

type TurnError = { message: string; retryable: boolean };

type TurnRecord = {
  id: number;
  input: ChatInput;
  text: string;
  views: ReferenceViews;
  results: BookingResult[];
  error?: TurnError;
};

const EMPTY_VIEWS: ReferenceViews = { slots: {}, bookings: {} };
const RESULT_LABELS: Record<BookingResult["kind"], string> = {
  booked: "Meeting booked",
  cancelled: "Meeting cancelled",
  rescheduled: "Meeting moved",
};

const OUTLINE_BUTTON = "min-h-11 rounded-md px-4 text-sm font-semibold focus-visible:outline-2 focus-visible:outline-ring disabled:opacity-50";
const PRIMARY_BUTTON = `${OUTLINE_BUTTON} bg-primary text-primary-foreground`;
const CONTACT_FIELD = "min-h-11 w-full rounded-md border border-border bg-background px-3 py-2 text-base focus-visible:outline-2 focus-visible:outline-ring";

function BookingConfirmation({
  proposal,
  sending,
  onAction,
}: {
  proposal: ProposalView;
  sending: boolean;
  onAction: (action: Extract<ChatInput, { kind: "ui" }>["action"]) => void;
}) {
  function confirm(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    const name = String(fields.get("name") ?? "").trim();
    const email = String(fields.get("email") ?? "").trim();
    const notes = String(fields.get("notes") ?? "").trim();
    onAction({
      type: "confirm_proposal",
      proposalId: proposal.id,
      name,
      email,
      ...(notes ? { notes } : {}),
    });
  }

  return (
    <form onSubmit={confirm} className="rounded-xl border border-border bg-card p-5 shadow-xs">
      <h2 className="text-lg font-semibold">Book this meeting?</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        {proposal.title} · {proposal.whenLabel} · {proposal.durationMinutes} min · {proposal.timeZone}
      </p>
      <fieldset className="mt-4 grid gap-3 sm:grid-cols-2" disabled={sending}>
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
        <div className="flex gap-2 sm:col-span-2">
          <button className={PRIMARY_BUTTON} type="submit" disabled={sending}>Confirm</button>
          <button
            className={`${OUTLINE_BUTTON} border border-border`}
            type="button"
            disabled={sending}
            onClick={() => onAction({ type: "dismiss_proposal", proposalId: proposal.id })}
          >
            Change
          </button>
        </div>
      </fieldset>
    </form>
  );
}

export default function Home() {
  const [draft, setDraft] = useState("");
  const [turns, setTurns] = useState<TurnRecord[]>([]);
  const [proposal, setProposal] = useState<ProposalView | null>(null);
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
    const log = transcript.current;
    if (log && followsTranscript.current) log.scrollTop = log.scrollHeight;
  }, [turns, proposal, status]);

  async function sendTurn(input: ChatInput, retryId?: number) {
    if (request.current) return;

    const controller = new AbortController();
    request.current = controller;
    followsTranscript.current = true;
    const id = retryId ?? nextTurnId.current++;
    const record: TurnRecord = { id, input, text: "", results: [], views: savedViews.current };
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

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
        signal: controller.signal,
        body: JSON.stringify({
          stateToken: token.current,
          timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
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
            updateTurn((turn) => ({ ...turn, views: nextViews }));
            break;
          }
          case "slot_chips": {
            const slots = event.refs.map((ref) => `[[${ref}]]`).join("\n");
            updateTurn((turn) => ({
              ...turn,
              text: [turn.text, event.note, slots].filter(Boolean).join("\n\n"),
            }));
            break;
          }
          case "proposal":
            nextProposal = event.proposal;
            break;
          case "booking_result":
            bookingResultReceived = true;
            updateTurn((turn) => ({ ...turn, results: [...turn.results, event] }));
            break;
          case "error":
            failure = { message: event.message, retryable: event.retryable && input.kind === "text" };
            resetSession = event.code === "session_expired" || event.code === "interaction_expired";
            break;
        }
      });

      token.current = terminal.stateToken;
      savedViews.current = resetSession ? EMPTY_VIEWS : views;
      if (resetSession || bookingResultReceived) {
        setProposal(null);
      } else if (terminal.metrics.outcome === "ok") {
        if (nextProposal) setProposal(nextProposal);
        else if (input.kind === "ui") setProposal(null);
      }

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
      if (input.kind === "ui" && input.action.type === "confirm_proposal" && !rejectedBeforeConfirm) {
        setProposal(null);
        if (!bookingResultReceived) message = "Check your calendar to see whether the meeting was booked.";
      }
      updateTurn((turn) => ({ ...turn, error: { message, retryable: input.kind === "text" } }));
    } finally {
      request.current = null;
      setActiveKind(null);
      setStatus("");
    }
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
            {turns.map((turn) => (
              <div key={turn.id} className="grid gap-3">
                {turn.input.kind === "text" && (
                  <p className="max-w-[90%] justify-self-end rounded-2xl bg-primary/10 px-4 py-3 whitespace-pre-wrap wrap-break-word">
                    <span className="sr-only">You: </span>{turn.input.text}
                  </p>
                )}
                {turn.text && (
                  <p className="whitespace-pre-wrap wrap-break-word">
                    <span className="sr-only">Assistant: </span>{displayReferences(turn.text, turn.views)}
                  </p>
                )}
                {turn.results.map((result) => (
                  <article key={result.booking.ref} className="rounded-xl border border-border bg-card px-4 py-3">
                    <h2 className="font-semibold text-primary">{RESULT_LABELS[result.kind]}</h2>
                    <p>{result.booking.title}</p>
                    <p className="text-sm text-muted-foreground">{result.booking.whenLabel}</p>
                  </article>
                ))}
                {turn.error && (
                  <div className="rounded-xl border border-border bg-card px-4 py-3">
                    <p>{turn.error.message}</p>
                    {turn.error.retryable && turn.id === turns.at(-1)?.id && (
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
              </div>
            ))}
          </div>

          {proposal?.kind === "book" && (
            <div className="mt-4">
              <BookingConfirmation
                key={proposal.id}
                proposal={proposal}
                sending={sending}
                onAction={(action) => void sendTurn({ kind: "ui", action })}
              />
            </div>
          )}

          <div role="status" className={status ? "mt-4 text-sm text-muted-foreground" : "sr-only"}>
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
