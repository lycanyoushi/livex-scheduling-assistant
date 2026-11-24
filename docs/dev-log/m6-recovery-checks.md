# M6 recovery checks

The automated checks exercise each failure through the normal turn, action, or HTTP entry point. Calendar timeout tests use isolated FakeCal bookings and fault injection. The live smoke used an unbilled Gemini key, and the browser embed smoke only opened the Cal.com booker.

| Check | Observed behavior |
| --- | --- |
| Slot taken during Confirm or stale at proposal | Up to three fresh, signed alternatives. The confirmation recovery uses zero model calls. |
| Cal.com unavailable before a write, or returning 429 | `cal_down` with Retry and Book directly, or `rate_limited`. Zero model calls. |
| Primary Gemini returns a pre-text 503, timeout, or 429 | One backup attempt when the request can continue safely. After partial text, the stream ends without a second attempt. |
| Both models fail with usable data | Code-written availability chips or an attendee-filtered booking list, with signed refs and Retry. Existing interaction history stays available for another turn. |
| Models unavailable without usable data | Book directly via the official Cal.com embed or its HTTPS link. |
| Safety block, expired provider interaction, or invalid state token | A scheduling-only refusal or a session reset. An invalid token makes zero provider calls. |
| Invalid tool arguments or four-call limit | Schema failure returns an error result and allows one correction for that tool. Repeated invalid arguments disable that tool for the turn. Hitting the overall limit offers the booker. |
| Local rate limits | HTTP 429 with `Retry-After` at the per-IP cap. The daily budget counts backup attempts and resets at midnight Pacific time. Code-only actions still work when the model budget is exhausted. |
| Prefetch deadline | Normal model path begins when prefetch exceeds its deadline. |
| Duplicate Confirm | Concurrent and sequential replays of the same signed request share a single calendar write in one process. |

## Calendar write timeouts

Each confirmation sent one mutation before reconciliation. The tests covered all three possible results for each write.

| Write | Applied | Verified unchanged | Inconclusive or partial |
| --- | --- | --- | --- |
| Create | Matching active booking for the attendee, event, and requested start and end produces a booking card. | A complete lookup finds no matching booking. Retry remains available. | Missing pages, failed reads, or multiple matches retire the proposal and report uncertainty. |
| Cancel | Re-reading the same booking shows it was cancelled. The cancellation card is returned. | The original booking remains active. Retry remains available. | An inconclusive lookup retires the proposal and reports uncertainty. |
| Reschedule | The original is cancelled and a matching distinct replacement is found. The new time is shown. | The original is active and a complete lookup finds no replacement. Retry remains available. | A partial move reports the observed original or replacement status. Uncertainty retires the proposal. |

## Case 23 and live smoke

Case 23 passed with the fixture's primary 429 and backup 503. Four Thursday-afternoon options remained selectable. Selecting the first option produced a booking proposal with zero model calls and zero writes. [Case fixture](../../evals/conversations.yaml) · [Recovery tests](../../tests/test_recovery.py)

The live backup smoke made one 3.1 Flash-Lite call after the forced primary 503. The call completed in 4,992 ms. Both directions are verified for stored tool continuations with the default pair. [Gemini checks](../gemini-notes.md#m6-live-backup-checks)

Chrome opened the Cal.com modal through the local Next.js and Flask app, verified the ordinary HTTPS link and the iframe created by the official SDK, and closed the dialog with Escape. A browser run with intercepted responses separately verified Retry after HTTP 429, Retry after a verified unchanged confirmation, retirement after an uncertain write, and selection of a signed code-only option. No live calendar bookings were created, cancelled, or moved during M6 verification.

## Verification output

```text
Test Files  2 passed (2)
     Tests  12 passed (12)
Ran 162 tests in 4.774s
OK
```

ESLint passed with zero warnings. `pnpm build` compiled, checked TypeScript, and generated the three static build pages.
