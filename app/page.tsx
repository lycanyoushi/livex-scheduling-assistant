export default function Home() {
  return (
    <main className="flex min-h-dvh flex-col">
      <header className="border-b border-border bg-card">
        <div className="mx-auto flex max-w-4xl items-center gap-3 px-4 py-4 sm:px-8">
          <span className="calendar-mark" aria-hidden="true" />
          <p className="text-base font-semibold tracking-tight">
            Scheduling Assistant
          </p>
        </div>
      </header>

      <section
        aria-labelledby="chat-title"
        className="mx-auto flex w-full max-w-3xl flex-1 items-center justify-center px-4 py-12 text-center sm:px-8"
      >
        <div className="max-w-sm">
          <h1 id="chat-title" className="text-3xl font-semibold tracking-tight">
            Your scheduling chat
          </h1>
          <p id="chat-status" className="mt-3 text-muted-foreground">
            The assistant is being set up.
          </p>
        </div>
      </section>

      <section
        aria-label="Message composer"
        className="mx-auto w-full max-w-3xl px-4 pb-4 sm:px-8 sm:pb-8"
      >
        <div className="flex items-end gap-2 rounded-xl border border-border bg-card p-2 shadow-xs">
          <label htmlFor="chat-message" className="sr-only">
            Message
          </label>
          <textarea
            id="chat-message"
            aria-describedby="chat-status"
            className="min-h-11 min-w-0 flex-1 resize-none rounded-md px-3 py-2.5 text-base placeholder:text-muted-foreground"
            placeholder="Type a message"
            rows={1}
            disabled
          />
          <button
            type="button"
            disabled
            className="min-h-11 rounded-md bg-primary/15 px-4 text-sm font-semibold text-primary"
          >
            Send
          </button>
        </div>
      </section>
    </main>
  );
}
