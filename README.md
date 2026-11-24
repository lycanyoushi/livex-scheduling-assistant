# LiveX Scheduling Assistant

A Python scheduling chatbot backed by Cal.com and Gemini, with a Next.js web interface.

## Local development

Use Python 3.12, Node.js 22, and pnpm. Create a virtual environment with `python -m venv .venv`.
Activate it with `.venv\Scripts\Activate.ps1` on Windows or `source .venv/bin/activate` on macOS or Linux.

```sh
python -m pip install -r requirements.txt
pnpm install --frozen-lockfile
```

Copy `.env.example` to `.env.local`. Set the Cal.com API key for an isolated test account, a Gemini API key from an unbilled project, and a signing secret generated with `python -c "import secrets; print(secrets.token_hex(32))"`. The test account needs availability and a visible 30-minute event type.

Run `pnpm dev:api` and `pnpm dev` in separate terminals, then open `http://localhost:3000`.

Set `DAILY_MODEL_CALL_CAP` below the daily free-tier request limit shown for your AI Studio project. `RATE_LIMIT_TURNS_PER_10MIN` sets the per-IP chat limit. A stalled primary model can switch to the free-tier 3.1 Flash-Lite backup. The web app offers a direct Cal.com booking option when available.

For terminal chat, run `pnpm chat` or `python -m backend.cli` from the activated virtual environment. Type a displayed option number to pick a time and answer `y` or `n` at the confirmation prompt. Use `r` or `c` to move or cancel your latest confirmed booking. `/new` starts a fresh chat, `/tz America/Los_Angeles` changes the time zone, `/debug` shows the last turn's metrics, and `/quit` exits.

Run the checks from the repository root.

```sh
pnpm test
pnpm lint
```

## Known limitations

- The browser-held session token is readable and includes booking IDs and the user's email. HMAC verifies the token's integrity. Use an isolated test account.
- Anyone who knows an attendee's email can look up or cancel that attendee's bookings in the demo.
- A production version would verify the attendee's email with a one-time code before showing or changing bookings.
- Per-IP limits, the daily model-call budget, and duplicate-confirm protection are held in each process's memory. They reset when the process restarts and are independent across instances.
- When Cal.com times out on a confirmed change, the app checks the booking records and reports what it can verify. If the records are inconclusive, check the calendar before submitting another change.
- Google can use free-tier Gemini prompts and responses to improve its products. Use fictional contact details in conversation and keep deliverable test aliases in the local Cal.com probe settings.
