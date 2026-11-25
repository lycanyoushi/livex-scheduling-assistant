"use client";

import { ArrowDown, ArrowUp, ArrowUpRight, CalendarClock, CalendarDays, CalendarPlus, CalendarX, Square, TriangleAlert } from "lucide-react";
import { type FormEvent, type ReactNode, useEffect, useRef, useState } from "react";

import {
  type ChatInput,
  type ProposalView,
  type ReferenceViews,
  type UiAction,
  bookingContact,
  canRetryTurn,
  clearsProposalOnSuccess,
  controlsForTurn,
  listedBookingRefs,
  proposalDecision,
  readChatStream,
  splitReferenceText,
  typedInput,
  unverifiedWrite,
  withoutRetiredBookings,
} from "./chat-protocol";
import { BookerFallback, safePublicBookingUrl } from "./booker-fallback";
import { BookingCard, ConfirmCard, OUTLINE_BUTTON, RefText, SlotChips } from "./chat-cards";
import { ChatDebug, type DebugSnapshot } from "./chat-debug";
import { AssistantAvatar, ChatHeader } from "./chat-header";
import {
  type ActiveProposal, type TurnError, type TurnRecord,
  browserStorage, clearHistory, loadHistory, saveHistory, validTimeZone,
} from "./chat-history";

const EMPTY_VIEWS: ReferenceViews = { slots: {}, bookings: {} };
const RATE_LIMITED = "Too many requests right now. Wait a moment, then retry.";
const SUGGESTIONS = [
  { title: "Book a meeting", prompt: "Book a 30-minute meeting", label: "Book a 30-min meeting", icon: CalendarPlus },
  { title: "View meetings", prompt: "What meetings do I have?", label: "What's on my calendar?", icon: CalendarDays },
  { title: "Reschedule", prompt: "Reschedule my meeting", label: "Reschedule my next meeting", icon: CalendarClock },
  { title: "Cancel a meeting", prompt: "Cancel my meeting", label: "Cancel my meeting", icon: CalendarX },
];
const LAST_EVENT_LIMIT = 200;
const ICON_TARGET = "inline-grid size-11 shrink-0 cursor-pointer place-items-center focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:cursor-default disabled:opacity-45";

function browserTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

function keepLatestReferences<T>(views: Record<string, T>, limit: number): Record<string, T> {
  const entries = Object.entries(views).sort(([a], [b]) => Number(a.slice(1)) - Number(b.slice(1)));
  return Object.fromEntries(entries.slice(-limit));
}

function AssistantRow({ children }: { children: ReactNode }) {
  return (
    <div className="grid min-w-0 grid-cols-[1.875rem_minmax(0,1fr)] items-start gap-2.5 sm:gap-3">
      <AssistantAvatar className="size-[30px]" />
      <div className="grid min-w-0 gap-3 pt-0.5">{children}</div>
    </div>
  );
}

