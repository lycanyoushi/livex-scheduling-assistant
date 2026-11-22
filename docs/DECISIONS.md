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
