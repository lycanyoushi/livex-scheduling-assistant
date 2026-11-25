"use client";

import { ArrowUpRight, MessageCircle } from "lucide-react";
import { type MouseEvent, useId, useLayoutEffect, useRef, useState } from "react";

import { PRIMARY_BUTTON } from "../chat-cards";
import ChatWorkspace from "../chat-workspace";

function showChatPanel(panel: HTMLDialogElement | null) {
  if (!panel || panel.open) return;
  panel.showModal();
  panel.querySelector<HTMLButtonElement>('header button[aria-label="Close"]')?.focus();
}

export default function WidgetDemo() {
  const panel = useRef<HTMLDialogElement>(null);
  const opener = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const [hasOpened, setHasOpened] = useState(false);

  useLayoutEffect(() => {
    if (hasOpened) showChatPanel(panel.current);
  }, [hasOpened]);

  function openChat(event: MouseEvent<HTMLButtonElement>) {
    opener.current = event.currentTarget;
    if (hasOpened) showChatPanel(panel.current);
    else setHasOpened(true);
  }

  return (
    <>
      <main className="mx-auto grid min-h-dvh max-w-5xl content-center gap-6 px-6 py-16 sm:px-10">
        <div className="max-w-xl border-t-2 border-primary pt-8">
          <h1 className="text-5xl font-semibold tracking-tight sm:text-7xl">Northwind Studio</h1>
          <p className="mt-5 max-w-md text-lg text-muted-foreground">
            An independent design studio. Schedule a consultation to talk through your project.
          </p>
          <button type="button" aria-haspopup="dialog" onClick={openChat} className={`${PRIMARY_BUTTON} mt-6 inline-flex items-center gap-2`}>
            Schedule a consultation
            <ArrowUpRight className="size-4" aria-hidden="true" />
          </button>
        </div>
      </main>
      <button type="button" aria-label="Open scheduling chat" aria-haspopup="dialog" onClick={openChat}
        className="fixed right-4 bottom-4 grid size-14 cursor-pointer place-items-center rounded-full bg-primary text-primary-foreground shadow-lg ring-1 ring-primary/15 hover:bg-primary/90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring sm:right-6 sm:bottom-6">
        <MessageCircle className="size-6" strokeWidth={2} aria-hidden="true" />
      </button>
      <dialog ref={panel} aria-labelledby={titleId} onClose={() => opener.current?.focus()}
        className="fixed inset-0 m-0 h-dvh max-h-none w-full max-w-none overflow-hidden bg-background text-foreground backdrop:bg-black/30 min-[641px]:inset-auto min-[641px]:right-6 min-[641px]:bottom-6 min-[641px]:h-[min(640px,calc(100dvh-3rem))] min-[641px]:w-[400px] min-[641px]:rounded-2xl min-[641px]:border min-[641px]:border-border min-[641px]:shadow-2xl">
        <h2 id={titleId} className="sr-only">Scheduling chat</h2>
        {hasOpened && <ChatWorkspace embedded onClose={() => panel.current?.close()} />}
      </dialog>
    </>
  );
}
