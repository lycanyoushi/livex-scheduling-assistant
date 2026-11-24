# Project decisions

## Assignment requirements

The confirmed LiveX brief requires an interactive Python chatbot that uses Cal.com’s REST API to book, view, cancel, and reschedule meetings through conversation. The web interface is a bonus. The submission includes repository access, a setup and run README, and a short recording within five days of receipt. The repository will be made public for submission after approval.

## Python backend and Next.js frontend

**Approved November 22, 2026.** Next.js, TypeScript, Tailwind, and Vitest remain the frontend stack. Flask and Google’s `google-genai` Python SDK replace the planned TypeScript route handlers and `@google/genai` backend adapter so the core application satisfies the language requirement. Python owns Cal.com requests, chat orchestration, validation, and booking writes. The frontend keeps the planned request and NDJSON event shape. The Cal.com and Gemini probes run from Python, and backend unit tests use `unittest`.

The approved `eslint` and `eslint-config-next` packages provide the linting required by M0. The approved runtime and build companions are `react-dom`, `@types/node`, `@types/react`, `@types/react-dom`, `@tailwindcss/postcss`, and `postcss`. The Node types target the supported Node 22 line.

## Frontend toolchain compatibility

**Approved November 22, 2026.** TypeScript 7.0.2 passed the standalone type check, but Next.js’s ESLint configuration failed to load because its TypeScript parser supports releases below 6.1. Several Next.js lint plugins also cap their supported ESLint peers at version 9. The scaffold uses TypeScript 6.0.3 and ESLint 9.39.5, the newest stable versions in those supported ranges at setup time.

## Gemini default for the initial milestones

**Approved November 22, 2026.** `gemini-3.5-flash-lite` with `minimal` thinking is the application and probe default. On the unbilled test project, it passed all eight live Interactions checks. The planned `gemini-3.8-flash` model returned HTTP 503, a stream error, or a timeout across several attempts, and its streaming `generateContent` diagnostic returned HTTP 503. Both models remain in the M7 comparison. See [Gemini probe notes](gemini-notes.md).

## Live Cal.com writes use controlled test aliases

**Approved November 22, 2026.** Cal.com rejected the reserved email domains in the initial probe. The write check uses two distinct aliases on a controlled test inbox and requires `--write-test`. Cleanup verifies attendee, event type, and time before canceling a probe booking. The addresses stay in `.env.local`. See [Cal.com probe notes](calcom-notes.md).

## Python mapping

**Approved for M2 on November 22, 2026.** The browser and terminal use one Python turn handler. Flask adapts its events to HTTP, and `python -m backend.cli` will consume the same events in-process. Both send the same typed actions for slot selection, confirmation, and dismissal. Frontend TypeScript keeps the existing JSON field names and event protocol.

### Date parsing and time zones

Use `dateparser` restricted to English and Spanish, with an injected reference time in the user's zone. Prescan resolves weekdays, relative days, ordinal days, and week ranges before passing other date spans to the parser. Preserve explicit past dates and leave date-only results without a time. A bare weekday naming today means today when the requested time is still ahead or omitted. If the requested time has already arrived or passed, use the same weekday next week. Test all three cases. Show the resolved date explicitly in replies and slot chips. Warm prescan before measuring its 5 ms target and report the measured duration if it exceeds that target. [dateparser settings](https://dateparser.readthedocs.io/en/latest/settings.html)