export default function ChatWorkspace({ embedded = false, onClose }: { embedded?: boolean; onClose?: () => void }) {
  const storageKey = embedded ? "livex.widget.v1" : "livex.chat.v1";
  const titleId = embedded ? "widget-chat-title" : "chat-title";
  const messageId = embedded ? "widget-chat-message" : "chat-message";
  const [draft, setDraft] = useState("");
  const [turns, setTurns] = useState<TurnRecord[]>([]);
  const [activeProposal, setActiveProposal] = useState<ActiveProposal | null>(null);
  const [currentViews, setCurrentViews] = useState(EMPTY_VIEWS);
  const [session, setSession] = useState(0);
  const [timeZone, setTimeZone] = useState("UTC");
  const [ready, setReady] = useState(false);
  const [host, setHost] = useState<{ hostName?: string; avatarUrl?: string; demoMode: boolean }>({ demoMode: false });
  const [debugEnabled, setDebugEnabled] = useState(false);
  const [debugOpen, setDebugOpen] = useState(false);
  const [debug, setDebug] = useState<DebugSnapshot | null>(null);
  const [behindLatest, setBehindLatest] = useState(false);
  const [activeKind, setActiveKind] = useState<ChatInput["kind"] | null>(null);
  const [status, setStatus] = useState("");
  const [bookingUrl, setBookingUrl] = useState<string | null>(null);
  const [booker, setBooker] = useState<"unused" | "open" | "closed">("unused");
  const [warming, setWarming] = useState(true);
  const [warmAttempt, setWarmAttempt] = useState(0);
  const token = useRef<string | null>(null);
  const savedViews = useRef(EMPTY_VIEWS);
  const nextTurnId = useRef(1);
  const request = useRef<AbortController | null>(null);
  const activeBookingForm = useRef<HTMLFormElement>(null);
  const transcript = useRef<HTMLElement>(null);
  const followsTranscript = useRef(true);
  const sending = activeKind !== null;

  useEffect(() => () => request.current?.abort(), []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const storage = browserStorage();
      const saved = storage && loadHistory(storage, storageKey);
      setTimeZone(saved?.timeZone ?? browserTimeZone());
      setDebugEnabled(new URLSearchParams(window.location.search).get("debug") === "1");
      if (saved) {
        token.current = saved.stateToken;
        savedViews.current = saved.views;
        nextTurnId.current = saved.nextTurnId;
        setCurrentViews(saved.views);
        setSession(saved.session);
        setTurns(saved.turns);
        setActiveProposal(saved.activeProposal);
      }
      setReady(true);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [storageKey]);

  useEffect(() => {
    if (!ready || sending) return;
    const storage = browserStorage();
    if (storage) saveHistory(storage, storageKey, {
      version: 1, timeZone, stateToken: token.current, session, nextTurnId: nextTurnId.current,
      views: savedViews.current, turns, activeProposal,
    });
  }, [activeProposal, ready, sending, session, storageKey, timeZone, turns]);

  useEffect(() => {
    if (!ready) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      const query = new URLSearchParams({ timeZone });
      // Warming up the calendar is independent of the chat turn.
      void fetch(`/api/warm?${query}`, { cache: "no-store", signal: controller.signal })
        .then((response) => response.ok ? response.json() as Promise<{
          bookingUrl?: unknown; hostName?: string; avatarUrl?: string; demoMode?: boolean;
        }> : null)
        .then((metadata) => {
          if (!controller.signal.aborted) {
            setBookingUrl(safePublicBookingUrl(metadata?.bookingUrl));
            if (metadata) setHost({ hostName: metadata.hostName, avatarUrl: metadata.avatarUrl,
                                    demoMode: metadata.demoMode === true });
          }
        })
        .catch(() => {})
        .finally(() => {
          if (!controller.signal.aborted) setWarming(false);
        });
    }, 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [ready, timeZone, warmAttempt]);

  useEffect(() => {
    const log = transcript.current;
    if (log && followsTranscript.current) log.scrollTop = log.scrollHeight;
  }, [turns, status]);

  async function sendTurn(requestedInput: ChatInput, retryId?: number) {
    if (request.current || !ready) return;

    const candidate = activeProposal?.value;
    const input = requestedInput.kind === "text"
      ? typedInput(requestedInput.text, token.current && requestedInput.proposalId === candidate?.id
        ? requestedInput.proposalId : undefined)
      : requestedInput;
    const decision = proposalDecision(input);
    const form = candidate?.kind === "book" ? activeBookingForm.current : null;
    const wireInput = input.kind === "text" && decision === "confirm" && form
      ? typedInput(input.text, input.proposalId, bookingContact(new FormData(form)))
      : input;
    const controller = new AbortController();
    request.current = controller;
    followsTranscript.current = true;
    setBehindLatest(false);
    const id = retryId ?? nextTurnId.current++;
    const record: TurnRecord = {
      id, session, input, text: "", results: [], groups: [], views: savedViews.current, controls: "none",
    };
    setTurns((previous) => retryId === undefined
      ? [...previous, record]
      : previous.map((turn) => turn.id === id ? record : turn));
    setActiveKind(decision ? "ui" : input.kind);
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
    const retiredBookings = new Set<string>();
    const confirmsProposal = decision === "confirm";
    const originalBookingRef = confirmsProposal ? candidate?.bookingRef : undefined;
    const debugEvents: object[] = [];
    let omittedEvents = 0;
    if (confirmsProposal) {
      // A reload during a possible write must leave its confirmation inactive.
      const storage = browserStorage();
      if (storage) saveHistory(storage, storageKey, {
        version: 1, timeZone, stateToken: token.current, session, nextTurnId: nextTurnId.current,
        views: savedViews.current, turns, activeProposal: null,
      });
    }

    function clearUnverifiedConfirmation() {
      setActiveProposal(null);
      const views = { slots: { ...savedViews.current.slots }, bookings: { ...savedViews.current.bookings } };
      if (candidate?.slotRef) delete views.slots[candidate.slotRef];
      if (candidate?.bookingRef) delete views.bookings[candidate.bookingRef];
      savedViews.current = views;
      setCurrentViews(views);
    }

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
        signal: controller.signal,
        body: JSON.stringify({
          stateToken: token.current,
          timeZone,
          input: wireInput,
        }),
      });
      if (response.status === 429) {
        if (debugEnabled) setDebug({ metrics: null, events: [{ type: "http_error", status: 429 }], omittedEvents: 0 });
        updateTurn((turn) => ({ ...turn, error: {
          code: "rate_limited", message: RATE_LIMITED, retryable: canRetryTurn(input, { kind: "quota" }),
        } }));
        return;
      }
      if (!response.ok || !response.body) {
        rejectedBeforeConfirm = response.status >= 400 && response.status < 500;
        throw new Error("The chat endpoint did not accept the request.");
      }

      const terminal = await readChatStream(response.body, (event) => {
        if (debugEnabled) {
          if (debugEvents.length < LAST_EVENT_LIMIT) debugEvents.push(event);
          else omittedEvents++;
        }
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
            if (event.retiredBookings?.length) {
              for (const ref of event.retiredBookings) retiredBookings.add(ref);
              savedViews.current = {
                ...savedViews.current,
                bookings: withoutRetiredBookings(savedViews.current.bookings, retiredBookings),
              };
              setCurrentViews(savedViews.current);
              setActiveProposal((current) =>
                current?.value.bookingRef && retiredBookings.has(current.value.bookingRef) ? null : current
              );
            }
            const nextViews = {
              slots: { ...views.slots, ...event.slots },
              bookings: withoutRetiredBookings({ ...views.bookings, ...event.bookings }, retiredBookings),
            };
            views = nextViews;
            updateTurn((turn) => ({
              ...turn,
              views: nextViews,
              results: turn.results.filter((result) => !retiredBookings.has(result.booking.ref)),
            }));
            break;
          }
          case "booking_cards": {
            if (event.refs.length) setStatus("");
            updateTurn((turn) => ({
              ...turn,
              listedBookings: [...new Set([...(turn.listedBookings ?? []), ...event.refs])],
            }));
            break;
          }
          case "slot_chips": {
            setStatus("");
            for (const ref of event.refs) recoveryRefs.add(ref);
            updateTurn((turn) => ({
              ...turn,
              groups: [...turn.groups, { refs: event.refs, plainRefs: event.plainRefs, note: event.note }],
            }));
            break;
          }
          case "proposal":
            nextProposal = event.proposal;
            break;
          case "booking_result": {
            bookingResultReceived = true;
            if (retiredBookings.has(event.booking.ref)) break;
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
            failure = { code: event.code, message: event.message, retryable: event.retryable, fallback: event.fallback };
            resetSession = event.code === "session_expired" || event.code === "interaction_expired";
            break;
        }
      });

      if (debugEnabled) setDebug({ metrics: terminal.metrics,
        events: [...debugEvents, { type: "turn_end", metrics: terminal.metrics }], omittedEvents });
      token.current = terminal.stateToken;
      const controls = controlsForTurn(input, terminal.metrics, failure?.code);
      const uncertainConfirmation = confirmsProposal && unverifiedWrite(failure?.code);
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
      const proposalBookingRetired = Boolean(nextProposal?.bookingRef && retiredBookings.has(nextProposal.bookingRef));
      const committedProposal = controls === "all" && !proposalBookingRetired ? nextProposal : undefined;
      if (proposalLostItsSlot && activeProposal) {
        setTurns((previous) => previous.map((turn) => turn.id === activeProposal.turnId ? { ...turn, proposal: undefined } : turn));
      }
      if (uncertainConfirmation) {
        clearUnverifiedConfirmation();
      } else if (resetSession || bookingResultReceived || proposalLostItsSlot) {
        setActiveProposal(null);
      } else if (controls === "all") {
        if (committedProposal) setActiveProposal({ turnId: id, value: committedProposal });
        else if (clearsProposalOnSuccess(wireInput, terminal.metrics.path)) setActiveProposal(null);
      }
      updateTurn((turn) => ({ ...turn, controls, proposal: committedProposal }));

      if (failure) {
        const error: TurnError = {
          ...failure,
          retryable: canRetryTurn(input, {
            kind: "terminal", retryable: failure.retryable && !uncertainConfirmation && !bookingResultReceived,
          }),
          fallback: uncertainConfirmation ? undefined : failure.fallback,
        };
        updateTurn((turn) => ({ ...turn, error }));
      } else if (terminal.metrics.outcome === "failed" && terminal.metrics.fallback !== "code_only") {
        updateTurn((turn) => ({
          ...turn,
          error: { message: "The request could not be completed.", retryable: input.kind === "text" && !confirmsProposal },
        }));
      }
    } catch {
      if (debugEnabled) setDebug({ metrics: null, events: [...debugEvents, {
        type: controller.signal.aborted ? "cancelled" : "transport_error",
      }], omittedEvents });
      let message = controller.signal.aborted ? "Response stopped." : "The chat connection closed.";
      if (confirmsProposal && !rejectedBeforeConfirm) {
        clearUnverifiedConfirmation();
        if (!bookingResultReceived) message = "Check your calendar to see whether the change went through.";
      }
      updateTurn((turn) => ({ ...turn, error: { message, retryable: canRetryTurn(input, { kind: "transport" }) } }));
    } finally {
      request.current = null;
      setActiveKind(null);
      setStatus("");
    }
  }

  function takeAction(action: UiAction) {
    void sendTurn({ kind: "ui", action });
  }

  function openBooker() {
    setBooker("open");
    if (!bookingUrl && !warming) {
      setWarming(true);
      setWarmAttempt((previous) => previous + 1);
    }
  }

  function sendDraft(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || request.current || !ready) return;
    setDraft("");
    void sendTurn(typedInput(text, token.current ? activeProposal?.value.id : undefined));
  }

  function newChat() {
    if (!ready || request.current) return;
    const storage = browserStorage();
    if (storage) clearHistory(storage, storageKey);
    token.current = null;
    savedViews.current = EMPTY_VIEWS;
    nextTurnId.current = 1;
    followsTranscript.current = true;
    setBehindLatest(false);
    setTurns([]);
    setSession((old) => old + 1);
    setCurrentViews(EMPTY_VIEWS);
    setActiveProposal(null);
    setDraft("");
    setDebug(null);
    setStatus("");
  }

  function changeTimeZone(zone: string) {
    if (request.current || zone === timeZone || !validTimeZone(zone)) return;
    setWarming(true);
    setTimeZone(zone);
  }

  const Container = embedded ? "div" : "main";
  const latestTurn = turns.at(-1);
  const waitingForText = sending && !latestTurn?.text.trim();
  const selectedRef = activeProposal?.value.slotRef ?? turns.findLast((turn) =>
    turn.session === session && turn.input.kind === "ui" && turn.input.action.type === "pick_slot" && turn.proposal,
  )?.proposal?.slotRef;
  return (
    <Container className={`relative flex min-h-0 flex-col overflow-hidden bg-background ${embedded ? "h-full" : "h-dvh"}`}>
      <ChatHeader {...host} timeZone={timeZone} onTimeZoneChange={changeTimeZone} onNewChat={newChat}
        busy={sending} debugEnabled={debugEnabled} debugOpen={debugOpen}
        onDebugToggle={() => setDebugOpen((visible) => !visible)} embedded={embedded} onClose={onClose} />

      <section
        ref={transcript}
        aria-labelledby={titleId}
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain"
        onScroll={(event) => {
          const { scrollHeight, scrollTop, clientHeight } = event.currentTarget;
          followsTranscript.current = scrollHeight - scrollTop - clientHeight < 48;
          setBehindLatest(!followsTranscript.current);
        }}
      >
        <div className={`mx-auto flex min-h-full w-full max-w-3xl flex-col px-3.5 py-5 ${embedded ? "" : "sm:px-7 sm:py-8"}`}>
          {turns.length ? <h1 id={titleId} className="sr-only">Scheduling conversation</h1> : (
            <div className={`my-auto grid justify-items-center gap-5 py-4 text-center ${embedded ? "" : "sm:gap-7 sm:py-8"}`}>
              <AssistantAvatar className={embedded ? "size-11" : "size-12 sm:size-14"} />
              <h1 id={titleId} className={`max-w-[34rem] font-semibold leading-[1.25] tracking-[-0.035em] ${embedded ? "text-[1.35rem]" : "text-[1.65rem] sm:text-[2.25rem]"}`}>
                Hi! I can book, show, reschedule or cancel meetings.
              </h1>
              <div role="group" aria-label="Suggested prompts" className={`grid w-full max-w-[36rem] grid-cols-1 gap-2 ${embedded ? "" : "sm:grid-cols-2 sm:gap-3"}`}>
                {SUGGESTIONS.map(({ title, prompt, label, icon: Icon }) => (
                  <button key={prompt} type="button" aria-label={label}
                    className="group flex min-h-14 cursor-pointer items-center gap-3 rounded-2xl border border-border bg-card px-3 py-2.5 text-left transition-colors hover:border-ring/45 hover:bg-ring/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring sm:px-4"
                    onClick={() => void sendTurn({ kind: "text", text: prompt })}>
                    <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-ring/10 text-ring" aria-hidden="true">
                      <Icon className="size-[18px]" />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm leading-5 font-semibold">{title}</span>
                      <span className="block text-xs leading-[1.45] text-muted-foreground">{prompt}</span>
                    </span>
                    <ArrowUpRight className="size-4 shrink-0 text-muted-foreground transition-colors group-hover:text-ring" aria-hidden="true" />
                  </button>
                ))}
              </div>
            </div>
          )}

          <div role="log" aria-label="Messages" className="grid gap-6">
            {turns.map((turn) => {
              const isLatest = turn.id === latestTurn?.id;
              const sameSession = turn.session === session;
              const isActiveProposal = sameSession && activeProposal?.turnId === turn.id &&
                activeProposal.value.id === turn.proposal?.id;
              const live = sameSession && !sending;
              const currentBookings = sameSession ? currentViews.bookings : EMPTY_VIEWS.bookings;
              const parts = splitReferenceText(turn.text, sending && isLatest);
              const listed = listedBookingRefs(turn.listedBookings, turn.views.bookings);
              const emptyList = turn.listedBookings?.length === 0 && turn.controls === "all";
              const hasAssistant = Boolean(parts.length || turn.groups.length || listed.length || emptyList ||
                turn.results.length || turn.error || turn.proposal);
              const slotAvailable = (ref: string) => live && Boolean(currentViews.slots[ref]);
              const bookingAvailable = (ref: string) => live && turn.controls === "all" && Boolean(currentBookings[ref]);
              const slotGroups = turn.groups.map((group, index) => (
                <SlotChips
                  key={index}
                  refs={group.refs}
                  plainRefs={group.plainRefs}
                  slots={turn.views.slots}
                  note={group.note === turn.error?.message ? undefined : group.note}
                  timeZone={timeZone}
                  selectedRef={sameSession ? selectedRef : undefined}
                  available={(ref) => turn.controls !== "none" && slotAvailable(ref)}
                  allowMore={turn.controls !== "alternatives"}
                  onAction={takeAction}
                />
              ));

              return (
                <div key={turn.id} className="chat-appear grid min-w-0 gap-4">
                  {turn.input.kind === "text" && (
                    <p className="max-w-[88%] justify-self-end rounded-[1.15rem] rounded-br-md bg-primary px-4 py-2.5 leading-[1.6] whitespace-pre-wrap wrap-break-word text-primary-foreground sm:max-w-[79%]">
                      <span className="sr-only">You: </span>{turn.input.text}
                    </p>
                  )}
                  {hasAssistant && (
                    <AssistantRow>
                      {turn.input.kind === "text" && slotGroups}
                      {parts.length > 0 && (
                        <RefText
                          parts={parts}
                          views={turn.views}
                          currentBookings={currentBookings}
                          listedBookings={listed}
                          selectedRef={sameSession ? selectedRef : undefined}
                          slotAvailable={(ref) => turn.controls === "all" && slotAvailable(ref)}
                          bookingAvailable={bookingAvailable}
                          showMore={!turn.groups.some((group) => group.refs.length) && turn.controls !== "alternatives"}
                          onAction={takeAction}
                        />
                      )}
                      {listed.length > 0 && (
                        <ul aria-label="Meetings" className="grid gap-2.5">
                          {listed.map((ref) => (
                            <li key={ref}>
                              <BookingCard
                                booking={currentBookings[ref] ?? turn.views.bookings[ref]}
                                available={bookingAvailable(ref)}
                                linksAvailable={Boolean(currentBookings[ref])}
                                onAction={takeAction}
                              />
                            </li>
                          ))}
                        </ul>
                      )}
                      {emptyList && (
                        <div className="space-y-2">
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
                            linksAvailable={Boolean(currentBookings[result.booking.ref])}
                            onAction={takeAction}
                          />
                      ))}
                      {turn.error && (
                        <div role="alert" className="grid grid-cols-[1.125rem_minmax(0,1fr)] gap-2.5 rounded-2xl border border-amber-600/25 bg-amber-500/5 px-3.5 py-3 sm:px-4">
                          <TriangleAlert className="mt-1 size-[18px] text-amber-700 dark:text-amber-400" aria-hidden="true" />
                          <div>
                            <p className="text-sm leading-relaxed">{turn.error.message}</p>
                            <div className="flex flex-wrap gap-2">
                              {turn.error.retryable && isLatest && (
                                <button
                                  type="button"
                                  disabled={sending}
                                  className={`${OUTLINE_BUTTON} mt-2 border border-border bg-card`}
                                  onClick={() => void sendTurn(turn.input, turn.id)}
                                >
                                  Retry
                                </button>
                              )}
                              {turn.error.fallback === "booker" && (
                                <button type="button" className={`${OUTLINE_BUTTON} mt-2 border border-border bg-card`} onClick={openBooker}>
                                  Book directly
                                </button>
                              )}
                            </div>
                          </div>
                        </div>
                      )}
                      {turn.input.kind === "ui" && slotGroups}
                      {turn.proposal && (
                        <ConfirmCard
                          key={turn.proposal.id}
                          proposal={turn.proposal}
                          active={isActiveProposal}
                          submitting={sending}
                          originalBooking={turn.proposal.bookingRef ? turn.views.bookings[turn.proposal.bookingRef] : undefined}
                          selectedSlot={turn.proposal.slotRef ? turn.views.slots[turn.proposal.slotRef] : undefined}
                          formRef={isActiveProposal && turn.proposal.kind === "book" ? activeBookingForm : undefined}
                          onAction={takeAction}
                        />
                      )}
                    </AssistantRow>
                  )}
                </div>
              );
            })}
          </div>

          <div role="status" className={waitingForText ? "chat-appear mt-4" : "sr-only"}>
            {waitingForText ? (
              <AssistantRow>
                <div className="flex min-h-[1.625rem] flex-wrap items-center gap-x-3 gap-y-1 text-[13px] text-muted-foreground">
                  <span className="inline-flex items-center gap-1" aria-hidden="true">
                    {[0, 1, 2].map((dot) => <span key={dot} className="chat-typing-dot size-[5px] rounded-full bg-ring" />)}
                  </span>
                  {status || "Thinking…"}
                </div>
              </AssistantRow>
            ) : status}
          </div>
        </div>
      </section>

      <form aria-label="Message composer" onSubmit={sendDraft}
        className={`relative mx-auto w-full max-w-3xl shrink-0 bg-background px-3.5 pt-2 pb-[max(0.75rem,env(safe-area-inset-bottom))] ${embedded ? "" : "sm:px-7 sm:pb-5"}`}>
        {behindLatest && (
          <button type="button" aria-label="↓ Latest messages" title="Latest messages"
            className={`${ICON_TARGET} absolute -top-12 right-5 rounded-full border border-border bg-card text-muted-foreground shadow-md hover:text-ring sm:right-8`}
            onClick={() => {
              followsTranscript.current = true;
              setBehindLatest(false);
              const log = transcript.current;
              if (log) log.scrollTop = log.scrollHeight;
            }}><ArrowDown className="size-5" aria-hidden="true" /></button>
        )}
        <div className="flex items-end gap-2 rounded-[1.35rem] border border-border bg-card p-1.5 shadow-sm transition-colors focus-within:border-ring/60 sm:p-2">
          <label htmlFor={messageId} className="sr-only">Message</label>
          <textarea
            id={messageId}
            className="chat-composer-input min-h-11 min-w-0 flex-1 resize-none rounded-xl px-2.5 py-2.5 text-base leading-[1.6] placeholder:text-muted-foreground focus:outline-none"
            placeholder="Type a message"
            rows={1}
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
            <button type="button" aria-label="Stop" title="Stop"
              className={`${ICON_TARGET} rounded-[0.85rem] border border-border bg-background text-foreground hover:border-ring/50`}
              onClick={() => request.current?.abort()}>
              <Square className="size-3.5 fill-current" aria-hidden="true" />
            </button>
          ) : (
            <button type="submit" aria-label="Send" title="Send" disabled={sending || !draft.trim()}
              className={`${ICON_TARGET} rounded-full bg-primary text-primary-foreground hover:brightness-110`}>
              <ArrowUp className="size-5" strokeWidth={2.2} aria-hidden="true" />
            </button>
          )}
        </div>
        <p className="mt-1.5 text-center text-[11px] leading-4 text-muted-foreground sm:text-xs">
          Nothing is booked or changed until you confirm.
        </p>
      </form>
      {booker !== "unused" && (
        <BookerFallback bookingUrl={bookingUrl} warming={warming} open={booker === "open"} onClose={() => setBooker("closed")} />
      )}
      {debugEnabled && debugOpen && <ChatDebug snapshot={debug} onClose={() => setDebugOpen(false)} />}
    </Container>
  );
}
