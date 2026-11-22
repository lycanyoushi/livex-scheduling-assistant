# M5 live checks

On November 22, 2026, ten read-only turns ran through the local Next.js rewrite and Flask with live Gemini 3.5 Flash-Lite and the Cal.com test account. The session continued from the signed interaction ID and recalled a fictional name from an earlier turn. All ten turns returned `ok`. On turn 10, Gemini returned HTTP 503 before text and the built-in retry completed. The chat made no calendar mutations.

A browser check independently observed the mount-time `GET /api/warm` return HTTP 200 with the browser time zone. The live chat also warmed the same route before turn 1. The first chat turn reported cache hits for the profile, catalog, and slots. The focused warm-cache test verifies the matching 14-day slot key.

## Before and after

The M3 row uses the 11 successful chat turns in [M3 live checks](m3-live-checks.md). The M5 row uses the ten turns below. These are separate workloads. Time to first model text is measured within a model call that emitted text. End-to-end timing starts before the Next.js request. p90 uses the nearest rank.

| Measurement | M3 p50 / p90 (ms) | M5 p50 / p90 (ms) | Target (ms) |
| --- | ---: | ---: | ---: |
| First model text | 3,247.904 / 5,876.646 | 6,880.595 / 9,266.888 | p50 ≤ 1,500 |
| Complete server turn | 4,158.320 / 9,406.323 | 10,545.351 / 16,675.968 | p50 ≤ 3,000, p90 ≤ 6,000 |
| First status through Next.js | — | 20.163 / 34.240 | p50 ≤ 300 |
| First text through Next.js | — | 7,460.718 / 15,860.545 | — |

The M5 run made 15 model calls across ten turns, with 5 single-call turns against the 70% target. The slowest first model text was 11,337.812 ms and the slowest turn was 20,888.274 ms. The median server time outside the measured model calls was 4.0 ms, with a maximum of 614.5 ms. Successful-call input tokens grew from 1,511 to 10,241 in the stored session. The wait on Gemini's request path dominated the measured turn times. Request latency combines network and provider time. A tier-specific latency floor remains unmeasured. [Controlled latency probe](gemini-notes.md#m5-latency-probe-on-november-22-2026)

## Eval case 2

Two initial live runs used one model call and zero tools, but returned no usable slot refs. The diagnostic reply asked for an email before showing openings. The prompt now confines email collection to the confirmation form and existing-booking operations, and tells the model to offer matching slot refs for new-meeting date requests.

With that change, the live Gemini run against the approved FakeCal fixture passed. Prefetch loaded six Thursday-afternoon slots in 1.156 ms and the model offered four dated refs at 12:00, 13:30, 14:30, and 15:00 in Asia/Singapore on November 26. The turn used one model call with zero tool calls, zero unknown refs, and no proposal. It emitted its first status in 2.291 ms, reached first model text in 2,300.381 ms, and finished in 3,475.468 ms.

## Turn metrics

Times are in milliseconds. A null model first-text time means that call emitted no text. Each row also includes first status and first visible text as observed through Next.js.

