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

## SDK and documented behavior

- In Python, `client.aio.interactions.create` takes snake-case arguments including `system_instruction`, `generation_config`, `previous_interaction_id`, and `store`. `[function_result, user_input]` is a typed sequence of input steps. A function result supplies `call_id` and `result`. Each user-input text part is represented as `{"type": "text", "text": "…"}` in `content`.
- The SDK defines `step.start`, `step.delta`, `step.stop`, `interaction.completed`, and `error`. `event.delta.type == "arguments_delta"` uses `event.delta.arguments` for the JSON fragment. Step indices associate fragments with their call and `step.stop` marks the parse boundary. The completion payload has `interaction.status` and optional `interaction.usage`, including `total_input_tokens`, `total_output_tokens`, `total_thought_tokens`, `total_cached_tokens`, and `total_tokens`. The API reference documents `incomplete` for output-token exhaustion. [Interactions API reference](https://aistudio.google.com/api-reference/interactions-api/create-interaction)
- A completed tool request may end in `requires_action`. For the scheduling app, continue to send the application outcome as a `function_result` before new user text so the model receives the confirmation, dismissal, or supersession result. `incomplete`, `failed`, and stream errors should leave the last good interaction ID in place.
- Google documents streaming safety blocks as an `error` event with `error.code` such as `safety`, `jailbreak`, `prohibited_content`, or `blocklist`. For non-streamed requests the corresponding HTTP status is 400. [Gemini safety settings](https://aistudio.google.com/docs/safety-settings#when-content-is-blocked)
- Google documents `store=True` by default, one day of retention on the free tier, and both model IDs as supported. One-day expiry was not measured. [Interactions overview](https://aistudio.google.com/docs/interactions-overview#data-storage-retention)
- Standard input and output for both models are listed as free of charge on the free tier. The project-specific RPM, TPM, and RPD were not verified. Read them in AI Studio before choosing `DAILY_MODEL_CALL_CAP`. RPD resets at midnight Pacific. [Gemini pricing](https://aistudio.google.com/docs/pricing), [Gemini rate limits](https://aistudio.google.com/docs/rate-limits)