Use standard-library `datetime` and `zoneinfo.ZoneInfo` for IANA names, local day boundaries, and formatting. Convert to UTC at the Cal.com boundary. Validate zone names and calendar dates, and use the original UTC offset when displaying a real slot during a repeated DST hour. Declare `tzdata` directly for hosts without an IANA database. The current Windows Python environment could resolve neither test zone with the standard library alone. [Python zoneinfo](https://docs.python.org/3.12/library/zoneinfo.html)

Test each phrase with an injected **Monday, November 23, 2026 at 10:00 a.m. local** clock in both zones. For this reference, `next Friday` means the Friday of the following week. Date-only results must carry their local date without an implied appointment time.

| Phrase | Asia/Singapore | America/New_York |
| --- | --- | --- |
| `Thursday afternoon` | Nov 26, 12:00–17:00 local, 04:00–09:00 UTC | Nov 26, 12:00–17:00 local, 17:00–22:00 UTC |
| `next Friday` | Dec 4, date only | Dec 4, date only |
| `tomorrow 9am` | Nov 24, 09:00 local, 01:00 UTC | Nov 24, 09:00 local, 14:00 UTC |
| `the 3rd` | Dec 3, date only | Dec 3, date only |

Use half-open part-of-day windows. Add a shared-instant test at `2026-11-23T02:00:00Z`: `tomorrow 9am` means Nov 24 in Singapore and Nov 23 in New York. Cover `next week`, intent and email hints, and New York DST gap and overlap cases in `unittest`. [Chrono weekday semantics](https://github.com/wanasit/chrono/blob/master/src/calculation/weekdays.ts)

### Validation and signed state

Use Pydantic v2 in strict mode for request bodies, action variants, decoded state, and model tool arguments. The UI action discriminator and known tool name select their respective schemas. Reject unknown fields and wrong types, enforce the 2,000-character message cap, validate time zones and date bounds, and check referenced slots or bookings against verified state before a tool runs. Expose only Gemini's supported object, string, and enum schema subset. The existing Cal.com response validators stay in the client. [Pydantic strict mode](https://pydantic.dev/docs/validation/latest/concepts/strict_mode/)

Serialize compact UTF-8 JSON and use the existing `base64url(payload).base64url(signature)` shape. Sign the encoded payload with standard-library `hmac` and SHA-256 using `STATE_SIGNING_SECRET`. Verify the signature with `hmac.compare_digest` before decoding or validating state. Keep the version, reference caps, and monotonic counters, and check a signed expiry. The session token is readable in the browser and contains booking IDs and the user's email. Treat it as sensitive and list that limitation in the README. The browser and model pass short refs to actions. Booking UIDs and start times are resolved from verified state, and Cal.com writes run only after confirmation.

`find_slots` requires `for_booking_ref`. An empty string requests ordinary availability. For a reschedule, validate the reference, the booking's status and start, and its membership for the session's attendee email before passing the real UID as `bookingUidToReschedule`. This lets a text request fetch the right slots without a prior UI action. Associate the returned refs with that booking so a proposal can validate the pairing.

### Streaming and development

Validate a request before opening the stream. Flask returns a generator-backed `Response` with `Content-Type: application/x-ndjson; charset=utf-8` and `Cache-Control: no-store`. Serialize each event as one UTF-8 JSON line and yield it as it arrives. A single async turn task keeps the SDK stream and its deadline in one task. A bounded queue lets the synchronous Flask iterator yield events and signal cancellation on close. Test a client disconnect with an in-flight model stream, verify cancellation, and assert that its thread and task have exited. The terminal consumes the same async events directly. On a failed turn, preserve the last good interaction ID and pending call in `turn_end` as specified by the protocol. [Flask streaming](https://flask.palletsprojects.com/en/stable/patterns/streaming/)

Run Next.js on `localhost:3000` and Flask on `127.0.0.1:5000` during development. A development-only Next.js rewrite forwards `/api/chat` and `/api/warm` to Flask. The browser uses relative same-origin URLs, and the Python process reads the private settings. Test that the first NDJSON event reaches the Next.js client before the final line. [Next.js rewrites](https://nextjs.org/docs/app/api-reference/config/next-config-js/rewrites)

### Hosting the live link

Use one Vercel Services project with two service entries from this repository. Route `/api/:path*` to the Flask Python 3.12 service and other paths to the Next.js service. Vercel preserves the original API path and supports Flask generator responses on the Python runtime. Use a Python function duration that accommodates the bounded turn, and supply API keys and the signing secret to Flask at runtime. The local loopback rewrite applies only to development. After deployment approval in M9, verify streaming, response headers, and cancellation on the public URL. Measure the Python cold start and check whether opening chat and calling `/api/warm` reaches Flask and warms the function for the next turn. [Vercel service routing](https://vercel.com/docs/services/routing) · [Flask on Vercel](https://vercel.com/docs/frameworks/backend/flask#stream-responses) · [Vercel's Next.js and Flask example](https://github.com/vercel/examples/tree/main/websockets/flask-ai-chat)

Pin the approved direct dependencies `dateparser`, `tzdata`, and `pydantic` v2 in `requirements.txt`. The browser keeps Vitest, and the Python tests use `unittest`.

## Eval acceptance and fallback

**Approved November 22, 2026.** The fake calendar removes the intervals of active bookings from availability. During rescheduling, it excludes the one booking named by `bookingUidToReschedule` from that conflict check. Cases 3 and 20 specify explicit future times. Cases 16 and 18 fail on a proposal event, a Cal.com mutation, or a missing refusal. Report model proposal attempts that the application rejected as soft failures. Wording checks are reserved for outcomes without a structural signal, and report wording-only failures separately.

Before using backup generation with a pending tool call, probe both continuation shapes against the same stored primary interaction. If either shape cannot continue, use the backup only on requests without a `function_result` step and use the code-only answer otherwise. Report HTTP 429 counts per model during the evals.

## M4 review changes

An unqualified hour from 1 through 7 is interpreted as PM. Hours 8 through 12 and explicit clock formats retain their written value. Prescan applies the same-day weekday rule after resolving the hour.

The model input replaces email addresses, including fixture addresses, with a placeholder. The signed session retains the user's address, and case 3 checks the resolved proposal rather than a model argument. Runtime environment variables take precedence over `.env.local` defaults.

The browser disables slots emitted by failed text turns. If a confirmation loses a slot, the server returns fresh signed refs for up to three nearest openings and the browser leaves those alternatives selectable.

In M5, measure Gemini client and connection setup as part of time to first text. Reuse a long-lived loop and client if setup adds more than about 150 ms. In M6, report only the unexpected exception type when mapping a Gemini failure to `model_down`.

## M5 model and client selection

Keep `gemini-3.5-flash-lite` with `minimal` thinking, `store=True`, and the full scheduling tool list. In the stored, full-tool comparison with a new client per call, it completed 9 of 9 and reached first text in a median 2.53 s. 3.1 Flash-Lite reached first text in 2.78 s with a better successful-sample p90 and one HTTP 503. 3.8 Flash timed out twice establishing stored history. The candidate-switch eval subset applies when changing the default. [Latency probe results](gemini-notes.md#m5-latency-probe-on-november-22-2026)

Run Flask turns on one process-owned event loop and reuse one SDK client. Setup cost was 320 ms at the median with a new SDK client. Close each stream separately, cancel a disconnected request's task, and close the client on process shutdown. The persistent probe removed repeat construction cost. Median first-text time remained near 2.5 s and the tail varied across configurations.

## M6 timed-out calendar writes

Reconcile each timed-out write with Cal.com before reporting its outcome. For create, look up the attendee's booking at the requested start. For cancel, re-read the original booking status. For reschedule, re-read the original booking and look for a distinct replacement at the requested start. Use the verified result to tell the user what happened.

If the readback shows no change, keep the confirmation available for a manual retry. If the result is partial or inconclusive, retire the proposal and tell the user which part could be verified. Confirmation replays with the same signed request share the recorded outcome within one process. The booking search uses at most three pages of 100 attendee records, with incomplete searches treated as inconclusive.

## M6 model and browser fallback

Make one attempt on the backup after a primary 503, timeout, or quota failure before any text streams. The verified 3.5 and 3.1 Flash-Lite pair can continue stored interactions in either direction. A different model pair can retry a fresh interaction without tool results. If the backup cannot run or fails too, use signed availability or booking data already collected. Keep the last working interaction ID and pending call for Retry. Offer the Cal.com booker when neither model can answer and the app has no usable scheduling options. [Gemini verification](gemini-notes.md#m6-live-backup-checks)

Load Cal.com's own embed script on demand using the public booking URL. This provides the approved booker without another package. Limit chat requests per IP over ten minutes and count primary and backup calls against a single daily budget that resets at midnight Pacific time. These counters and confirmation replay records are scoped to one process. [Official Cal.com embed](https://site.cal.com/embed)

## M5b latency changes

Measure before and after with the committed ten-turn driver, live Gemini, and identical read-only fixture availability through Next.js. First useful content records the first visible chips or text. A closed window that Cal checked successfully is distinct from a timed-out or missing read.

Show up to four matching new-booking openings from prefetch before the model responds. Use explicit dates in the chips, leave existing-booking changes in their current flow, and count app chips as offers in case 2. Cap Gemini generation at 300 output tokens.

Retain the interaction ID while sending the 14-day calendar only when the model first receives it or the local date or time zone changes. Track slot and booking details already sent so unchanged details stay in the stored history. Reuse identical prefetched slot refs where they remain in signed state.

The size probe measured a 2.49 s median first-text difference between the shortest and longest stored parents. Start a new stored interaction before a text turn when the last successful model call used more than 6,000 input tokens. Use a recap of the known name, email presence, the session attendee's active bookings in app state, and any open proposal. The next successful turn drops the old pending provider call and preserves the signed proposal. [Stored-history probe](gemini-notes.md#m5b-stored-history-size-probe)

## Closed-window alternatives

A completed new-booking lookup that misses the requested window or exact start shows up to three real alternatives from the 14-day availability window. The ranking prefers the same part of day on nearby dates, then other times on the requested day. Stale proposals use the same ranking on freshly checked availability. The alternative chips precede a single tool-free explanation on the normal response path.

## M7 review changes

Raise the output cap from 300 to 800 tokens. Treat `incomplete` with streamed text as a usable reply and label it as cut short. Continue from capped text-only replies on 3.5 and 3.1 Flash-Lite, where the live probe verified the interaction ID and retained history. Keep the prior model history for other capped replies. Record `model_down` reasons without provider message content. [Output-cap probe](gemini-notes.md#m7-review-output-cap-probe)

Booking cards, including an explicit empty list, are displayed by a `booking_cards` event. Ref inventory is lookup data. Booking-card order counts toward evaluation offers. Named refs missing from the attendee's active signed bookings receive the same diagnostic context whether the underlying record is missing, inactive, or owned by another attendee.

Warm requests share the configured per-IP quota and window with chat but have their own bucket. A timed-out calendar write waits two seconds before reconciliation. A taken create slot checks for an active booking that already matches this attendee, event, and exact interval before looking up alternatives. Move success still requires verifying what happened to the original booking. In the terminal, blank input or `/cancel` at either contact prompt dismisses the proposal.

M7 checks explicit card offers in observed order. Tool traces count named attempts separately from calls whose arguments resolve to the required fixture booking or slot. The approved YAML remains the source of truth. The runner reads a generated JSON snapshot checked against the YAML hash so `pnpm eval` uses the existing project dependencies.
