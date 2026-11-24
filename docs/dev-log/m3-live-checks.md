# M3 live checks

Revision `b0568d2`. Times are measured in milliseconds. A null first-text time means the call produced no text.

## Browser booking

The original live browser run sent text through Next.js and Flask, rendered a dated proposal, and made no booking before Confirm. Confirm created one booking on the configured Cal.com test event. It made zero model calls.

```jsonl
{"turn":"browser proposal","elapsed_ms":8122.075,"status":"ok","model_calls":[{"status":"requires_action","elapsed_ms":3306.239,"first_text_ms":null},{"status":"requires_action","elapsed_ms":4169.318,"first_text_ms":3481.202}],"errors":[]}
{"turn":"browser Confirm","elapsed_ms":1227.755,"status":"ok","model_calls":[],"cal_create_http":201,"errors":[]}
```

The cleanup canceled the one new booking and verified its canceled status. A final read showed zero active bookings for the test alias.

## Eleven-turn chat and Retry

The read-only chat was rerun on the same M3 revision on 2026-11-22 Pacific to capture each turn's timing. It used live Gemini and the test Cal.com account. Turn 6 first injected a timeout before contacting Gemini. The failed response preserved the signed session, and resending the same input completed successfully.

```jsonl
{"turn":"01","elapsed_ms":7425.51,"status":"ok","model_calls":[{"status":"completed","elapsed_ms":6620.945,"first_text_ms":5876.646}],"errors":[]}
{"turn":"02","elapsed_ms":4375.612,"status":"ok","model_calls":[{"status":"completed","elapsed_ms":4374.607,"first_text_ms":3560.165}],"errors":[]}
{"turn":"03","elapsed_ms":3676.203,"status":"ok","model_calls":[{"status":"completed","elapsed_ms":3675.312,"first_text_ms":2936.274}],"errors":[]}
{"turn":"04","elapsed_ms":3806.787,"status":"ok","model_calls":[{"status":"completed","elapsed_ms":3805.814,"first_text_ms":3036.886}],"errors":[]}
{"turn":"05","elapsed_ms":3659.915,"status":"ok","model_calls":[{"status":"completed","elapsed_ms":3658.982,"first_text_ms":2909.81}],"errors":[]}
{"turn":"06 (injected timeout)","elapsed_ms":0.844,"status":"failed","model_calls":[{"status":"timeout","elapsed_ms":0.015,"first_text_ms":null}],"errors":["timeout"]}
{"turn":"06 (Retry)","elapsed_ms":12521.794,"status":"ok","model_calls":[{"status":"requires_action","elapsed_ms":5540.506,"first_text_ms":null},{"status":"completed","elapsed_ms":6440.245,"first_text_ms":3026.387}],"errors":[]}
{"turn":"07","elapsed_ms":9406.323,"status":"ok","model_calls":[{"status":"completed","elapsed_ms":9404.981,"first_text_ms":8521.221}],"errors":[]}
{"turn":"08","elapsed_ms":4158.32,"status":"ok","model_calls":[{"status":"completed","elapsed_ms":4156.963,"first_text_ms":3247.904}],"errors":[]}
{"turn":"09","elapsed_ms":4135.486,"status":"ok","model_calls":[{"status":"completed","elapsed_ms":4134.281,"first_text_ms":3393.252}],"errors":[]}
{"turn":"10","elapsed_ms":3707.567,"status":"ok","model_calls":[{"status":"completed","elapsed_ms":3706.383,"first_text_ms":2959.356}],"errors":[]}
{"turn":"11","elapsed_ms":5483.542,"status":"ok","model_calls":[{"status":"completed","elapsed_ms":5482.104,"first_text_ms":4763.734}],"errors":[]}
```

All 11 completed turns had zero provider errors. The only failed attempt was the injected timeout.
