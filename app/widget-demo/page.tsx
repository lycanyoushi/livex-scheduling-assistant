"use client";

import { type MouseEvent, useId, useRef, useState } from "react";

import { OUTLINE_BUTTON, PRIMARY_BUTTON } from "../chat-cards";
import ChatWorkspace from "../chat-workspace";

export default function WidgetDemo() {
  const panel = useRef<HTMLDialogElement>(null);
  const opener = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const [hasOpened, setHasOpened] = useState(false);

  function openChat(event: MouseEvent<HTMLButtonElement>) {
    opener.current = event.currentTarget;
    setHasOpened(true);
    panel.current?.showModal();
  }

  return (
    <>
      <main className="mx-auto grid min-h-dvh max-w-5xl content-center gap-6 px-6 py-16 sm:px-10">
        <div className="max-w-xl border-t-2 border-primary pt-8">
          <h1 className="text-5xl font-semibold tracking-tight sm:text-7xl">Northwind Studio</h1>
          <p className="mt-5 max-w-md text-lg text-muted-foreground">
            An independent design studio. Schedule a consultation to talk through your project.
          </p>
          <button type="button" aria-haspopup="dialog" onClick={openChat} className={`${PRIMARY_BUTTON} mt-6`}>
            Schedule a consultation
          </button>
        </div>
      </main>
      <button type="button" aria-label="Open scheduling chat" aria-haspopup="dialog" onClick={openChat}
        className="fixed right-4 bottom-4 grid size-14 place-items-center rounded-full bg-primary text-sm font-semibold text-primary-foreground shadow-lg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring sm:right-6 sm:bottom-6">
        Chat
      </button>
      <dialog ref={panel} aria-labelledby={titleId} onClose={() => opener.current?.focus()}
        className="fixed inset-0 m-0 h-dvh max-h-none w-full max-w-none overflow-hidden bg-background text-foreground backdrop:bg-black/30 min-[641px]:inset-auto min-[641px]:right-6 min-[641px]:bottom-6 min-[641px]:h-[min(640px,calc(100dvh-3rem))] min-[641px]:w-[400px] min-[641px]:rounded-2xl min-[641px]:border min-[641px]:border-border min-[641px]:shadow-2xl">
        <div className="flex h-full min-h-0 flex-col">
          <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border bg-card px-4 py-1">
            <h2 id={titleId} className="text-sm font-semibold">Scheduling chat</h2>
            <button type="button" className={`${OUTLINE_BUTTON} px-2.5`} onClick={() => panel.current?.close()}>Close</button>
          </div>
          <div className="min-h-0 flex-1 overflow-hidden">
            {hasOpened && <ChatWorkspace embedded />}
          </div>
        </div>
      </dialog>
    </>
  );
}
