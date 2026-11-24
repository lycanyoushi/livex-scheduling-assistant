# Gemini M1 probe

Run `pnpm model:probe` using the `google-genai` version pinned in `requirements.txt`. Put the key for an unbilled Google AI Studio project in `.env.local`. The probe sends synthetic prompts only and reports structural event names, known fixture tool names, token counts, and sanitized errors. Each call has a 20-second deadline. The default is `gemini-3.5-flash-lite`, configurable through `GEMINI_MODEL`. Use `pnpm model:probe --model gemini-3.8-flash` to check the original default. Add `--legacy-diagnostic` to check that model's streaming `generateContent` endpoint.

## Live results on November 22, 2026

| Check | Observation |
| --- | --- |
| `client.models.get` with `gemini-3.8-flash` | Returned the expected model metadata with the same key. This establishes metadata access. |
| 3.8 Flash streaming Interactions | One call returned HTTP 503 (`InternalServerError`) before an event. Another stream emitted a single `error` event and no completion. A third call stalled during the initial stream and was stopped. A subsequent call with the wall-clock deadline timed out at 20 seconds before any event. |
| 3.5 Flash-Lite non-streaming, `store=False` | A separate synthetic call completed with `thinking_level="minimal"`. |
| 3.5 Flash-Lite streaming, `store=True` | A synthetic call completed with text and thought-signature deltas, 19 input tokens, 1 output token, 20 total tokens, and 0 cached tokens. |
| 3.5 Flash-Lite stateful capability suite | All eight assertions passed across nine calls. The initial call omitted `store`. Two continuations from its ID produced distinct branches and retained the fixture value in both branches. |
| 3.5 Flash-Lite streamed function calls | A single tool call used one `arguments_delta` fragment, and two parallel tool calls used two fragments in total. Arguments matched the fixtures when parsed after `step.stop`. The terminal interaction status was `requires_action`. |
| 3.5 Flash-Lite pending call and tool results | A new `user_input` alone completed after a tool call. A separate branch from the same tool-call interaction accepted `[function_result, user_input]` and produced both expected markers. A call with two `function_result` steps matched the two parallel call IDs and returned both fixtures. |
| 3.5 Flash-Lite output limit | A one-token cap ended with `interaction.completed`, `interaction.status == "incomplete"`, 27 input tokens, and 0 output tokens. It emitted no steps or tool calls. |
| 3.5 Flash-Lite safety filter | A fictional anti-bullying classification with the strictest harassment filter completed. The safety-block path was not triggered in the live probe. |
| 3.8 Flash streaming `generateContent` | The diagnostic returned HTTP 503 (`ServerError`) with no finish reason. |

Flash-Lite passed all eight checks across nine live calls. 3.8 Flash generation failed in these observations. Following approval, Flash-Lite is the default for the initial milestones, and both models remain in the M7 comparison. The installed SDK is `google-genai==3.6.1`. An offline fixture also passed for multi-fragment argument parsing, redacted logging, usage extraction, and the wall-clock deadline.

## Backup-model follow-up on November 22, 2026