```jsonl
{"turn":"01","elapsed_ms":3485.862,"status":"ok","first_feedback_ms":22.379,"first_text_ms":2713.57,"model_calls":[{"status":"completed","elapsed_ms":3481.622,"first_text_ms":2690.012,"input_tokens":1511,"thought_tokens":0}],"errors":[],"prefetch":{"ms":2.850899996701628,"slots":6,"bookings":0,"timedOut":false},"cal_cache_hits":3}
{"turn":"02","elapsed_ms":6871.768,"status":"ok","first_feedback_ms":6.447,"first_text_ms":6110.699,"model_calls":[{"status":"requires_action","elapsed_ms":3220.33,"first_text_ms":null,"input_tokens":1954,"thought_tokens":0},{"status":"completed","elapsed_ms":3036.904,"first_text_ms":2270.653,"input_tokens":2080,"thought_tokens":0}],"errors":[],"prefetch":{"ms":1.065699994796887,"slots":0,"bookings":0,"timedOut":false},"cal_cache_hits":3}
{"turn":"03","elapsed_ms":16675.968,"status":"ok","first_feedback_ms":34.24,"first_text_ms":15860.545,"model_calls":[{"status":"requires_action","elapsed_ms":8154.282,"first_text_ms":null,"input_tokens":2576,"thought_tokens":0},{"status":"completed","elapsed_ms":8268.694,"first_text_ms":7435.133,"input_tokens":2846,"thought_tokens":0}],"errors":[],"prefetch":{"ms":1.3477000029524788,"slots":2,"bookings":0,"timedOut":false},"cal_cache_hits":3}
{"turn":"04","elapsed_ms":7766.59,"status":"ok","first_feedback_ms":23.899,"first_text_ms":6793.515,"model_calls":[{"status":"completed","elapsed_ms":7762.682,"first_text_ms":6768.563,"input_tokens":3587,"thought_tokens":0}],"errors":[],"prefetch":{"ms":2.71499999507796,"slots":6,"bookings":0,"timedOut":false},"cal_cache_hits":3}
{"turn":"05","elapsed_ms":7694.302,"status":"ok","first_feedback_ms":17.947,"first_text_ms":7001.321,"model_calls":[{"status":"completed","elapsed_ms":7690.887,"first_text_ms":6992.627,"input_tokens":4287,"thought_tokens":0}],"errors":[],"prefetch":{"ms":2.1558000007644296,"slots":6,"bookings":0,"timedOut":false},"cal_cache_hits":3}
{"turn":"06","elapsed_ms":12986.43,"status":"ok","first_feedback_ms":36.103,"first_text_ms":6456.468,"model_calls":[{"status":"completed","elapsed_ms":12984.905,"first_text_ms":6432.521,"input_tokens":4987,"thought_tokens":0}],"errors":[],"prefetch":{"ms":0,"slots":0,"bookings":0,"timedOut":false},"cal_cache_hits":2}
{"turn":"07","elapsed_ms":20888.274,"status":"ok","first_feedback_ms":6.471,"first_text_ms":20030.961,"model_calls":[{"status":"requires_action","elapsed_ms":8151.738,"first_text_ms":null,"input_tokens":5767,"thought_tokens":0},{"status":"completed","elapsed_ms":12200.181,"first_text_ms":11337.812,"input_tokens":7060,"thought_tokens":0}],"errors":[],"prefetch":{"ms":233.47900000226218,"slots":16,"bookings":0,"timedOut":false},"cal_cache_hits":2}
{"turn":"08","elapsed_ms":12396.986,"status":"ok","first_feedback_ms":17.567,"first_text_ms":11160.531,"model_calls":[{"status":"requires_action","elapsed_ms":6209.273,"first_text_ms":null,"input_tokens":7890,"thought_tokens":0},{"status":"completed","elapsed_ms":6184.802,"first_text_ms":4943.782,"input_tokens":8599,"thought_tokens":0}],"errors":[],"prefetch":{"ms":0,"slots":0,"bookings":0,"timedOut":false},"cal_cache_hits":3}
{"turn":"09","elapsed_ms":8693.715,"status":"ok","first_feedback_ms":31.582,"first_text_ms":7920.115,"model_calls":[{"status":"completed","elapsed_ms":8689.717,"first_text_ms":7896.896,"input_tokens":9551,"thought_tokens":0}],"errors":[],"prefetch":{"ms":2.7513999957591295,"slots":6,"bookings":0,"timedOut":false},"cal_cache_hits":3}
{"turn":"10","elapsed_ms":14596.552,"status":"ok","first_feedback_ms":10.926,"first_text_ms":13871.662,"model_calls":[{"status":"model_down","elapsed_ms":4598.713,"first_text_ms":null,"input_tokens":0,"thought_tokens":0},{"status":"completed","elapsed_ms":9996.265,"first_text_ms":9266.888,"input_tokens":10241,"thought_tokens":0}],"errors":[],"prefetch":{"ms":0,"slots":0,"bookings":0,"timedOut":false},"cal_cache_hits":2}
```
