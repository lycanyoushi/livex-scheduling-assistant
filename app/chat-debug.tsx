import { useEffect, useId, useRef } from "react";
import { OUTLINE_BUTTON } from "./chat-cards";

type DebugModelCall = {
  modelRole?: string;
  status?: string;
  reason?: string;
  firstEventMs?: number;
  ttftMs?: number;
  totalMs?: number;
  inputTokens?: number;
  outputTokens?: number;
};

export type DebugSnapshot = {
  metrics: {
    outcome: string;
    path?: string;
    fallback?: string;
    totalMs?: number;
    modelCalls?: DebugModelCall[];
  } | null;
  events: object[];
  omittedEvents: number;
};

function milliseconds(value?: number): string {
  return typeof value === "number" && Number.isFinite(value) ? `${Math.round(value)} ms` : "—";
}

export function ChatDebug({ snapshot, onClose }: { snapshot: DebugSnapshot | null; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useId();
  useEffect(() => {
    const element = dialog.current;
    if (element && !element.open) element.showModal();
  }, []);

  const metrics = snapshot?.metrics;
  return (
    <dialog ref={dialog} aria-labelledby={heading} onClose={onClose}
      className="fixed inset-y-0 right-0 left-auto m-0 h-dvh max-h-none w-full max-w-lg overflow-y-auto border-l border-border bg-card p-5 text-foreground shadow-lg backdrop:bg-black/35">
      <header className="flex items-center justify-between gap-4">
        <h2 id={heading} className="text-lg font-semibold">Turn diagnostics</h2>
        <button type="button" className={`${OUTLINE_BUTTON} border border-border`} onClick={() => dialog.current?.close()}>
          Close diagnostics
        </button>
      </header>
      {!snapshot && <p className="mt-5 text-sm text-muted-foreground">Send a message or take an action to inspect its turn.</p>}
      {metrics && (
        <div className="mt-5 grid gap-3">
          <dl className="grid grid-cols-2 gap-3 rounded-lg border border-border p-3 text-sm">
            <div><dt className="text-muted-foreground">Outcome</dt><dd>{metrics.outcome}</dd></div>
            <div><dt className="text-muted-foreground">Path</dt><dd>{metrics.path === "ui_action" ? "UI action" : "Model"}</dd></div>
            <div><dt className="text-muted-foreground">Model calls</dt><dd>{metrics.modelCalls?.length ?? "—"}</dd></div>
            <div><dt className="text-muted-foreground">Turn time</dt><dd>{milliseconds(metrics.totalMs)}</dd></div>
            {metrics.fallback && <div><dt className="text-muted-foreground">Fallback</dt><dd>{metrics.fallback}</dd></div>}
          </dl>
          {(metrics.modelCalls ?? []).map((call, index) => (
            <section key={index} aria-label={`Model call ${index + 1}`} className="rounded-lg border border-border p-3 text-sm">
              <p className="font-semibold">{call.modelRole === "backup" ? "Backup" : "Primary"} · {call.status ?? "—"}</p>
              <p className="text-muted-foreground">First event {milliseconds(call.firstEventMs)} · First text {milliseconds(call.ttftMs)} · Total {milliseconds(call.totalMs)}</p>
              <p className="text-muted-foreground">Tokens {call.inputTokens ?? "—"} in / {call.outputTokens ?? "—"} out</p>
              {call.reason && <p className="wrap-anywhere text-muted-foreground">{call.reason}</p>}
            </section>
          ))}
        </div>
      )}
      {snapshot && (
        <details className="mt-6 rounded-lg border border-border p-3">
          <summary className="cursor-pointer text-sm font-semibold focus-visible:outline-2 focus-visible:outline-ring">Events from last turn</summary>
          {snapshot.omittedEvents > 0 && <p className="mt-2 text-sm text-muted-foreground">{snapshot.omittedEvents} additional events omitted.</p>}
          <pre className="mt-3 overflow-auto rounded bg-background p-2 text-xs">{JSON.stringify(snapshot.events, null, 2)}</pre>
        </details>
      )}
    </dialog>
  );
}
