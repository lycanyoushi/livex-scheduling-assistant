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

Before the test aliases were supplied, Cal.com returned HTTP 400 for synthetic addresses at `example.com` and `example.net`. The rejection concerned email validity, and a fresh slots read showed the requested time was still free. The documented reschedule contract has client test coverage. Live rescheduling is a later milestone check. [Cal.com API v2](https://cal.com/docs/api-reference/v2/introduction)

## Client behavior

The slots cache keys include the queried bounds, the time zone, and the booking UID for rescheduling. A write attempt clears the cached slot windows because a single host's calendar can affect several event types and because a transport failure can leave the result of a write uncertain. For a create rejected with HTTP 400, 409, or 422, the client checks fresh slots before assigning `slot_taken`. Authentication, missing-resource, and throttling responses keep their upstream classifications.

## Reproduce

Set `CAL_API_KEY` in `.env.local` and run `pnpm cal:probe` for the live read checks. If the account has several visible 30-minute event types, set `CAL_DEFAULT_EVENT_TYPE_SLUG` to the one intended for testing.

For the write check, set `CAL_PROBE_ATTENDEE_EMAIL` and `CAL_PROBE_CONFLICT_EMAIL` to distinct addresses that deliver to inboxes you control. Run `pnpm cal:probe --write-test`. The probe checks for a free slot, creates one synthetic booking, attempts a second booking at the same time and verifies cancellation of bookings it created. Cal.com can send notifications to the test addresses and organizer.