Three synthetic streaming Interactions requests used the same unbilled key, `store=True`, `thinking_level="minimal"`, and a 20-second deadline. Google lists `gemini-3.1-flash-lite` input and output on the Standard free tier. [Gemini pricing](https://aistudio.google.com/docs/pricing#gemini-3.1-flash-lite)

| Check | Live observation |
| --- | --- |
| Backup access on the free key | A standalone request reported `gemini-3.1-flash-lite` and reached `completed`. Its exact acknowledgment assertion failed. The continuation below verified text generation on the backup. |
| Store a primary interaction | `gemini-3.5-flash-lite` accepted a synthetic random fixture code, returned the expected acknowledgment, and completed with 47 input tokens and 1 output token. |
| Continue on the backup | A new request asked for the fixture code and supplied the primary interaction through `previous_interaction_id`. The service reported `gemini-3.1-flash-lite` and reached `completed`. Two text deltas reproduced the code, which was present only in the primary turn. Usage was 61 input tokens and 11 output tokens. |

This verifies the planned primary-to-backup switch for stored text history.

### Pending tool calls on the backup model

Three additional synthetic streaming Interactions requests used the same unbilled key, `store=True`, `thinking_level="minimal"`, and a 20-second deadline each. A `gemini-3.5-flash-lite` parent ended in `requires_action` after emitting the expected `find_slots` function call and arguments. Both requests on `gemini-3.1-flash-lite` branched from that same stored parent through `previous_interaction_id` and supplied a result for its pending call.

| Continuation input | Live observation |
| --- | --- |
| `[function_result, user_input]` | Completed and returned both the synthetic tool result marker and the marker from the new user input. Usage was 208 input tokens and 7 output tokens. |
| `[function_result]` | Completed and returned the synthetic tool result marker. Usage was 190 input tokens and 5 output tokens. |

Both streams emitted text and thought-signature deltas without another tool call or error. The primary-to-backup capability gate is verified for both tested continuation forms. The reverse model switch and independent free-tier quotas remain unverified.

## SDK and documented behavior

- In Python, `client.aio.interactions.create` takes snake-case arguments including `system_instruction`, `generation_config`, `previous_interaction_id`, and `store`. `[function_result, user_input]` is a typed sequence of input steps. A function result supplies `call_id` and `result`. Each user-input text part is represented as `{"type": "text", "text": "…"}` in `content`.
- The SDK defines `step.start`, `step.delta`, `step.stop`, `interaction.completed`, and `error`. `event.delta.type == "arguments_delta"` uses `event.delta.arguments` for the JSON fragment. Step indices associate fragments with their call and `step.stop` marks the parse boundary. The completion payload has `interaction.status` and optional `interaction.usage`, including `total_input_tokens`, `total_output_tokens`, `total_thought_tokens`, `total_cached_tokens`, and `total_tokens`. The API reference documents `incomplete` for output-token exhaustion. [Interactions API reference](https://aistudio.google.com/api-reference/interactions-api/create-interaction)
- A completed tool request may end in `requires_action`. For the scheduling app, continue to send the application outcome as a `function_result` before new user text so the model receives the confirmation, dismissal, or supersession result. See the M7 review probe below for when a capped text response can be resumed.
- Google documents streaming safety blocks as an `error` event with `error.code` such as `safety`, `jailbreak`, `prohibited_content`, or `blocklist`. For non-streamed requests the corresponding HTTP status is 400. [Gemini safety settings](https://aistudio.google.com/docs/safety-settings#when-content-is-blocked)
- Google documents `store=True` by default, one day of retention on the free tier, and both model IDs as supported. One-day expiry was not measured. [Interactions overview](https://aistudio.google.com/docs/interactions-overview#data-storage-retention)
- Standard input and output for both models are listed as free of charge on the free tier. The project-specific RPM, TPM, and RPD were not verified. Read them in AI Studio before choosing `DAILY_MODEL_CALL_CAP`. RPD resets at midnight Pacific. [Gemini pricing](https://aistudio.google.com/docs/pricing), [Gemini rate limits](https://aistudio.google.com/docs/rate-limits)

## M5 latency probe on November 22, 2026

Run `node scripts/run-python.mjs -m scripts.latency_probe`. Use `--describe` to inspect the matrix offline. The serial run used a synthetic text turn and all five app tool schemas on an unbilled key with Standard inference. It paced request starts at least 10 seconds apart, capped attempts at 55, and reserved a 65-second backoff for quota errors. The probe made 49 attempts, including history seeds, with 46 successes and zero HTTP 429s. Project-specific quota ceilings were unavailable from the key. [Gemini pricing](https://aistudio.google.com/docs/pricing) · [Gemini rate limits](https://aistudio.google.com/docs/rate-limits)

Each row changes one variable from the stored 3.5 Flash-Lite baseline. All requests use `store=True`. For stored runs, a same-model parent contains a synthetic marker that every measured continuation must reproduce. The fresh request omits `previous_interaction_id` and returns a shorter acknowledgment. Successful-call timings include loop and SDK setup where incurred. p90 uses interpolation. Setup-inclusive first-text values for this run were reconstructed by adding the measured loop and SDK setup to time to first text. The probe now records wall-clock first-text timing directly.

| Configuration | Successful samples | First text p50 / p90 (ms) | Total p50 / p90 (ms) | Input tokens p50 / p90 | Thought tokens p50 / p90 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 3.5 Flash-Lite minimal, new client, stored, five tools | 9/9 | 2,533 / 6,269 | 3,580 / 9,399 | 779 / 779 | 0 / 0 |
| 3.5 Flash-Lite, persistent loop and client | 9/9 | 2,575 / 10,364 | 3,296 / 11,053 | 779 / 779 | 0 / 0 |
| 3.5 Flash-Lite, fresh interaction | 9/9 | 2,479 / 3,702 | 3,312 / 4,405 | 751 / 751 | 0 / 0 |
| 3.5 Flash-Lite, no tools | 9/9 | 2,505 / 3,152 | 3,238 / 3,957 | 78 / 78 | 0 / 0 |
| 3.1 Flash-Lite minimal | 8/9 | 2,782 / 3,209 | 3,567 / 3,998 | 779 / 779 | 0 / 0 |
| 3.8 Flash low | 0 measured | — | — | — | — |

3.1 Flash-Lite returned one HTTP 503 before text. Both 3.8 Flash history seeds timed out at the 20-second deadline, so there was no stored interaction to measure. Across successful baseline calls, SDK construction took 320 ms p50 and 351 ms p90. The eight persistent calls after the first incurred zero repeat construction time, with 2,551 ms p50 first text and 3,323 ms p50 total. The SDK's connection pool expires idle connections after five seconds. With ten-second pacing, the probe verified loop and client reuse, and TCP reuse remains unverified.

Removing schemas saved 701 median input tokens and 29 ms median first-text time. The tail was lower in that row. The small sequential sample and pronounced request-to-request variation limit attribution. The same-model stored and fresh requests differed by 55 ms in median first-text time. The fresh response used one output token, while stored responses used ten. The app retains stored history and the full scheduling tool list.

## M5b stored-history size probe

Run `node scripts/run-python.mjs -m scripts.latency_probe --history-sizes`. The probe created three synthetic parents, then rotated the order of six measured continuations per parent. It kept the prompt, expected marker, five tool schemas, model, thinking level, shared client, and ten-second request pacing fixed. All 21 attempts, including seeds, succeeded on the free key. Every measured continuation returned the identical marker with nine output tokens and zero thought tokens.

| Input tokens on continuation | Samples | First text p50 / p90 (ms) | Total p50 / p90 (ms) | Cached input tokens |
| ---: | ---: | ---: | ---: | --- |
| 979 | 6/6 | 3,168 / 4,537 | 3,876 / 5,428 | 0 in all six |
| 4,779 | 6/6 | 3,890 / 5,676 | 4,519 / 6,303 | 2,019 in five of six |
| 9,529 | 6/6 | 5,658 / 6,013 | 6,499 / 6,810 | 6,100 in three of six |

The longest parent was slower to first text than the shortest in all six paired rounds. Their medians differed by 2.49 s, and the median paired difference was 1.46 s. The middle size was slower than the shortest in three rounds. First text at the shortest size ranged from 2.20 to 5.00 s with constant cache usage. Larger history contributes to latency at these sizes. Cache behavior and request timing also varied, and the probe did not directly measure a 6,000-token threshold.

## M6 live backup checks

The reverse continuation probe used the unbilled key, stored interactions, minimal thinking, and synthetic text. It seeded a Gemini 3.1 Flash-Lite interaction ending in `find_slots`, then asked Gemini 3.5 Flash-Lite to continue from the same interaction ID and call ID. Both continuation shapes recovered the expected marker. The three calls completed with zero HTTP 429s.

| Model | Request | Result | Input / output tokens |
| --- | --- | --- | ---: |
| 3.1 Flash-Lite | Store a pending `find_slots` call | Pending call received | 177 / 16 |
| 3.5 Flash-Lite | Continue with `[function_result]` | Marker returned | 211 / 16 |
| 3.5 Flash-Lite | Continue with `[function_result, user_input]` | Marker returned | 234 / 18 |

In a separate application smoke test, a forced primary HTTP 503 returned before text. The real 3.1 Flash-Lite backup completed in 4,992 ms and streamed a response. The turn recorded `outcome=ok`, `fallback=backup_model`, and roles `primary`, `backup`, with zero calendar writes against FakeCal. [Recovery checks](dev-log/m6-recovery-checks.md)

## M7 review output-cap probe

Run `node scripts/run-python.mjs -m scripts.model_probe --model gemini-3.5-flash-lite --incomplete`. The same diagnostic accepts `gemini-3.1-flash-lite`. On the unbilled key, each model stored an interaction capped at 48 tokens, streamed text, and ended at `interaction.completed` with status `incomplete`. Both accepted that interaction as `previous_interaction_id` and recovered the marker on a follow-up with status `completed`. All four attempts succeeded without quota errors.

| Model | Capped reply input / output tokens | Continuation input / output tokens |
| --- | ---: | ---: |
| 3.5 Flash-Lite | 53 / 44 | 114 / 9 |
| 3.1 Flash-Lite | 53 / 44 | 114 / 9 |

The app now caps output at 800 tokens. A capped reply with visible text ends successfully with `(reply cut short)`. It retains the interaction ID only for the two verified models and when no tool call was present. A capped reply without text remains an error eligible for the normal pre-text backup. Model-call metrics and server logs record sanitized failure reasons for `model_down`, with HTTP status when available. [Gemini token-limit documentation](https://ai.google.dev/gemini-api/docs/thought-signatures#token-limits-and-max_output_tokens)
