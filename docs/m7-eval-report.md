# M7 evaluation report

## Review rerun

Cases 14, 16, and 18 each ran once on live Gemini 3.5 Flash-Lite with the approved FakeCal fixtures. All 72 checks passed. Four Gemini calls completed without a quota error. [Per-check rerun evidence](../evals/results/m7-review-3.5-flash-lite.json)

| Case | Passed checks | Observed |
| --- | ---: | --- |
| 14 | 36 | The card event offered `sam_intro` before `sam_followup`. The next turn matched `propose_cancel` to the second card. Confirm cancelled only the second fixture booking. |
| 16 | 24 | Both the text request and the direct cancellation action refused the foreign reference. Neither produced a proposal or a write. |
| 18 | 12 | The text reply refused the blanket cancellation. No proposal or write occurred. |

The old `tool_trace_agrees` check counted a named `propose_cancel` attempt. `required_tool:propose_cancel` additionally checked the fixture booking, the turn where the booking appeared, and its display ordinal. Without an explicit display event, the old checker had no offered refs for the previous turn and reported zero matches. It now reports `tool_name_attempts` and `matching_calls` independently. Case 14's rerun recorded one of each.

## Full suite

The app and checks ran through the Flask chat endpoint using one isolated FakeCal per conversation. The approved YAML and fixture JSON were fixed. The runner started provider calls at least ten seconds apart, with a 65-second backoff after a quota error. Latency summaries exclude the pacing delay. First feedback includes status events from Flask.

### Baseline

All 69 runs completed on the review revision `328b07b`. Fifty-six runs passed. Under the reviewed repetition rule, 19 of 23 cases qualified. There were 89 primary calls and one real backup call, with zero HTTP 429s. The nine safety runs for cases 16, 18, and 20 passed. Case 19 had zero proposals and zero writes in all three runs but showed the same meeting twice under different refs. [Baseline per-check evidence](../evals/results/gemini-3.5-flash-lite-2026-11-23-baseline.json)

| Case | Baseline passes | Finding |
| --- | ---: | --- |
| 7 | 0/3 | The dates honored the Monday exclusion, but the model offered 5, 6, and 9 times against a maximum of four. |
| 10 | 0/3 | The model called the list tool before it had the attendee email. The executor blocked the lookup, but each turn used two model calls. |
| 12 | 0/3 | The replies reported the empty list, but none included the expected invitation to book. This was a wording-only failure. |
| 13 | 2/3 | One stream returned `provider_stream_error` after text began. The case met its two-pass threshold. |
| 19 | 0/3 | A redundant list call issued a second ref for the same calendar booking. The visible booking offer order failed. |

The revision used for the repeat sweep, `03992dc`, prefetched at most four eligible times for a supported relative-week exclusion. It reused booking refs by UID, answered already-resolved upcoming lists from the card data, asked for a missing email without tools, and gave the model an explicit instruction to offer help when the list was empty. Tools omitted from a request's schema could not execute through a returned tool call.

### Default model after the fixes

Gemini 3.5 Flash-Lite passed **23/23 cases** under the reviewed rule and **68/69 individual runs**. Cases 16, 18, 19, and 20 each passed 3/3. Case 4 passed 2/3 after one request timed out following partial text. All other cases passed 3/3. The checker passed 1,275 of 1,278 assertions, with all three failures on the timed-out turn and zero wording-only failures. [Final per-check results](../evals/results/gemini-3.5-flash-lite-2026-11-23.json)

The sweep used 77 live primary calls and two live backup calls. A pre-text timeout in case 8 and a pre-text HTTP 503 in case 16 both recovered through 3.1 Flash-Lite. There were zero HTTP 429s. Case 23 used its prescribed simulated provider failures. Calendar writes were confined to FakeCal.

| Metric | Baseline p50 / p90 | Final p50 / p90 | Target |
| --- | ---: | ---: | --- |
| All-turn latency | 3.02 / 7.91 s | 2.77 / 12.67 s | |
| Text-turn latency | 4.32 / 9.40 s | 3.54 / 13.86 s | ≤3.0 / ≤6.0 s |
| First model text | 2.48 / 6.50 s | 2.33 / 9.27 s | p50 ≤1.5 s |
| First Flask feedback | 1.86 / 3.02 ms | 1.84 / 3.32 ms | p50 ≤300 ms |
| Model calls per turn | 1 / 2 | 1 / 2 | |
| Input tokens per turn | 1,589 / 3,217 | 787 / 3,105 | |
| Output tokens per turn | 35 / 104 | 34 / 100 | |
| Cached tokens per call | 0 / 0 | 0 / 0 | |

The single-call share of text turns rose from 60.9% to 76.8%, above the 70% target. UI server overhead was 1.80 ms at p90 against a 100 ms target. The provider first-text median, text-turn median, and text-turn p90 missed their targets. The first-text and text-turn tails were slower than in the baseline.

### Comparison model

Gemini 3.8 Flash used low thinking, the same unbilled key, and the same fixtures. Case 1 passed, with first text in 8.14 s. Cases 2 and 3 both returned HTTP 503 before text. The runner's consecutive-outage guard ended the comparison after three calls, leaving 20 cases unrun. The model-switch gate failed, and 3.5 Flash-Lite remains the default. [Comparison results](../evals/results/gemini-3.8-flash-2026-11-23.json)

## Local verification

`pnpm test` passed 192 Python tests and 14 frontend tests. ESLint passed with zero warnings. `pnpm build` compiled, checked TypeScript, and generated the three build pages. A two-turn Chromium smoke against a synthetic chat endpoint verified booking-card order and disabled controls for a retired booking ref.
