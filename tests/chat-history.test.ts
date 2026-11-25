import { expect, it } from "vitest";
import { CHAT_HISTORY_KEY, type ChatHistory, clearHistory, loadHistory, saveHistory } from "../app/chat-history";

function storage() {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
  };
}

const views = { slots: {}, bookings: {} };
const history: ChatHistory = {
  version: 1, stateToken: "opaque-synthetic-state", timeZone: "Asia/Singapore", session: 1, nextTurnId: 2,
  views, activeProposal: null,
  turns: [{ id: 1, session: 1, input: { kind: "text", text: "Show my meetings" },
    text: "No upcoming meetings.", controls: "all", views, groups: [], results: [] }],
};

it("restores open chat history in its own namespace", () => {
  const store = storage();
  const formerProtectedKey = "livex.owner.chat.v1";
  saveHistory(store, formerProtectedKey, history);
  expect(loadHistory(store, CHAT_HISTORY_KEY)).toBeNull();
  saveHistory(store, CHAT_HISTORY_KEY, history);
  expect(loadHistory(store, CHAT_HISTORY_KEY)).toEqual(history);
  clearHistory(store, CHAT_HISTORY_KEY);
  expect(loadHistory(store, CHAT_HISTORY_KEY)).toBeNull();
  expect(loadHistory(store, formerProtectedKey)).toEqual(history);
});

it("ignores malformed history and never revives a confirmation whose active record was cleared", () => {
  const store = storage();
  store.setItem("page", "not valid JSON");
  expect(loadHistory(store, "page")).toBeNull();
  const proposal = { id: "P1", kind: "cancel" as const, title: "Fixture meeting", whenLabel: "Mon 30 Nov",
    timeZone: "Asia/Singapore", durationMinutes: 30, bookingRef: "B1" };
  const turns = [{ ...history.turns[0], proposal }];
  saveHistory(store, "page", { ...history, turns, activeProposal: null });
  expect(loadHistory(store, "page")?.activeProposal).toBeNull();
  expect(loadHistory(store, "page")?.turns[0].proposal).toEqual(proposal);
});

it("only restores a proposal from a live card in the current signed chat", () => {
  const store = storage();
  const proposal = { id: "P1", kind: "cancel" as const, title: "Fixture meeting", whenLabel: "Mon 30 Nov",
    timeZone: "Asia/Singapore", durationMinutes: 30, bookingRef: "B1" };
  const turns = [{ ...history.turns[0], proposal }];
  const pending = { ...history, turns, activeProposal: { turnId: 1, value: proposal } };
  saveHistory(store, "page", pending);
  expect(loadHistory(store, "page")?.activeProposal).toEqual(pending.activeProposal);

  for (const stale of [{ ...pending, stateToken: null }, { ...pending, session: 2 },
    { ...pending, turns: [{ ...turns[0], controls: "none" as const }] }]) {
    saveHistory(store, "page", stale);
    expect(loadHistory(store, "page")?.activeProposal).toBeNull();
  }
});
