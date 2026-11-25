# Vercel deployment plan

`vercel.json` routes the web app to Next.js and `/api/*` to Flask's `wsgi:app`. The browser uses the same origin for `/api/auth`, `/api/warm`, and `/api/chat`. Python is pinned to 3.12, and the Flask function has a 120-second duration limit. Local development uses Next.js loopback rewrites. [Vercel Services configuration](https://vercel.com/docs/services/config-reference) · [Vercel service routing](https://vercel.com/docs/services/routing) · [Python streaming](https://vercel.com/docs/functions/runtimes/python#streaming-python-functions)

## Final owner-mode Preview

Revision `bd731e7` deployed to Preview with `DEMO_MODE=fake` and `DEMO_TIME_ZONE=Asia/Singapore`. The authenticated Chromium check started at 09:20 Singapore time on November 25, 2026. [Owner Preview](https://livex-scheduling-assistant-rafkbwm8m-zenith-dbf8.vercel.app)

| Brief prompt | Hosted result |
| --- | --- |
| `book a 30-min intro with a candidate Thursday afternoon` | Four Thursday afternoon buttons appeared with a short reply asking for the guest's name and email. A typed time choice, fictional contact and `yes` created one fake booking at 12:30. |
| `what's on my calendar tomorrow?` | Two fictional meetings appeared for Thursday, November 26. The lookup returned zero write results. |
| `move my 3pm to later today` | The exact seeded 15:00 meeting was offered later times. A typed choice and `yes please` moved it to 15:30 on the same date. |

The browser restored the seeded meeting to 15:00, cancelled the synthetic 12:30 booking, and signed out to the access prompt. Each tested confirmation used zero model calls.

All 288 Python tests and 29 frontend tests passed, along with ESLint, TypeScript and the Next.js build. The last review suite passed 93 of 93 main runs and all six held-out cases. [Final evaluation report](m7-eval-report.md#owner-mode-final-review)

## Initial owner-mode Preview

Revision `fbfcd08` is ready on Vercel Preview with `DEMO_MODE=fake` and the owner access code enabled. Production promotion requires separate approval. [Owner Preview](https://livex-scheduling-assistant-4o6me8vul-zenith-dbf8.vercel.app)

The root and favicon returned HTTP 200. The retired widget route returned 404. Direct warm and chat requests returned 401 before sign-in, and the warm reply omitted host metadata. An invalid code returned 401. Signing in from the Preview origin set a Secure, HttpOnly, SameSite=Strict cookie. The authenticated warm request took 150 ms and returned fake-calendar metadata. Chat streamed tomorrow’s two meetings as NDJSON in one Gemini call. Signing out restored the 401 response.

Local Chromium verified the access prompt, cross-tab sign-out, the compact five-meeting agenda and the typed guest confirmation. The terminal ran the exact brief prompts on live Gemini with fictional data, including confirmed booking and rescheduling. The real Cal.com checks stayed read-only. [Terminal transcripts](owner-acceptance.md) · [Initial owner evaluations](m7-eval-report.md#owner-mode-initial-baseline) · [Agenda screenshot](images/scheduling-demo.png)

All 282 Python tests and 28 frontend tests passed. Lint, TypeScript, and the Next.js build passed. The focused owner rerun passed 4 of 4 cases, and the safety rerun passed 15 of 15 repetitions. [Follow-up evaluation](m7-eval-report.md#after-the-baseline)

1. Confirm the GitHub `origin`, the reviewed revision, and the linked Vercel project. Set secrets in the Preview environment. Set `DEMO_MODE=fake`, a fresh random `OWNER_ACCESS_CODE` and `STATE_SIGNING_SECRET`, the unbilled project's `GEMINI_API_KEY`, and a daily call cap below that project's quota. Deploy with `vercel deploy --environment preview` and check the deployment target. Keep Fluid compute enabled for the configured Flask duration. [Vercel deploy CLI](https://vercel.com/docs/cli/deploy) · [Preview environment variables](https://vercel.com/docs/environment-variables#preview-environment-variables) · [Function duration limits](https://vercel.com/docs/functions/limitations#max-duration)
2. Open the Preview in a fresh browser context. Check that the access prompt appears and that a direct request to `/api/warm` or `/api/chat` returns 401 with no calendar details. Exercise invalid-code handling and the per-IP cooldown. Sign in and verify the signed cookie is HttpOnly, SameSite=Strict, and Secure on HTTPS. The first signed-in warm request should show the host. Sign out and check that local history clears and both calendar endpoints again return 401.
3. Set the chat time zone to the Preview's configured `DEMO_TIME_ZONE`. Run the three exact brief prompts and record the responses. A backend starting before 14:00 local seeds the 15:00 meeting today. A later start seeds it tomorrow, so the exact prompt can exercise the signed fallback choice instead. Confirm that a list covering the seed dates displays compact agenda rows, that proposing a change makes zero calendar writes, and that a typed confirmation changes one booking. Run typed book, list, move, and cancel using the fake calendar. Capture the access prompt, agenda, and confirmation summary with fictional data.
4. Check that chat returns `application/x-ndjson` and `Cache-Control: no-store`, with at least one useful event before `turn_end`. Test Stop during a streamed reply. Record the first authenticated `/api/warm` round trip and the runtime's `flask_start` setup time. Compare warm and chat boot IDs before attributing timing to a reused Flask instance.
5. Record the verified Preview URL, test and eval totals, transcripts, and screenshots. Provide the shared app code to an authorized reviewer through a private channel. When Vercel Deployment Protection applies, provide authorized Vercel access separately. The app code belongs in neither the Preview URL nor the repository. [Vercel deployment protection](https://vercel.com/docs/deployment-protection)

Real Cal.com checks use only the isolated test account. A real Preview requires `CAL_API_KEY`, the allowed test event, and the owner code in Preview settings. The seeder's dry run verifies the planned slots. Wait for explicit approval before running the live seeder or any real booking cycle. Keep the test aliases private. [Cal.com verification](calcom-notes.md)

## Earlier visitor-model verification

The results below record earlier releases on the attendee-scoped application.

### Visitor Preview

Vercel built both services from the shared root. The browser pages loaded in Chromium, and `/api/warm` and `/api/chat` reached Flask. Chat responded with `application/x-ndjson` and `Cache-Control: no-store`. Status arrived at 100 ms, opening chips at 112 ms, and the first text at 1.12 s, before `turn_end` at 1.66 s. The browser booked, listed, rescheduled to a different time, and cancelled in fake mode. The final list returned zero active bookings.

Stop closed the browser stream in **25–27 ms** across two checks. Flask logged that both Gemini calls finished in about 1.9–2.1 s. The next browser turn completed normally. Calendar changes still require Confirm. [Vercel request cancellation](https://vercel.com/docs/functions/functions-api-reference#cancel-requests)

| Fresh Flask instance | First `/api/warm` round trip | Flask setup |
| --- | ---: | ---: |
| 1 | 3.88 s | 2.11 s |
| 2 | 3.39 s | 1.95 s |
| 3 | 2.74 s | 1.56 s |

In the browser run, the widget's `/api/warm` and the subsequent `/api/chat` logged the same Flask boot identifier.

### Visitor Production

The CLI automatically assigned the first deployment to Production. It had no Gemini or Cal.com credentials and was removed before the verified release. The release deployed revision `30e1590` with `DEMO_MODE` unset and only the isolated 30-minute Cal.com test event allowed. Both public pages returned HTTP 200. `/api/warm` exposed only the verified test event, and `/api/chat` returned NDJSON with `Cache-Control: no-store`.

| Flow | Independent Cal.com check |
| --- | --- |
| Book | Confirm created one active booking for the controlled test alias at the selected time. |
| List | The app showed the exact active booking returned by a fresh Cal.com list. The turn used one Gemini call and made zero writes. |
| Reschedule | Confirm cancelled the original and created an active replacement at a distinct time. |
| Cancel | Confirm cancelled the replacement. Both controlled test aliases had zero active upcoming bookings after the cycle. |

Each Confirm made one calendar write and used zero Gemini calls. The proposals made zero calendar writes.

Two Production Flask startups logged **2.04 s** and **2.16 s** of setup. The cycle's public `/api/warm` took **156 ms** and reused an already initialized instance. The warm request and all nine chat requests in the cycle logged the same Flask boot identifier.

### Typed confirmation release

Vercel deployed revision `da941d0` to the same public URL and isolated Cal.com test event. The public `/api/warm` round trip was 172 ms. The verifier loaded the booking card from the preceding signed proposal in Chromium, entered the contact fields, and typed `yes` in the message composer. The browser sent the current card ID and contact fields. The proposal made zero writes, and the typed confirmation made one create with zero Gemini calls.

Fresh Cal.com reads verified that the app listed the exact active booking, rescheduling cancelled the original and activated a replacement at a different time, and cancelling retired the replacement. Both test aliases had zero active upcoming bookings after the cycle. [Typed confirmation evaluation](m7-eval-report.md#typed-confirmation-regression)

### Expanded confirmation phrases release

Vercel deployed revision `b9913df` to Production. The verifier entered the approved test details on the signed booking card and typed `yes please` in Chromium. The proposal made zero writes. The typed reply made exactly one create with zero Gemini calls. An independent Cal.com read found the booking at the selected time. Confirm then cancelled it, and Cal.com verified zero active upcoming bookings for both test aliases. [Safety and typed reply rerun](m7-eval-report.md#expanded-confirmation-phrases)

The Cal.com account reported zero connected calendars and no booking destination. Vercel Production has `DAILY_MODEL_CALL_CAP=30` per process per day. The Google AI Studio project's current daily quota is visible in the signed-in dashboard. [Cal.com calendar API](https://cal.com/docs/api-reference/v2/calendars/get-all-calendars) · [AI Studio rate limits](https://aistudio.google.com/rate-limit)

### Schedule lookup and daytime availability release

Vercel deployed revision `a645132` to Production. “Show me my schedule,” “What's my schedule?” and “What's on my calendar?” each asked for the email used to book with zero Gemini calls and zero availability reads. “Which email do I provide?” explained that the attendee should use the address entered when booking. Supplying the approved test alias by itself resumed the lookup and showed an empty booking list. A second calendar request with that attendee produced the same list.

The Cal.com test event's schedule is Monday through Friday, 09:00–17:00 in `America/Los_Angeles`. In UTC, an undated “Schedule a meeting” request showed daytime starts at 17:00 and 17:30 across the offered dates. In `Asia/Kathmandu`, where the test event's opening hours fell outside the daytime window, the same request returned after-hours starts. All Production checks made zero calendar writes. [Cal.com schedule API](https://cal.com/docs/api-reference/v2/schedules/get-a-schedule) · [Schedule listing regression](m7-eval-report.md#schedule-listing-regression)

## Recording outline

Show the sign-in prompt, then resume after sign-in with the code off-screen. Type the three brief prompts and show the calendar agenda. Select a suggested time in text, provide a fictional guest's name and email in chat, then type `yes` on the confirmation summary. Move or cancel one meeting to show the same confirmation flow. Include the terminal version and the controls that are available as shortcuts. Aim for two to three minutes.
