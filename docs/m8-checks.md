# M8 verification

## Stream and offer limits

The primary has an eight-second deadline for its first raw stream event. Both models use an eight-second idle deadline once visible text starts and retain the 20-second overall limit. A pre-text failure can go to the backup under the existing continuation rules. A stalled reply after text closes the stream, displays the partial reply with `(reply cut short)`, and keeps the previous completed interaction. Tests cover cancellation, continued raw-event activity, and state preservation. The held-out run exercised the first-event backup live. [Held-out evidence](m7-eval-report.md#held-out-rewordings)

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
