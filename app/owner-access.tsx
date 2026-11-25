"use client";

import { type FormEvent, useCallback, useEffect, useId, useRef, useState } from "react";

import { OUTLINE_BUTTON, PRIMARY_BUTTON } from "./chat-cards";
import { AssistantAvatar } from "./chat-header";
import { clearOwnerHistory, ownerHistoryKey } from "./chat-history";
import ChatWorkspace from "./chat-workspace";

type Access = "checking" | "locked" | "owner" | "demo" | "unavailable" | "signing-out" | "signout-failed";

async function readAccess(response: Response): Promise<"locked" | "owner" | "demo"> {
  if (!response.ok) throw new Error("Could not verify access.");
  const state = await response.json();
  if (typeof state?.authenticated !== "boolean" || typeof state?.required !== "boolean") {
    throw new Error("Invalid access status.");
  }
  if (!state.authenticated) return "locked";
  return state.required ? "owner" : "demo";
}

export default function OwnerAccess() {
  const [access, setAccess] = useState<Access>("checking");
  const [signingIn, setSigningIn] = useState(false);
  const [error, setError] = useState("");
  const codeId = useId();
  const errorId = useId();
  const checker = useRef<AbortController | null>(null);
  const action = useRef<AbortController | null>(null);
  const signingOut = useRef(false);
  const changes = useRef<BroadcastChannel | null>(null);

  const expireAccess = useCallback(() => {
    checker.current?.abort();
    clearOwnerHistory();
    setError("");
    setAccess("locked");
  }, []);

  const refreshAccess = useCallback(async () => {
    if (action.current || signingOut.current) return;
    checker.current?.abort();
    const controller = new AbortController();
    checker.current = controller;
    try {
      const response = await fetch("/api/auth", { cache: "no-store", signal: controller.signal });
      const verified = await readAccess(response);
      if (controller.signal.aborted) return;
      if (verified === "locked") expireAccess();
      else setAccess(verified);
    } catch {
      if (!controller.signal.aborted) setAccess("unavailable");
    }
  }, [expireAccess]);

  useEffect(() => {
    const initialCheck = window.setTimeout(() => void refreshAccess(), 0);
    function onVisibilityChange() {
      if (document.visibilityState === "visible") void refreshAccess();
    }
    function onPageShow(event: PageTransitionEvent) {
      if (event.persisted) void refreshAccess();
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("pageshow", onPageShow);
    if (typeof BroadcastChannel !== "undefined") {
      const channel = new BroadcastChannel("livex-owner-access");
      channel.onmessage = (event) => {
        if (event.data === "signed-out") expireAccess();
      };
      changes.current = channel;
    }
    return () => {
      window.clearTimeout(initialCheck);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("pageshow", onPageShow);
      checker.current?.abort();
      action.current?.abort();
      changes.current?.close();
    };
  }, [expireAccess, refreshAccess]);

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (action.current) return;
    const code = new FormData(event.currentTarget).get("code");
    if (typeof code !== "string" || !code) return;
    event.currentTarget.reset();
    checker.current?.abort();
    const controller = new AbortController();
    action.current = controller;
    setSigningIn(true);
    setError("");
    try {
      const response = await fetch("/api/auth", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }), signal: controller.signal, cache: "no-store",
      });
      if (controller.signal.aborted) return;
      if (response.status === 401 || response.status === 429) {
        setError(response.status === 401 ? "Invalid access code." : "Too many access attempts. Try again later.");
        return;
      }
      const verified = await readAccess(response);
      if (!controller.signal.aborted) setAccess(verified);
    } catch {
      if (!controller.signal.aborted) setError("Couldn’t sign in. Try again.");
    } finally {
      action.current = null;
      if (!controller.signal.aborted) setSigningIn(false);
    }
  }

  async function signOut() {
    if (action.current) return;
    checker.current?.abort();
    signingOut.current = true;
    const controller = new AbortController();
    action.current = controller;
    setAccess("signing-out");
    clearOwnerHistory();
    try {
      const response = await fetch("/api/auth", { method: "DELETE", signal: controller.signal, cache: "no-store" });
      const verified = await readAccess(response);
      if (controller.signal.aborted) return;
      if (verified === "owner") throw new Error("Access is still active.");
      signingOut.current = false;
      expireAccess();
      changes.current?.postMessage("signed-out");
    } catch {
      if (!controller.signal.aborted) setAccess("signout-failed");
    } finally {
      action.current = null;
    }
  }

  if (access === "owner" || access === "demo") {
    return <ChatWorkspace key={access} historyKey={ownerHistoryKey(access === "owner")}
      onAccessExpired={expireAccess} onSignOut={access === "owner" ? signOut : undefined} />;
  }

  return (
    <main className="grid min-h-dvh min-w-0 place-items-center overflow-y-auto bg-background p-4">
      <section className="grid w-full max-w-sm gap-4 rounded-2xl border border-border bg-card p-5 shadow-sm sm:p-7">
        <AssistantAvatar className="size-12" />
        <h1 className="text-xl font-semibold tracking-tight">Owner access</h1>
        {access === "locked" && (
          <form aria-label="Owner access" onSubmit={signIn} className="grid gap-3">
            <p className="text-sm text-muted-foreground">Enter your access code to open the calendar.</p>
            <div className="grid gap-1.5">
              <label htmlFor={codeId} className="text-sm font-medium">Access code</label>
              <input id={codeId} name="code" type="password" autoComplete="current-password" required maxLength={256}
                aria-invalid={Boolean(error)} aria-describedby={error ? errorId : undefined}
                className="min-h-11 w-full rounded-xl border border-border bg-background px-3 text-base focus-visible:outline-2 focus-visible:outline-ring" />
            </div>
            {error && <p id={errorId} role="alert" className="text-sm text-red-700 dark:text-red-300">{error}</p>}
            <button type="submit" className={PRIMARY_BUTTON} disabled={signingIn}>
              {signingIn ? "Signing in…" : "Sign in"}
            </button>
          </form>
        )}
        {access === "checking" && <p role="status" className="text-muted-foreground">Checking access…</p>}
        {access === "signing-out" && <p role="status" className="text-muted-foreground">Signing out…</p>}
        {access === "unavailable" && (
          <>
            <p role="alert" className="text-muted-foreground">Couldn’t check access.</p>
            <button type="button" className={OUTLINE_BUTTON} onClick={() => void refreshAccess()}>Try again</button>
          </>
        )}
        {access === "signout-failed" && (
          <>
            <p role="alert" className="text-muted-foreground">Couldn’t complete sign-out.</p>
            <button type="button" className={OUTLINE_BUTTON} onClick={() => void signOut()}>Retry sign out</button>
          </>
        )}
      </section>
    </main>
  );
}
