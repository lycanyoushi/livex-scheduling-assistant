import type { BookingResult, ChatInput, ProposalView, ReferenceViews, TurnControls } from "./chat-protocol";

export type TurnError = { message: string; retryable: boolean; code?: string; fallback?: "booker" };
export type SlotGroup = { refs: string[]; plainRefs?: string[]; note?: string };

export type TurnRecord = {
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

export type ActiveProposal = { turnId: number; value: ProposalView };

export type ChatHistory = {
  version: 1;
  timeZone: string;
  stateToken: string | null;
  session: number;
  nextTurnId: number;
  views: ReferenceViews;
  turns: TurnRecord[];
  activeProposal: ActiveProposal | null;
};

type BrowserStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export function browserStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function validTimeZone(value: unknown): value is string {
  if (typeof value !== "string" || value.length > 100 || !/^[A-Za-z][A-Za-z0-9_+\-/]*$/.test(value)) return false;
  try {
    new Intl.DateTimeFormat("en", { timeZone: value });
    return true;
  } catch {
    return false;
  }
}

export function loadHistory(storage: BrowserStorage, key: string): ChatHistory | null {
  try {
    const entry = JSON.parse(storage.getItem(key) ?? "null") as ChatHistory | null;
    if (entry?.version !== 1 || !validTimeZone(entry.timeZone) || !Array.isArray(entry.turns) ||
        !entry.views?.slots || !entry.views.bookings || !Number.isSafeInteger(entry.nextTurnId) ||
        !Number.isSafeInteger(entry.session) ||
        (entry.stateToken !== null && typeof entry.stateToken !== "string")) return null;
    const turns = entry.turns.filter((turn) => turn && Number.isSafeInteger(turn.id) &&
      (turn.input?.kind === "text" || turn.input?.kind === "ui") && typeof turn.text === "string" &&
      Array.isArray(turn.groups) && Array.isArray(turn.results) && turn.views?.slots && turn.views.bookings);
    const activeProposal = entry.activeProposal;
    const retainedProposal = entry.stateToken && turns.find((turn) => turn.id === activeProposal?.turnId &&
      turn.session === entry.session && turn.controls === "all")?.proposal;
    return { ...entry, turns, activeProposal: activeProposal && retainedProposal && retainedProposal.id === activeProposal.value?.id
      ? { turnId: activeProposal.turnId, value: retainedProposal } : null };
  } catch {
    return null;
  }
}

export function saveHistory(storage: BrowserStorage, key: string, entry: ChatHistory): void {
  try {
    // Session tokens and reference maps have separate server-side limits.
    const turns = entry.turns.slice(-40);
    storage.setItem(key, JSON.stringify({ ...entry, turns }));
  } catch {
    // Chat remains usable if storage is blocked or exceeds the browser's quota.
  }
}

export function clearHistory(storage: BrowserStorage, key: string): void {
  try {
    storage.removeItem(key);
  } catch {
    // A restricted browser may deny access to local storage.
  }
}
