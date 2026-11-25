"use client";

import { Bug, CalendarDays, Check, Globe, Search, SquarePen, X } from "lucide-react";
import { useId, useRef, useState } from "react";

import { OUTLINE_BUTTON } from "./chat-cards";

const FALLBACK_ZONES = [
  "America/Los_Angeles", "America/New_York", "America/Sao_Paulo", "Asia/Dubai",
  "Asia/Kolkata", "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney", "Europe/London", "Europe/Paris",
];

const ICON_BUTTON = "inline-grid size-11 shrink-0 cursor-pointer place-items-center rounded-xl text-muted-foreground hover:bg-primary/10 hover:text-ring focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:cursor-default disabled:opacity-50";

export function AssistantAvatar({ className = "size-10" }: { className?: string }) {
  return (
    <span className={`grid shrink-0 place-items-center rounded-full bg-primary/10 text-ring ${className}`} aria-hidden="true">
      <CalendarDays className="size-[52%]" strokeWidth={1.9} />
    </span>
  );
}

function zoneSearchLabel(zone: string): string {
  return zone.replaceAll("_", " ").toLowerCase();
}

function exactZone(query: string): string | undefined {
  if (!/^[a-z][a-z0-9_+./-]*$/i.test(query)) return undefined;
  try {
    return new Intl.DateTimeFormat("en", { timeZone: query }).resolvedOptions().timeZone;
  } catch {
    return undefined;
  }
}

export function ChatHeader({
  hostName, demoMode, timeZone, onTimeZoneChange, onNewChat, busy,
  debugEnabled, debugOpen, onDebugToggle,
}: {
  hostName?: string;
  demoMode: boolean;
  timeZone: string;
  onTimeZoneChange: (zone: string) => void;
  onNewChat: () => void;
  busy: boolean;
  debugEnabled: boolean;
  debugOpen: boolean;
  onDebugToggle: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const titleId = useId();
  const searchId = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const normalized = zoneSearchLabel(query.trim());
  const exact = normalized ? exactZone(query.trim()) : undefined;
  const zones = open
    ? [...new Set([timeZone, "UTC", ...(Intl.supportedValuesOf?.("timeZone") ?? FALLBACK_ZONES)])]
        .filter((zone) => zoneSearchLabel(zone).includes(normalized))
    : [];
  if (exact && !zones.includes(exact)) zones.unshift(exact);
  const matchingZone = exact ?? (zones.length === 1 ? zones[0] : undefined);
  const subtitle = `Managing ${hostName ? `${hostName}'s` : "your"} Cal.com`;

  function openPicker() {
    setQuery("");
    setOpen(true);
    dialog.current?.showModal();
    search.current?.focus();
  }

  function selectZone(zone: string) {
    onTimeZoneChange(zone);
    dialog.current?.close();
  }

  return (
    <header className="sticky top-0 z-10 shrink-0 border-b border-border bg-card">
      <div className="mx-auto flex max-w-4xl flex-wrap items-center justify-between gap-x-4 gap-y-1 px-4 py-2.5 sm:px-8 sm:py-3">
        <div className="flex min-w-0 flex-1 basis-44 items-center gap-3">
          <AssistantAvatar />
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <p className="truncate text-sm font-semibold tracking-tight sm:text-base">Scheduling Assistant</p>
              {demoMode && <span className="shrink-0 rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-ring">Demo</span>}
            </div>
            <p className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground" title={subtitle}>
              <span className={`size-1.5 shrink-0 rounded-full ${hostName ? "bg-emerald-600" : "bg-muted-foreground/50"}`} aria-hidden="true" />
              <span className="truncate">{subtitle}</span>
            </p>
          </div>
        </div>
        <div className="ml-auto flex max-w-full flex-wrap items-center justify-end gap-1">
          <button ref={trigger} type="button" disabled={busy} aria-label={`Change time zone, currently ${timeZone}`}
            aria-haspopup="dialog" aria-expanded={open} onClick={openPicker}
            className="inline-flex min-h-11 max-w-full min-w-0 cursor-pointer items-center gap-2 rounded-full border border-border px-3 text-xs font-medium text-muted-foreground tabular-nums hover:border-ring/50 hover:text-ring focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:cursor-default disabled:opacity-50">
            <Globe className="size-4 shrink-0" aria-hidden="true" />
            <span className="max-w-32 truncate sm:max-w-44">{timeZone.replaceAll("_", " ")}</span>
          </button>
          <div className="group/new-chat relative">
            <button type="button" disabled={busy} className={ICON_BUTTON} aria-label="New chat" onClick={onNewChat}>
              <SquarePen className="size-[18px]" aria-hidden="true" />
            </button>
            <span aria-hidden="true" className="pointer-events-none absolute top-full right-0 z-20 mt-1 hidden rounded-md bg-foreground px-2 py-1 text-xs whitespace-nowrap text-background shadow-sm group-hover/new-chat:block group-focus-within/new-chat:block">
              New chat
            </span>
          </div>
          {debugEnabled && (
            <button type="button" className={`${ICON_BUTTON} aria-expanded:bg-primary/10 aria-expanded:text-ring`}
              aria-expanded={debugOpen} aria-label="Debug" title="Debug" onClick={onDebugToggle}>
              <Bug className="size-[18px]" aria-hidden="true" />
            </button>
          )}
        </div>
      </div>
      <dialog ref={dialog} aria-labelledby={titleId} onClose={() => {
        setOpen(false);
        trigger.current?.focus();
      }}
        className="m-auto max-h-[80dvh] w-[calc(100%_-_2rem)] max-w-sm overflow-y-auto rounded-2xl border border-border bg-card p-4 text-foreground shadow-xl backdrop:bg-black/45">
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 id={titleId} className="text-lg font-semibold">Choose a time zone</h2>
          <button type="button" className={ICON_BUTTON} aria-label="Close" title="Close" onClick={() => dialog.current?.close()}>
            <X className="size-5" aria-hidden="true" />
          </button>
        </div>
        <div>
          <label htmlFor={searchId} className="sr-only">Search time zones</label>
          <div className="relative mb-2">
            <Search className="pointer-events-none absolute top-3.5 left-3 size-4 text-muted-foreground" aria-hidden="true" />
            <input ref={search} id={searchId} type="search" autoComplete="off" maxLength={100} value={query}
              onChange={(event) => setQuery(event.target.value)} placeholder="Search by city or zone"
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.nativeEvent.isComposing && matchingZone) {
                  event.preventDefault();
                  selectZone(matchingZone);
                }
              }}
              className="min-h-11 w-full rounded-xl border border-border bg-background pr-3 pl-9 text-base focus-visible:outline-2 focus-visible:outline-ring" />
          </div>
          {open && zones.length === 0 && <p role="status" className="p-2 text-sm text-muted-foreground">No matching time zones.</p>}
          <ul className="grid max-h-[46dvh] gap-0.5 overflow-y-auto overscroll-contain">
            {zones.map((zone) => (
              <li key={zone}>
                <button type="button" aria-pressed={zone === timeZone} onClick={() => selectZone(zone)}
                  className={`${OUTLINE_BUTTON} flex w-full cursor-pointer items-center justify-between gap-2 text-left font-normal hover:bg-primary/10 aria-pressed:bg-primary/10 aria-pressed:font-semibold`}>
                  <span className="truncate">{zone.replaceAll("_", " ")}</span>
                  {zone === timeZone && <Check className="size-4 shrink-0 text-ring" aria-hidden="true" />}
                </button>
              </li>
            ))}
          </ul>
        </div>
      </dialog>
    </header>
  );
}
