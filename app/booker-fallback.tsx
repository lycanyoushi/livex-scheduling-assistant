"use client";

import { useEffect, useRef, useState } from "react";

import { OUTLINE_BUTTON } from "./chat-cards";

const CAL_ORIGIN = "https://app.cal.com";
const CAL_EMBED_SCRIPT = `${CAL_ORIGIN}/embed/embed.js`;
const CAL_HOSTS = new Set(["cal.com", "app.cal.com"]);

export function safePublicBookingUrl(value: unknown): string | null {
  if (typeof value !== "string" || value.length > 2_048 || /\s/.test(value)) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || !url.hostname || url.username || url.password) return null;
    url.hash = "";
    url.search = "";
    return url.href;
  } catch {
    return null;
  }
}

export function calEmbedLink(bookingUrl: string | null): string | null {
  const safe = safePublicBookingUrl(bookingUrl);
  if (!safe) return null;
  const url = new URL(safe);
  const parts = url.pathname.split("/").filter(Boolean);
  if (!CAL_HOSTS.has(url.hostname) || url.port || parts.length < 2 ||
      !parts.every((part) => /^[\w.-]{1,120}$/.test(part))) return null;
  return parts.join("/");
}

type Command = ((...args: unknown[]) => void) & { q: unknown[][] };
type Cal = Command & { ns: Record<string, Command>; loaded: boolean };
type CalWindow = Window & { Cal?: Cal };

let namespaceSequence = 0;
let scriptReady: Promise<void> | undefined;

function calClient(): { cal: Cal; ready: Promise<void> } {
  const browser = window as CalWindow;
  if (browser.Cal) return { cal: browser.Cal, ready: scriptReady ?? Promise.resolve() };

  // Match the official embed's root and namespace command queues until its script loads.
  const cal: Cal = Object.assign((...args: unknown[]) => {
    if (args[0] === "init" && typeof args[1] === "string") {
      const name = args[1];
      const queue: unknown[][] = [];
      cal.ns[name] ??= Object.assign((...call: unknown[]) => { queue.push(call); }, { q: queue });
      cal.ns[name].q.push(args);
      cal.q.push(["initNamespace", name]);
    } else {
      cal.q.push(args);
    }
  }, { q: [] as unknown[][], ns: {} as Record<string, Command>, loaded: true });

  browser.Cal = cal;
  const ready = new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = CAL_EMBED_SCRIPT;
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () => {
      script.remove();
      if (browser.Cal === cal) delete browser.Cal;
      scriptReady = undefined;
      reject(new Error("The Cal.com script could not be loaded."));
    };
    document.head.append(script);
  });
  scriptReady = ready;
  return { cal, ready };
}

export function BookerFallback({ bookingUrl, warming, open, onClose }: {
  bookingUrl: string | null;
  warming: boolean;
  open: boolean;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const container = useRef<HTMLDivElement>(null);
  const [embedStatus, setEmbedStatus] = useState<"loading" | "ready" | "failed">("loading");
  const safeUrl = safePublicBookingUrl(bookingUrl);
  const calLink = calEmbedLink(safeUrl);

  useEffect(() => {
    const element = dialog.current;
    if (open && element && !element.open) element.showModal();
    else if (!open && element?.open) element.close();
  }, [open]);

  useEffect(() => {
    const target = container.current;
    if (!calLink || !target) return;
    let active = true;
    const namespace = `livex-direct-${++namespaceSequence}`;
    const timer = window.setTimeout(() => {
      if (active) setEmbedStatus("failed");
    }, 10_000);

    function completed(status: "ready" | "failed") {
      if (active) {
        window.clearTimeout(timer);
        setEmbedStatus(status);
      }
    }

    const { cal, ready } = calClient();
    void ready.then(() => {
      if (!active) return;
      const api = (window as CalWindow).Cal ?? cal;
      api("init", namespace, { origin: CAL_ORIGIN });
      const inline = api.ns[namespace];
      inline("on", { action: "linkReady", callback: () => completed("ready") });
      inline("on", { action: "linkFailed", callback: () => completed("failed") });
      inline("inline", { elementOrSelector: target, calLink, config: { layout: "month_view" } });
    }).catch(() => completed("failed"));

    return () => {
      active = false;
      window.clearTimeout(timer);
      target.replaceChildren();
    };
  }, [calLink]);

  return (
    <dialog ref={dialog} aria-label="Book directly" onClose={onClose}
      className="m-auto max-h-[90dvh] w-[calc(100%_-_2rem)] max-w-[46rem] overflow-y-auto rounded-xl border border-border bg-card p-4 text-foreground shadow-xl backdrop:bg-black/45 sm:p-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">Book directly</h2>
        <button type="button" className={`${OUTLINE_BUTTON} border border-border`} onClick={() => dialog.current?.close()}>
          Close
        </button>
      </header>
      {safeUrl && (
        <a href={safeUrl} target="_blank" rel="noopener noreferrer" className="my-3 inline-block font-medium text-primary underline">
          Open booking page
        </a>
      )}
      {!safeUrl && <p role="status" className="mt-3 text-sm text-muted-foreground">{warming ? "Loading the booking page…" : "The booking page is unavailable."}</p>}
      {calLink && (
        <>
          {embedStatus !== "ready" && <p role="status" className="text-sm text-muted-foreground">
            {embedStatus === "loading" ? "Loading the calendar…" : "The calendar couldn’t load here. Use the booking page link above."}
          </p>}
          <div ref={container} className={embedStatus === "failed" ? "hidden" : "min-h-48"} />
        </>
      )}
    </dialog>
  );
}
