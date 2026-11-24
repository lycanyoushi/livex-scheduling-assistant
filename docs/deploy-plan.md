# Vercel deployment plan

`vercel.json` defines a Next.js service for `/` and `/widget-demo`, with `/api/*` routed to Flask's `wsgi:app` on the same origin. The rewrite preserves Flask's request path. Both services use the current repository root and explicitly select their framework. Python is pinned to 3.12, and the Flask function has a 120-second duration limit. The development loopback rewrite remains limited to `next dev`. [Vercel Services configuration](https://vercel.com/docs/services/config-reference) · [Vercel service routing](https://vercel.com/docs/services/routing) · [Python streaming](https://vercel.com/docs/functions/runtimes/python#streaming-python-functions)

Local checks passed against Vercel's published JSON schema. The route check covered `/`, `/widget-demo`, a static asset, `/api/warm`, and `/api/chat`. A local WSGI smoke in fake mode served warm metadata and streamed status before `turn_end`. Both requests logged the same boot identifier.

## After approval

1. Confirm the GitHub `origin` and the intended Vercel project. Push the reviewed commits and link an archive of the reviewed revision to Vercel. Deploy with `--environment preview` or `--environment production` to select the target explicitly. Keep Fluid compute enabled for the configured Flask duration. [Vercel Services overview](https://vercel.com/docs/services) · [Function duration limits](https://vercel.com/docs/functions/limitations#max-duration)
2. Set Preview to `DEMO_MODE=fake`. Supply `GEMINI_API_KEY` from the unbilled project, a separate random `STATE_SIGNING_SECRET`, and a daily call cap below the project's quota. For the live Cal.com demonstration, configure the isolated test account's `CAL_API_KEY`, allowlist, and default 30-minute event type in the intended environment. Keep all secrets in Vercel's environment settings. [Vercel environment variables](https://vercel.com/docs/environment-variables)
3. Open `/` and `/widget-demo` on the preview URL. Check `/api/warm` and `/api/chat` both hit Flask. Verify that the chat sends `application/x-ndjson`, `Cache-Control: no-store`, and a status line before `turn_end`. Use Stop during a streamed reply and compare the browser abort with the Flask runtime log. Repeat booking, listing, moving, and cancelling in fake mode.
4. Capture the Python cold start from the first `flask_start` log and the first public `/api/warm` request. Compare the next chat's timing and check that both `/api/warm` and `/api/chat` log the same `boot` identifier before claiming that opening chat warmed the Flask instance. Repeat on a fresh instance and record the observed times.
5. With the live environment pointed at the isolated Cal.com test event, verify a complete test booking cycle and clean up the synthetic bookings. Use the approved test inbox aliases for real invites and keep their addresses out of recordings and logs. Add the verified public URL and recording link to the README.

## Preview verification

Vercel built both services from the shared root. Both pages loaded in Chromium, and `/api/warm` and `/api/chat` reached Flask. Chat responded with `application/x-ndjson` and `Cache-Control: no-store`. Status arrived at 100 ms, opening chips at 112 ms, and the first text at 1.12 s, before `turn_end` at 1.66 s. The browser booked, listed, rescheduled to a different time, and cancelled in fake mode. The final list returned zero active bookings.

Stop closed the browser stream in **25–27 ms** across two checks. Flask logged that both Gemini calls finished in about 1.9–2.1 s. The next browser turn completed normally. Calendar changes still require Confirm. [Vercel request cancellation](https://vercel.com/docs/functions/functions-api-reference#cancel-requests)

| Fresh Flask instance | First `/api/warm` round trip | Flask setup |
| --- | ---: | ---: |
| 1 | 3.88 s | 2.11 s |
| 2 | 3.39 s | 1.95 s |
| 3 | 2.74 s | 1.56 s |

In the browser run, the widget's `/api/warm` and the subsequent `/api/chat` logged the same Flask boot identifier.

## Production verification

The CLI automatically assigned the first deployment to Production. It had no Gemini or Cal.com credentials and was removed before the verified release. The release deployed revision `30e1590` with `DEMO_MODE` unset and only the isolated 30-minute Cal.com test event allowed. Both public pages returned HTTP 200. `/api/warm` exposed only the verified test event, and `/api/chat` returned NDJSON with `Cache-Control: no-store`. [Live web chat](https://livex-scheduling-assistant.vercel.app) · [Live widget demo](https://livex-scheduling-assistant.vercel.app/widget-demo)

| Flow | Independent Cal.com check |
| --- | --- |
| Book | Confirm created one active booking for the controlled test alias at the selected time. |
| List | The app showed the exact active booking returned by a fresh Cal.com list. The turn used one Gemini call and made zero writes. |
| Reschedule | Confirm cancelled the original and created an active replacement at a distinct time. |
| Cancel | Confirm cancelled the replacement. Both controlled test aliases had zero active upcoming bookings after the cycle. |

Each Confirm made one calendar write and used zero Gemini calls. The proposals made zero calendar writes.

Two Production Flask startups logged **2.04 s** and **2.16 s** of setup. The cycle's public `/api/warm` took **156 ms** and reused an already initialized instance. The warm request and all nine chat requests in the cycle logged the same Flask boot identifier.

## Recording outline

Show a text request, early slots, and explicit Confirm. Verify the synthetic booking in Cal.com. List it, move it to a different time, and cancel it. Show the taken-slot alternatives and one diagnostics view with zero model calls on a UI action. Finish with a numbered choice in the Python terminal and the mobile widget. Aim for two to three minutes.
