# M8 verification

## Stream and offer limits

The primary has an eight-second deadline for its first raw stream event. Both models use an eight-second idle deadline once visible text starts and retain the 20-second overall limit. A pre-text failure can go to the backup under the existing continuation rules. A stalled reply after text closes the stream, displays the partial reply with `(reply cut short)`, and keeps the previous completed interaction. Tests cover cancellation, continued raw-event activity, and state preservation. The held-out run exercised the first-event backup live. [Held-out evidence](../m7-eval-report.md#held-out-rewordings)

Python limits each reply to four distinct selectable slot refs across groups and inline markers. Extra times render as date and time text in both clients. Tests cover interleaved and fragmented markers and verify that proposals still use signed refs.

## Browser and demo calendar

- Desktop Chromium opened the real Next.js app through the development rewrite to Flask with `DEMO_MODE=fake`, an empty Cal.com key, and a loopback Cal.com base URL. The flow reached booking, move, listing, and cancellation through the app. The fictional booking was cancelled. A calendar test separately verified that rescheduling to a different start frees the old slot and occupies the new one.
- The browser focused the booking result after Confirm. Diagnostics displayed zero model calls for the confirmation, and its event view omitted the signed token. Reload restored the transcript and signed state. New chat cleared them.
- A Chromium smoke using a synthetic event stream measured the widget at 400×640 on desktop and full-screen at 640 px and 390 px widths. It verified dark mode, the composer inside the mobile viewport, searchable time-zone changes, and Escape returning focus to the launcher. The widget does not warm until opened.
- The terminal loaded the shared fake calendar. Its availability rolls with the clock, and fake changes remain in memory.

## Provider configuration check

`pnpm check` authenticated the Cal.com profile read and fetched Gemini model metadata. In fake mode it fetched Gemini model metadata and skipped Cal.com. Both checks exited successfully without showing credentials.

## Build and test results

The Vitest suite passed all 16 tests across three files. The Python suite passed all 208 tests. ESLint passed with zero warnings. The production Next.js build type-checked and produced `/` and `/widget-demo` as static page routes.

## Review follow-up

Weekday exclusions now apply in the request's local time zone to prefetched chips, `find_slots` results and alternatives, and previously issued slot refs used in a reply or proposal. The app skips early slot offers if it cannot resolve an exclusion. The two booking-list rewordings also reach the one-call path. These changes followed the frozen 5/8 held-out run. [Held-out evidence](../m7-eval-report.md#held-out-rewordings)

The model receives a four-time offer limit and uses reference markers for exact time labels. The terminal keeps the number for a selectable time and elides its adjacent duplicate label. A booking already shown in an explicit list can appear inline by ref, with its full details printed once in the list. For standalone bookings, the terminal removes an immediately repeated time range and keeps the title and date. Repeated ranges at overflow refs are also elided.

Gemini call metrics now record `firstEventMs` for the first raw stream event, including an event with no user-visible text. Any timeout after visible text keeps the partial reply, adds `(reply cut short)`, and preserves the last good interaction. The named regression test moves the first text to 13 seconds and exercises the 20-second overall timeout.

### First raw event measurement

The fixed ten-turn driver ran once through Next.js and Flask against its read-only fixture on an unbilled Gemini key. All ten turns completed with one model call each. Healthy means a call that returned `completed` or `requires_action`. The measurement starts before the provider connection opens and ends at the first raw stream event. [Per-turn trace](m8-review-live-trace.jsonl)

| Healthy calls | p50 | p90 | Maximum |
| ---: | ---: | ---: | ---: |
| 10 | 2,476.50 ms | 3,234.70 ms | 4,078.00 ms |

The maximum exceeded the three-second threshold for switching to five seconds, so the primary uses the eight-second first-event deadline.

### Live terminal check

In the final `DEMO_MODE=fake` session, “What times are free tomorrow afternoon?” returned four numbered times on November 24 and said more were available. A confirmed fictional booking appeared as one entry when asked “Could you pull up the meetings I'm booked for?” Diagnostics reported one completed model call, one prefetched booking, and zero tool calls. The first smoke had exposed a second rendering of an inline ref for an already-listed booking. The final session verified that correction.

The follow-up with Mondays and Wednesdays excluded showed only Tuesday, Thursday, and Friday. The model referred back to two of the displayed times and introduced no additional time. The fictional booking was cancelled before exiting. All calendar changes were in memory.

The final suite passed 222 Python tests and 16 frontend tests. ESLint and the production build also passed.
