# M5b repeatable latency checks

The benchmark used the same [committed driver](../scripts/live_chat.py), ten fictional prompts in one session, live Gemini 3.5 Flash-Lite with minimal thinking, and local Next.js rewrites to Flask. The baseline backend was M5 (`7b4d792`). Driver commit `6086512` contains that backend and the benchmark entry point. The driver serves a read-only FakeCal fixture fixed at Sunday, November 22, 2026, 21:00 in Asia/Singapore. Thursday afternoon is closed in every run. No Cal.com writes are possible in the benchmark.

The driver records the prompt, prescan and prefetch results, sanitized tool names and arguments, and per-call input, output, and cached tokens. The [complete traces](m5b-live-traces.jsonl) include both post-change runs. First useful content is the first visible chips or text through Next.js. The timing summary uses successful turns and interpolated p90, with model first text measured within calls that produced it.

## Paired results

| Measurement | M5 baseline p50 / p90 (ms) | First M5b run p50 / p90 (ms) | Final M5b run p50 / p90 (ms) | Target (ms) |
| --- | ---: | ---: | ---: | ---: |
| First model text | 5,755.8 / 6,495.4 | 2,702.5 / 3,220.9 | 3,687.7 / 5,846.0 | p50 ≤ 1,500 |
| Complete server turn | 6,668.3 / 8,443.0 | 3,492.9 / 4,442.7 | 4,508.2 / 6,672.8 | p50 ≤ 3,000, p90 ≤ 6,000 |
| First status through Next.js | 196.3 / 213.9 | 200.7 / 209.4 | 191.3 / 207.7 | p50 ≤ 300 |
| First text through Next.js | 6,018.8 / 7,738.1 | 2,908.6 / 3,421.5 | 3,882.3 / 6,034.7 | — |
| First useful content through Next.js | 6,018.8 / 7,738.1 | 2,487.4 / 3,421.5 | 3,291.6 / 6,034.7 | — |
| Complete request through Next.js | 6,847.5 / 8,641.9 | 3,683.8 / 4,635.8 | 4,701.3 / 6,859.2 | — |

The baseline completed ten turns, with nine one-call turns. The first M5b run completed nine of ten. Turn 5 returned `model_down` after text had started, so the following turn continued from the last good interaction. All nine successful turns used one model call. The exact provider failure during the stream was unavailable. The final run completed all ten with one model call and zero tool calls on every turn. Last-call input grew from 1,465 to 8,449 tokens in the baseline and 1,539 to 3,606 in the final run. Turn 6 took 670.6 ms after its first model text in the baseline and 754.2 ms in the final run.

## Availability turns

| Turn | Prompt | Baseline first useful | Final first useful | Trace |
| ---: | --- | ---: | ---: | --- |
| 2 | Can I grab thirty minutes Thursday afternoon? | 12,701.2 ms | 3,099.2 ms | Read-only availability request. The before trace shows `find_slots` for exactly `2026-11-26` afternoon after prefetch checked that window and found zero. The final run answered using the checked-empty result in one model call. |
| 3 | What about Friday morning? | 2,544.8 ms | 219.3 ms | Prefetch found two Friday-morning slots. The older M5 run made two model calls, while this fixture baseline made one. The final run showed both slots before its only model response. |
| 7 | Can you show me some times next week? | 5,906.3 ms | 191.2 ms | Prefetch found sixteen openings across Nov 23–27. The older M5 run made two model calls, while this fixture baseline made one. The final run offered four dated chips, with one model call. |
| 8 | Does any day have a morning option? | 6,640.8 ms | 198.1 ms | Prescan knew `morning` but identified neither a date nor a request for slots. The new hint searched seven local dates, found six morning slots, and showed four before the model reply. |

The early groups stay above the model reply in the browser. The controls become selectable after the successful turn commits its signed state.

## Case 2 and rotation

With the approved open-Thursday eval fixture, live case 2 offered four dated chips at 12:00, 13:30, 14:30, and 15:00 on November 26 in Asia/Singapore. Flask emitted the group immediately after refs, at 6.425 ms, before first model text at 2,563.166 ms. The turn finished in 3,435.665 ms with one model call, zero tool calls, and zero unknown refs.

The final ten-turn run remained below the 6,000-token rotation threshold. In a separate live check, a signed fixture reported 6,001 tokens on its prior call. The next turn started a fresh interaction, recalled the fictional name from the recap, and used 1,386 input tokens in one model call. The unit test covers dropping an old pending provider call, retaining the open proposal and its slot at ref capacity, and restoring old state after a failed rotation. [Controlled stored-history probe](gemini-notes.md#m5b-stored-history-size-probe)

## Reproduce

Start the read-only fixture with `node scripts/run-python.mjs -m scripts.live_chat serve --port 5001`. Run Next.js on loopback with `FLASK_ORIGIN=http://127.0.0.1:5001`. Run `node scripts/run-python.mjs -m scripts.live_chat run --origin http://127.0.0.1:3100 --label before --report PATH` against the baseline and repeat with `--label after` and a fresh report path against the final app. The fixture provenance check prevents the driver from using a normal calendar service.
