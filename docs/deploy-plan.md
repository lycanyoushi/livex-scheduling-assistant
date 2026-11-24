# Vercel deployment plan

`vercel.json` defines a Next.js service for `/` and `/widget-demo`, with `/api/*` routed to Flask's `wsgi:app` on the same origin. The rewrite preserves Flask's request path. Both services use the current repository root and explicitly select their framework. Python is pinned to 3.12, and the Flask function has a 120-second duration limit. The development loopback rewrite remains limited to `next dev`. [Vercel Services configuration](https://vercel.com/docs/services/config-reference) · [Vercel service routing](https://vercel.com/docs/services/routing) · [Python streaming](https://vercel.com/docs/functions/runtimes/python#streaming-python-functions)

Local checks passed against Vercel's published JSON schema. The route check covered `/`, `/widget-demo`, a static asset, `/api/warm`, and `/api/chat`. A local WSGI smoke in fake mode served warm metadata and streamed status before `turn_end`. Both requests logged the same boot identifier. Hosted service routing, cancellation, and cold-start timing still need preview verification.

## After approval

1. Confirm the GitHub `origin` and the intended Vercel project, then push the reviewed commits and connect the project using the Services preset. Keep Fluid compute enabled for the configured Flask duration. [Vercel Services overview](https://vercel.com/docs/services) · [Function duration limits](https://vercel.com/docs/functions/limitations#max-duration)
2. Set Preview to `DEMO_MODE=fake`. Supply `GEMINI_API_KEY` from the unbilled project, a separate random `STATE_SIGNING_SECRET`, and a daily call cap below the project's quota. For the live Cal.com demonstration, configure the isolated test account's `CAL_API_KEY`, allowlist, and default 30-minute event type in the intended environment. Keep all secrets in Vercel's environment settings. [Vercel environment variables](https://vercel.com/docs/environment-variables)
3. Open `/` and `/widget-demo` on the preview URL. Check `/api/warm` and `/api/chat` both hit Flask. Verify that the chat sends `application/x-ndjson`, `Cache-Control: no-store`, and a status line before `turn_end`. Use Stop during a streamed reply and confirm in the Python logs that the turn ends. Repeat booking, listing, moving, and cancelling in fake mode.
4. Capture the Python cold start from the first `flask_start` log and the first public `/api/warm` request. Compare the next chat's timing and check that both `/api/warm` and `/api/chat` log the same `boot` identifier before claiming that opening chat warmed the Flask instance. Repeat on a fresh instance and record the observed times.
5. With the live environment pointed at the isolated Cal.com test event, verify a complete test booking cycle and clean up the synthetic bookings. Use the approved test inbox aliases for real invites and keep their addresses out of recordings and logs. Add the verified public URL and recording link to the README.

## Recording outline

Show a text request, early slots, and explicit Confirm. Verify the synthetic booking in Cal.com. List it, move it to a different time, and cancel it. Show the taken-slot alternatives and one diagnostics view with zero model calls on a UI action. Finish with a numbered choice in the Python terminal and the mobile widget. Aim for two to three minutes.
