# TAMU AI gateway — observed behavior

Established by probing the live API on **2026-07-23** and re-probed on
**2026-11-17**. Where the gateway changed between the two, both are recorded. The
official documentation at `https://docs.tamus.ai/docs/prod/api-tool/` returns
**HTTP 403** to any non-browser client, so this file is the working record.

Base URL: `https://chat-api.tamu.ai/openai`
Auth: `Authorization: Bearer sk-...` (key from chat.tamu.ai → Settings → API Key)

The gateway is LiteLLM in front of Open WebUI, routing to Bedrock (Claude), Azure/
OpenAI (GPT, gpt-image), and Google (Gemini). Most of the surprises come from that
layering: errors from different tiers reach you in different formats.

---

## What changed between 2026-07 and 2026-11

| Behavior | 2026-07-23 | 2026-11-17 |
|---|---|---|
| Upstream errors | HTTP 200 + `{"error": {...}}` | HTTP 4xx/5xx + `{"detail": "🚫 **...**"}` |
| `response_format` on Claude Sonnet 4.6 | 200, content `{}` | 200, real content (often ` ```json `-fenced) — accepted but not enforced |
| Claude models with thinking always on | assumed all | only Opus 4.1/4.5/4.6, Sonnet 4.5/4.6 |
| Rate limiting | none seen | 429 seen occasionally, none at 15 concurrent |
| `stream` omitted | not tested | gateway streams SSE |
| Image models | none | 5 models, reached through chat |
| Empty replies | not seen | `Claude-Sonnet-5` / `Claude-Opus-5` can spend the whole output budget thinking |
| Request time cap | not seen | HTTP 524 after 120 s |

---

## Endpoint inventory

| Endpoint | Method | Status |
|---|---|---|
| `/models` | GET | works — returns `{"data": [{"id": ...}]}` |
| `/chat/completions` | POST | works — **send `"stream": false`** |
| `/embeddings` | POST | **403, blocked** |
| `/images/generations` | POST | **403, blocked** (2026-11) |

The blocked endpoints are worth knowing about because the model list actively
misleads you — `protected.text-embedding-3-small` and five image models are
listed as available:

```
POST /openai/embeddings  {"model": "protected.text-embedding-3-small", "input": "hello"}
→ HTTP 403
  {"detail":"Direct API passthrough is disabled. Set ENABLE_OPENAI_API_PASSTHROUGH=True to enable."}

POST /openai/images/generations  {"model": "protected.gpt-image-1-mini", "prompt": "a plain red circle"}
→ HTTP 403  (same body)
```

**There is no embeddings capability on this gateway.** Anything needing vectors —
semantic search, clustering, dedup — has to use a local model (e.g.
`sentence-transformers`) or a different provider. Images *are* available, but
only through `/chat/completions` (see "Image models" below).

### Streaming is the default

With no `stream` field in the body, `/chat/completions` returns
`text/event-stream` even for a one-word reply:

```
data: {"id":"chatcmpl-...","model":"Claude Opus 4.8","object":"chat.completion.chunk",
       "choices":[{"index":0,"delta":{"content":"ok","role":"assistant"}}]}

data: {"id": "protected.Claude Opus 4.8-...", "object": "chat.completion.chunk", ...}
```

`resp.json()` on that raises. Always send `"stream": false`.

Streamed output from thinking models also exposes the reasoning inline — Sonnet
4.6's first chunk carried `"content": "<think>{\""` alongside `thinking_blocks`.
With `stream: false` the reasoning is kept out of `content` (it appears in a
separate `reasoning_content` field).

---

## The error shapes

A `resp.raise_for_status()` / `resp.ok` check catches only some of these.

### 1. HTTP 4xx/5xx with a `detail` body — the usual shape (2026-11)

Upstream rejections, gateway rejections and rate limits all use `detail`, with a
markdown banner and the real code repeated at the end:

```
POST /chat/completions  {"model":"protected.gpt-5.6-sol", ..., "temperature":0}
→ HTTP 400
  {"detail": "🚫 **Invalid Request Error**\n\nlitellm.BadRequestError: AzureException
    BadRequestError - Unsupported value: 'temperature' does not support 0 with this model.
    Only the default (1) value is supported.. Received Model Group=gpt-5.6-sol\n
    Available Model Group Fallbacks=None\n\n*Error Code: 400*"}

POST /chat/completions  {"model":"protected.<model>", ...}
→ HTTP 429
  {"detail":"🚫 **Rate Limit Reached**\n\nYou are sending requests faster than your account
    allows. Please wait a moment and try again.\n\n*Error Code: 429*"}

POST /chat/completions  {"model":"nope", ...}
→ HTTP 403  {"detail":"Model not found"}
```

Unknown models are **403, not 404**, and not retryable — almost always a mistyped
id. 429 and 5xx are retryable. No `Retry-After` or rate-limit headers are sent.

### 2. HTTP 200 with an `error` body (2026-07)

In July, upstream model errors came back with a **success status code**:

```
POST /chat/completions  {"model":"protected.Claude Sonnet 4.6", ..., "temperature":0}
→ HTTP 200
  {"error":{"message":"litellm.BadRequestError: BedrockException - {\"message\":\"The model
    returned the following errors: `temperature` may only be set to 1 when thinking is
    enabled...\"}","type":null,"param":null,"code":"400"}}
```

The same request now returns shape 1. Keep handling this shape anyway — it was
the gateway's behavior once and code that trusts the status code dies with a bare
`KeyError: 'choices'` several frames from the real cause.

### 3. HTTP 200 whose reply text is the error

```
POST /chat/completions  {"model":"protected.gemini-3.1-flash-image",
                         "messages":[{"role":"user","content":"Reply with exactly: ok"}]}
→ HTTP 200, choices[0].message.content =
  "🚫 **Empty Response**\n\nThe image service returned no renderable image."
```

Structurally a perfect success. Any reply that starts with the `🚫 **...**`
banner is a gateway error, and the client raises on it.

### 4. Transport timeouts

Seen once on an otherwise ordinary request that succeeded on retry. Use a generous
timeout (180 s) and retry with backoff.

### 5. HTTP 200 with an empty reply — adaptive thinking ran out of budget

`Claude-Sonnet-5` and `Claude-Opus-5` decide for themselves how much to think.
Their thinking comes back in `thinking_blocks` (a signed block), **not** in
`reasoning_content`. `usage.reasoning_tokens` reports 0 even though the thinking
is billed as output. With no `max_tokens`, the gateway caps output at 4096
tokens.

Prompt: *"Review this module for bugs in under 150 words"* plus a 20 KB Python
file (~5k tokens):

| Model, settings | Time | `finish_reason` | Content chars | Output tokens |
|---|---|---|---|---|
| `Claude-Sonnet-5`, defaults | 44 s | **`length`** | **0** | 4096 |
| `Claude-Sonnet-5`, `max_tokens: 16000` | 63 s | `stop` | 1139 | 3912 |
| `Claude-Sonnet-5`, `reasoning_effort: "low"` | 10 s | `stop` | 1757 | 676 |
| `Claude-Sonnet-5`, `thinking: {"type": "disabled"}` | 11 s | `stop` | 1948 | 744 |
| `Claude-Opus-5`, defaults | 40 s | `stop` | 1195 | 2977 |
| `Claude-Opus-5`, `reasoning_effort: "low"` | 20 s | `stop` | 1133 | 1333 |
| `Claude-Opus-5`, `thinking: {"type": "disabled"}` | 10 s | `stop` | 1334 | 514 |
| `Claude Sonnet 4.6`, defaults | 8 s | `stop` | 1773 | 545 |
| `gpt-5.6-sol`, defaults | 9 s | `stop` | 1166 | 532 |

With a ~13k-token prompt, `Claude-Sonnet-5` at `max_tokens: 4096` and at `12000`
both returned `length` with empty content, and the 12000 run took 123 s. A fixed
budget is rejected:

```
{"thinking": {"type": "enabled", "budget_tokens": 1024}}
→ HTTP 400  ... "thinking.type.enabled" is not supported for this model. Use
  "thinking.type.adaptive" and "output_config.effort" to control thinking behavior.
```

The client sends `max_tokens: 16000` to these two models when the caller sets no
budget or thinking control, and raises on any empty reply.

### 6. HTTP 524 after 120 s

```
→ HTTP 524
  {"detail": "... The origin web server did not return a complete response within the
    120-second Proxy Read Timeout window. ... *Error Code: 524*"}
```

The proxy in front of the gateway enforces a 120 s cap, regardless of the
client's own timeout. Seen with `Claude-Sonnet-5`, a 63 KB prompt and
`max_tokens: 32000`. Retrying the same request won't help.

### Also: no `choices` from reasoning models with small `max_tokens`

```
POST /chat/completions  {"model":"protected.gemini-3.5-flash", ..., "max_tokens":50}
→ HTTP 500
  {"detail": "🚫 **Error**\n\nlitellm.APIError: LiteLLM: provider returned a response with
    no 'choices'. ... Received Model Group=gemini-3.5-flash ... *Error Code: 500*"}
```

Reproduced twice; a third attempt returned HTTP 200 with `"choices": []`. With
`max_tokens: 2000` the same call succeeds and `usage` shows why:
`"reasoning_tokens": 67, "text_tokens": 1`. The budget goes to thinking first. This
500 is deterministic, so the client does not retry it.

---

## Parameter support by model

Verified 2026-11-17 by sending `"Reply with exactly: ok"` to every listed text
model with each parameter in turn (`temperature: 0`, `top_p: 0.9`,
`max_tokens: 300`) and, for Claude models, `temperature` + `top_p` together.

| Models | `temperature: 0` | `top_p: 0.9` | `max_tokens: 300` | both temp + top_p |
|---|---|---|---|---|
| `Claude Opus 4.1 / 4.5 / 4.6`, `Claude Sonnet 4.5 / 4.6` | **400** | **400** | **400** | — |
| `Claude Opus 4.7 / 4.8`, `Claude-Opus-5`, `Claude-Sonnet-5` | OK | OK | OK | OK |
| `Claude-Haiku-4.5` | OK | OK | OK | **400** |
| `gpt-5.5`, `gpt-5.6-luna / sol / terra` | **400** | **400** | OK | — |
| all other GPT, o-series, Gemini, `llama3.2` | OK | OK | OK | — |

Error texts:

- Thinking Claude: "`temperature` may only be set to 1 when thinking is enabled",
  "`top_p` must be greater than or equal to 0.95 or unset", "`max_tokens` must be
  greater than `thinking.budget_tokens`" (300 is still too small).
- gpt-5.5 / gpt-5.6: "Unsupported value: 'temperature' does not support 0 with this
  model. Only the default (1) value is supported", "Unsupported parameter: 'top_p'
  is not supported with this model".
- Haiku 4.5: "`temperature` and `top_p` cannot both be specified for this model.
  Please use only one."

`temperature: 1` is accepted everywhere.

The thinking-on Claude models return `reasoning_content` and `thinking_blocks` in
the message, and `usage.completion_tokens_details.reasoning_tokens` > 0.

### `response_format` on Claude

2026-07, `protected.Claude Sonnet 4.6`: **HTTP 200, content is `"{}"`** — an empty
object that parses fine and quietly poisons whatever comes next.

2026-11, asked *"Give a JSON object with keys name and age for a fictional
person."* with `response_format: {"type":"json_object"}`:

| Model | Content |
|---|---|
| `Claude Sonnet 4.6`, `Claude Opus 4.8`, `Claude-Sonnet-5`, `Claude-Opus-5`, `Claude-Haiku-4.5` | ` ```json\n{\n  "name": "...",\n  "age": 34\n}\n``` ` |
| `gpt-5.6-sol`, `gemini-3.5-flash` | bare `{"name": "...", "age": ...}` |

So the parameter is accepted and ignored on every Claude model. It is no longer
destructive, but it still does not guarantee JSON.

**Working approach on Claude:** state the JSON contract in the prompt text and
parse defensively. This is what the quiz grader does and it produced 457/457 valid
responses:

```
Respond with ONLY a JSON object, no prose and no code fences:
{"score": <number 0-15>, "comment": "<max 12 words>"}
```

---

## Output formatting is inconsistent — parse defensively

Asked for `{"ok":true}` with no `response_format`, across separate runs:

| Model | Observed content |
|---|---|
| `protected.Claude Sonnet 4.6` | ` ```json\n{"ok":true}\n``` ` **and**, on a later identical call, bare `{"ok": true}` |
| `protected.Claude-Haiku-4.5` | ` ```json\n{"ok": true}\n``` ` |
| `protected.gpt-5.4-mini`, `gpt-5.6-*`, `Claude-Sonnet-5`, `Claude-Opus-5` | bare `{"ok": true}` |

Claude **sometimes** fences its JSON and sometimes does not — the same prompt
produced both. Never assume either form. `extract_json()` in
`scripts/tamu_ai.py` strips fences and then decodes the first complete JSON value,
so prose around the object (including stray `{x}`-style braces) does not break it.

---

## Image models

Five models, all reached through `/chat/completions`. The reply has no `usage`
block and its content is a single markdown image with a base64 data URI:

```
choices[0].message.content = "![image](data:image/png;base64,iVBORw0KGgo...)"
```

Prompt: *"Generate an image of a plain red circle on a white background."* — every
model drew it correctly.

| Model | Time | Bytes | Actual format |
|---|---|---|---|
| `gemini-3.1-flash-lite-image` | 2.9 s | 32 KB | **JPEG**, declared `image/png` |
| `gemini-3.1-flash-image` | 9.5 s | 1.2 MB | PNG 1024×1024 |
| `gpt-image-2` | 15.2 s | 0.3 MB | PNG 1024×1024 |
| `gpt-image-1.5` | 24.4 s | 0.2 MB | PNG 1024×1024 |
| `gpt-image-1-mini` | 29.8–49.6 s | 1.8 MB | PNG 1024×1024 |

Given a non-image prompt (*"Reply with exactly: ok"*), the two Gemini image models
return the `🚫 **Empty Response**` banner (error shape 3); `gpt-image-1-mini` drew
an image anyway.

---

## Response metadata

The `usage` block is present on text models and includes cache accounting and,
on reasoning models, reasoning tokens:

```json
{"completion_tokens": 38, "prompt_tokens": 50, "total_tokens": 88,
 "completion_tokens_details": {"reasoning_tokens": 3, "text_tokens": 35},
 "prompt_tokens_details": {"cached_tokens": 0, "text_tokens": 50, "cache_write_tokens": 0,
                           "cache_creation_tokens": 0},
 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
```

---

## Throughput

From the 457-call quiz-grading run (2026-07):

| Measure | Value |
|---|---|
| Sustained rate, 10 workers | 2.7–3.4 req/s |
| Full 457-call job | 175 s, 0 failures |
| `protected.Claude Sonnet 4.6` latency | ~1.6–2.5 s per short call |
| `protected.gpt-5.4-mini` latency | ~0.9–1.0 s per short call |
| Rate limiting observed | none at 10 concurrent |

2026-11: 30 calls at 15 concurrent to `gpt-4.1-nano` all succeeded; a 36-model
sweep at 8 concurrent saw an occasional 429. 8 workers is a safe starting point. No documented quota
was discoverable, so don't assume unlimited — cache results so a re-run is free.
