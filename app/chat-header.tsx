"use client";

import { useId, useRef, useState } from "react";

import { OUTLINE_BUTTON } from "./chat-cards";

const FALLBACK_ZONES = [
  "America/Los_Angeles", "America/New_York", "America/Sao_Paulo", "Asia/Dubai",
  "Asia/Kolkata", "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney", "Europe/London", "Europe/Paris",
];

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
  hostName, avatarUrl, demoMode, timeZone, onTimeZoneChange, onNewChat, busy,
  debugEnabled, debugOpen, onDebugToggle, embedded = false,
}: {
  hostName?: string;
  avatarUrl?: string;
  demoMode: boolean;
  timeZone: string;
  onTimeZoneChange: (zone: string) => void;
  onNewChat: () => void;
  busy: boolean;
  debugEnabled: boolean;
  debugOpen: boolean;
  onDebugToggle: () => void;
  embedded?: boolean;
}) {
  const label = hostName || "Scheduling Assistant";
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const titleId = useId();
  const searchId = useId();
  const [failedAvatar, setFailedAvatar] = useState<string>();
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

  function openPicker() {
    setQuery("");
    setOpen(true);
    dialog.current?.showModal();
    search.current?.focus();
  }

  function selectZone(zone: string) {
    if (zone !== timeZone) onTimeZoneChange(zone);
    dialog.current?.close();
  }

  return (
    <header className="shrink-0 border-b border-border bg-card">
      <div className={`mx-auto flex max-w-4xl flex-wrap items-center justify-between gap-2 px-4 py-2.5 ${embedded ? "" : "sm:px-8 sm:py-3"}`}>
        <div className="flex min-w-0 flex-1 basis-32 items-center gap-2.5">
          {avatarUrl?.startsWith("https://") && avatarUrl !== failedAvatar ? (
            // eslint-disable-next-line @next/next/no-img-element -- Cal.com sends the host's avatar URL at runtime.
            <img src={avatarUrl} alt="" width={36} height={36} referrerPolicy="no-referrer"
              onError={() => setFailedAvatar(avatarUrl)} className="size-9 shrink-0 rounded-full border border-border object-cover" />
          ) : (
            <span className="calendar-mark shrink-0" aria-hidden="true" />
          )}
          <p className="truncate text-sm font-semibold tracking-tight sm:text-base" title={label}>{label}</p>
          {demoMode && <span className="shrink-0 rounded-full border border-border px-2 py-0.5 text-xs text-muted-foreground">Demo</span>}
        </div>
        <div className="flex max-w-full flex-wrap items-center gap-1">
          <button ref={trigger} type="button" disabled={busy} aria-label={`Change time zone, currently ${timeZone}`}
            aria-haspopup="dialog" onClick={openPicker}
            className={`${OUTLINE_BUTTON} max-w-full truncate rounded-full border border-border px-3 text-primary`}>
            {timeZone.replaceAll("_", " ")}
          </button>
          <button type="button" disabled={busy} className={`${OUTLINE_BUTTON} px-2.5`} onClick={onNewChat}>New chat</button>
          {debugEnabled && (
            <button type="button" className={`${OUTLINE_BUTTON} px-2.5`} aria-expanded={debugOpen} onClick={onDebugToggle}>Debug</button>
          )}
        </div>
      </div>
      <dialog ref={dialog} aria-labelledby={titleId} onClose={() => {
        setOpen(false);
        trigger.current?.focus();
      }}
        className="m-auto max-h-[80dvh] w-[calc(100%_-_2rem)] max-w-sm overflow-y-auto rounded-xl border border-border bg-card p-4 text-foreground shadow-xl backdrop:bg-black/45">
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 id={titleId} className="text-lg font-semibold">Choose a time zone</h2>
          <button type="button" className={OUTLINE_BUTTON} onClick={() => dialog.current?.close()}>Close</button>
        </div>
        <div>
          <label htmlFor={searchId} className="sr-only">Search time zones</label>
          <input ref={search} id={searchId} type="search" autoComplete="off" maxLength={100} value={query}
            onChange={(event) => setQuery(event.target.value)} placeholder="Search by city or zone"
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.nativeEvent.isComposing && matchingZone) {
                event.preventDefault();
                selectZone(matchingZone);
              }
            }}
            className="mb-2 min-h-11 w-full rounded-md border border-border bg-background px-3 text-base focus-visible:outline-2 focus-visible:outline-ring" />
          {open && zones.length === 0 && <p role="status" className="p-2 text-sm text-muted-foreground">No matching time zones.</p>}
          <ul className="grid max-h-[46dvh] gap-0.5 overflow-y-auto overscroll-contain">
            {zones.map((zone) => (
              <li key={zone}>
                <button type="button" aria-pressed={zone === timeZone} onClick={() => selectZone(zone)}
                  className={`${OUTLINE_BUTTON} w-full truncate text-left font-normal hover:bg-primary/10 aria-pressed:bg-primary/10 aria-pressed:font-semibold`}>
                  {zone.replaceAll("_", " ")}
                </button>
              </li>
            ))}
          </ul>
        </div>
      </dialog>
    </header>
  );
}
