# Cal.com API probe

## Live observations on November 22, 2026

The probe reached `https://api.cal.com` using the test account's API key. The first profile request with the default Python HTTP headers returned 403. Explicit `Accept: application/json` and a product user agent produced the Cal.com JSON responses below.

| Request | Version header | Live result |
| --- | --- | --- |
| `GET /v2/me` | Omitted | Authenticated 200, unauthenticated 401. Profile included `id`, `username`, `name`, `email`, `timeZone` and `avatarUrl`. |
| `GET /v2/event-types` | `2026-06-12` | 200 with a list of event types. Exactly one visible 30-minute event belonged to the test account. The booking fields advertised `field: notes` and `slug: notes`. |
| `GET /v2/slots` | `2024-09-04` | 200 with and without a bearer key for the selected public event. `format=range` returned an object keyed by date, with `{start, end}` objects inside each day's list. The application sends the key. |
| `GET /v2/bookings` | `2026-05-01` | 200 with a synthetic attendee filter, zero matching bookings, and cursor pagination containing `nextCursor` and `hasMore`. |
| `POST /v2/bookings` | `2026-02-25` | A synthetic booking with an authorized test alias returned 201. A second create at the occupied time with the other test alias returned 409. The response had `status: error` and an `error` object containing `code` and `message`. The client refreshed slots, confirmed the time had disappeared, and classified the result as `slot_taken`. |
| `GET /v2/bookings/{uid}` | `2026-02-25` | 200. The returned booking matched the create and attendee-filtered list results. The synthetic note round-tripped under `bookingFieldsResponses.notes`. |
| `POST /v2/bookings/{uid}/cancel` | `2026-02-25` | 200. A follow-up single-booking read confirmed `cancelled`. A separate paginated list across both test aliases counted one canceled booking and zero active bookings. |

Before the test aliases were supplied, Cal.com returned HTTP 400 for synthetic addresses at `example.com` and `example.net`. The rejection concerned email validity, and a fresh slots read showed the requested time was still free. [Cal.com API v2](https://cal.com/docs/api-reference/v2/introduction)

## M4 live confirmation checks

The live run sent eight UI actions through Flask's `/api/chat` on the configured test event. It selected and confirmed an opening, requested and confirmed a move to a different time, then requested and confirmed cancellation. All eight turns returned `ok` with zero model calls.

| Confirmation | Cal.com HTTP status | Turn latency |
| --- | --- | --- |
| Book | 201 | 1,391 ms |
| Reschedule | 201 | 1,649 ms |
| Cancel | 200 | 761 ms |

Cal.com returned a distinct replacement booking. The original was already cancelled after the move, and the replacement was active until its cancellation was confirmed. Follow-up reads verified both bookings cancelled and zero remaining active. The run took 7.08 seconds after preflight.

The browser check used live Gemini and the local calendar fixture. The first booking search took two model calls with prefetch scheduled for M5. Selecting a dated chip opened the confirmation, and Confirm returned a booking card. The browser downloaded the `.ics` file and verified the Google Calendar link. Selecting the second meeting by text took one model call and cancelled the intended meeting only after Confirm. Rescheduling with chips used zero model calls, including selection with the arrow keys. The empty-list booking button, More times, and Change also worked with zero model calls.

The first browser conflict search returned `model_down`. A later search succeeded. The fixture then occupied the selected slot during Confirm. Flask returned `slot_taken` with zero model calls and three selectable replacements. Clicking a replacement opened a fresh confirmation.

## Client behavior

The slots cache keys include the queried bounds, the time zone, and the booking UID for rescheduling. A write attempt clears the cached slot windows because a single host's calendar can affect several event types and because a transport failure can leave the result of a write uncertain. For a create rejected with HTTP 400, 409, or 422, the client checks fresh slots before assigning `slot_taken`. Authentication, missing-resource, and throttling responses keep their upstream classifications.

## Owner booking ranges and calendar time zone

On November 24, 2026, read-only requests on the same test account verified `GET /v2/bookings` with version `2026-05-01`. The API documents `afterStart` against the booking start and `beforeEnd` against its end. Both bounds were inclusive in the live response. Each query below used `status=cancelled` and omitted `attendeeEmail`. The match column tracks one existing synthetic booking from the test event. [Cal.com booking filters](https://cal.com/docs/api-reference/v2/bookings/get-all-bookings)

| Range relative to the synthetic booking | HTTP | Fixture matches |
| --- | --- | --- |
| One second outside start and end | 200 | 1 |
| Lower bound equals start | 200 | 1 |
| Upper bound equals end | 200 | 1 |
| Lower bound one second after start | 200 | 0 |
| Upper bound one second before end | 200 | 0 |

The client sends offset-aware bounds as UTC timestamps. For a query by local start date, `afterStart` can include an exact midnight start. An upper bound on the booking's end would drop a meeting that starts on that date and runs past midnight, so an upper start cutoff belongs in the application.

The profile and default availability schedule both returned `America/Los_Angeles`. The 30-minute event had no schedule override. The default schedule was 09:00–17:00 Monday through Friday, with zero date overrides. A seven-date slots query returned 64 openings, all starting between 09:00 and 16:30 Pacific. The reported odd hours did not appear in that window. [Cal.com schedules](https://cal.com/docs/api-reference/v2/schedules/get-all-schedules) · [Cal.com event types](https://cal.com/docs/api-reference/v2/event-types/get-an-event-type)

The follow-up reads at 09:13 Singapore time on November 25 still returned `America/Los_Angeles` for both settings. The 30-minute event still inherits the default schedule. The Singapore seeder check waits for the account owner to save both timezone settings. [Profile settings](https://cal.com/help/my-account/general-settings) · [Default availability schedule](https://cal.com/help/availabilities/availability-dashboard-overview)

## Guest invitations and booking scope

Cal.com API v2 documents a required primary `attendee` and an optional `guests` array of email addresses in `POST /v2/bookings` with version `2026-02-25`. It also documents a recurring booking input. [Create a booking](https://cal.com/docs/api-reference/v2/bookings/create-a-booking)

This app confirms one guest and one occurrence at a time. A request naming several guests asks the owner to choose one. A recurring booking request offers the first occurrence as a separate choice. Personal time blocks belong in the owner's calendar app.

## Reproduce

Set `CAL_API_KEY` in `.env.local` and run `pnpm cal:probe` for the live read checks. If the account has several visible 30-minute event types, set `CAL_DEFAULT_EVENT_TYPE_SLUG` to the one intended for testing.

For the write check, set `CAL_PROBE_ATTENDEE_EMAIL` and `CAL_PROBE_CONFLICT_EMAIL` to distinct addresses that deliver to inboxes you control. Run `pnpm cal:probe --write-test`. The probe checks for a free slot, creates one synthetic booking, attempts a second booking at the same time and verifies cancellation of bookings it created. Cal.com can send notifications to the test addresses and organizer.
