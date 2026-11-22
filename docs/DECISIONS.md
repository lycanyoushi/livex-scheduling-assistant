# Project decisions

## Assignment requirements

The confirmed LiveX brief requires an interactive Python chatbot that uses Cal.com’s REST API to book, view, cancel, and reschedule meetings through conversation. The web interface is a bonus. The submission includes repository access, a setup and run README, and a short recording within five days of receipt. The repository will be made public for submission after approval.

## Python backend and Next.js frontend

**Approved November 22, 2026.** Next.js, TypeScript, Tailwind, and Vitest remain the frontend stack. Flask and Google’s `google-genai` Python SDK replace the planned TypeScript route handlers and `@google/genai` backend adapter so the core application satisfies the language requirement. Python owns Cal.com requests, chat orchestration, validation, and booking writes. The frontend keeps the planned request and NDJSON event shape. The Cal.com and Gemini probes run from Python, and backend unit tests use `unittest`.

The approved `eslint` and `eslint-config-next` packages provide the linting required by M0. The approved runtime and build companions are `react-dom`, `@types/node`, `@types/react`, `@types/react-dom`, `@tailwindcss/postcss`, and `postcss`. The Node types target the supported Node 22 line.

## Frontend toolchain compatibility

**Approved November 22, 2026.** TypeScript 7.0.2 passed the standalone type check, but Next.js’s ESLint configuration failed to load because its TypeScript parser supports releases below 6.1. Several Next.js lint plugins also cap their supported ESLint peers at version 9. The scaffold uses TypeScript 6.0.3 and ESLint 9.39.5, the newest stable versions in those supported ranges at setup time.
