import { expect, it } from "vitest";
import { type ChatHistory, clearHistory, loadHistory, saveHistory } from "../app/chat-history";

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

it("restores the zone, token, transcript and refs without crossing widget and page storage", () => {
  const store = storage();
  saveHistory(store, "page", history);
  expect(loadHistory(store, "page")).toEqual(history);
  expect(loadHistory(store, "widget")).toBeNull();
  clearHistory(store, "page");
  expect(loadHistory(store, "page")).toBeNull();
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
