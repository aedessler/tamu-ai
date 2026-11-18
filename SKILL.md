---
name: tamu-ai
description: Use the Texas A&M University AI system (TAMU AI, chat.tamu.ai, chat-api.tamu.ai) as the LLM backend for a task. Trigger whenever the user says "use TAMU AI", "TAMU AI", "the TAMU API", "TAMU GPT/LLM", "Texas A&M AI", or names chat.tamu.ai / tamu.ai as where a request should go — and for all follow-on work in a session where TAMU AI is already the chosen backend. Covers single calls, large parallel jobs (grading, classifying, summarizing, extracting across many rows), and image generation. The gateway is OpenAI-compatible on the surface but streams unless told not to, reports errors in several shapes, ignores response_format on Claude models, and rejects sampling parameters per model, so use this skill rather than writing a plain OpenAI snippet. This skill never contains an API key — it resolves one from the environment or a user-owned config file, and asks the user if neither exists.
---

# TAMU AI

Texas A&M's LLM gateway. OpenAI-compatible at
`https://chat-api.tamu.ai/openai`, fronting Claude, GPT, Gemini, o-series,
Llama and image-generation models. Free to TAMU affiliates with a NetID.

**It is not a drop-in OpenAI endpoint.** It streams by default, fails in several
different shapes (one of them an HTTP 200 whose *reply text* is the error), and
which sampling parameters work depends on the exact model. Read "The traps"
below before writing any call by hand.

## 1. Get the API key — never hardcode it

**The key must never be written into a script, a project file, this skill, or a
shell profile.** Resolve it in this order:

1. `$TAMU_AI_API_KEY` in the environment
2. `~/.config/tamu-ai/api-key`
3. **Ask the user for it**, then use it for the session

Check the first two before asking — the user may already have it set up:

```bash
[ -n "$TAMU_AI_API_KEY" ] && echo "env: set" || echo "env: unset"; [ -f ~/.config/tamu-ai/api-key ] && echo "file: present" || echo "file: absent"
```

If neither exists, ask the user: *"What's your TAMU AI API key? Get one from
chat.tamu.ai → Settings → API Key."*

Once you have a key that works, **offer** to save it so future sessions don't ask
again — and only write it after they say yes:

```bash
mkdir -p ~/.config/tamu-ai && printf %s 'sk-REPLACE' > ~/.config/tamu-ai/api-key && chmod 600 ~/.config/tamu-ai/api-key
```

Never save it unprompted. If the user declines, pass it inline per command
(`TAMU_AI_API_KEY="sk-..." python3 script.py`) and write nothing.

**Pass the key as an environment variable, never as a command-line flag** — argv
is visible to other processes via `ps`. Never echo it, log it, or include it in a
file you write.

## 2. Use the client

`scripts/tamu_ai.py` already encodes everything below. Prefer it over hand-rolled
requests.

```bash
python3 ~/.claude/skills/tamu-ai/scripts/tamu_ai.py --check          # verify key + round-trip
python3 ~/.claude/skills/tamu-ai/scripts/tamu_ai.py --check-all      # round-trip every text model
python3 ~/.claude/skills/tamu-ai/scripts/tamu_ai.py --list-models
python3 ~/.claude/skills/tamu-ai/scripts/tamu_ai.py --chat "hello" -m protected.Claude-Sonnet-5
python3 ~/.claude/skills/tamu-ai/scripts/tamu_ai.py --image "a red circle" -o circle.png
```

```python
import os, sys
sys.path.insert(0, os.path.expanduser("~/.claude/skills/tamu-ai/scripts"))
from tamu_ai import chat, list_models, run_batch, extract_json, generate_image, FAST_MODEL

chat("summarize this in one line", model=FAST_MODEL)
```

(`sys.path` does not expand `~` on its own — `expanduser` is required. If the
skill lives elsewhere, point that path at wherever `tamu_ai.py` actually is.)

## 3. Pick a model deliberately

There is **no default** — the right choice depends on the job. Every model id
carries a `protected.` prefix, and the naming is not normalized: some ids use
spaces (`"protected.Claude Sonnet 4.6"`), newer ones use hyphens
(`protected.Claude-Sonnet-5`).

| Job shape | Reach for |
|---|---|
| Bulk, mechanical, high volume | `protected.gpt-5.4-mini`, `protected.gemini-3.1-flash-lite`, `protected.Claude-Haiku-4.5` |
| Judgment: grading, rubrics, nuanced classification | `protected.Claude-Sonnet-5`, `protected.Claude-Opus-5` (they think adaptively; use `reasoning_effort="low"` for bulk runs; see the traps) |
| Hard reasoning, math, multi-step | `protected.gpt-5.6-sol`, `protected.gpt-5.5`, `protected.o3` |
| Structured JSON output | a **GPT** or **Gemini** model — `response_format` works there and is ignored on Claude |
| Images | `protected.gemini-3.1-flash-image`, `protected.gpt-image-2` — see "Images" |

The roster changes; confirm with `list_models()` (or `--check-all`, which also
shows which listed models actually answer) rather than trusting this table.
Full list and tradeoffs: [references/models.md](references/models.md).

## The traps

Read these even if you are writing your own request code.

**Always send `"stream": false`.** If `stream` is omitted the gateway answers
with a `text/event-stream` of `data: {...}` chunks, so `resp.json()` fails. The
client always sends it.

**Only `GET /models` and `POST /chat/completions` work.** `/embeddings` and
`/images/generations` both return 403 *"Direct API passthrough is disabled"*,
even though embedding and image models appear in the model list. There is no
embeddings capability here — don't design around one. Images are reached through
chat (see below).

**Errors arrive in five shapes. Checking the status code is not enough.**

1. HTTP 4xx/5xx with `{"detail": "🚫 **Error**\n\n...*Error Code: 400*"}` — the
   usual shape now. 429 (rate limit) and 5xx are worth retrying; 400/403/404
   are not.
2. HTTP 200 with `{"error": {...}}` in the body — how upstream errors arrived in
   2026-07. Code that only checks `resp.ok` sails into a `KeyError: 'choices'`.
3. HTTP 200 whose **reply text** is a gateway banner such as
   `"🚫 **Empty Response**"` — an image model given a prompt it can't draw.
4. Transport timeouts, occasionally on an otherwise fine request. Retry these.
5. HTTP 200 with **empty** reply text. A thinking model used its whole output
   budget (see below).

The client handles all five. It retries the retryable ones with backoff, and
everything else raises `TamuAIError` with the gateway's message.

**Sampling parameters are per model, not per family:**

| Models | Rejected |
|---|---|
| `Claude Opus 4.1 / 4.5 / 4.6`, `Claude Sonnet 4.5 / 4.6` (thinking always on) | `temperature` ≠ 1, `top_p` < 0.95, `max_tokens` / `max_completion_tokens` |
| `gpt-5.5`, `gpt-5.6-luna / sol / terra` | `temperature` ≠ 1, `top_p` |
| `Claude-Haiku-4.5` | `temperature` and `top_p` **together** (either alone is fine) |
| `Claude Opus 4.7 / 4.8`, `Claude-Opus-5`, `Claude-Sonnet-5`, other GPT, Gemini, o-series, Llama | nothing |

The client drops rejected parameters with a warning. Simplest rule: **don't send
sampling parameters unless you need them.**

**`response_format` is ignored by every Claude model.** The gateway accepts it
and the model replies in ordinary text — usually ` ```json `-fenced — so you do
not get guaranteed JSON. (In 2026-07 it was worse: a bare `{}`.) The client
refuses `response_format` on Claude models rather than let you believe it
worked. To get JSON from Claude, ask for it in the prompt and parse with
`extract_json()`, which handles fences, surrounding prose, and stray braces.

**`Claude-Sonnet-5` and `Claude-Opus-5` can think until the reply is empty.**
They decide for themselves how long to think, and the thinking counts against
the output budget. When no `max_tokens` is sent, the gateway's own limit is 4096
tokens. On a long or hard prompt the model can spend all of it thinking. You
then get HTTP 200, `finish_reason: "length"`, and **empty content**. The client
handles this in two ways:

- When you set no budget, it sends `max_tokens: 16000`.
- It raises on any empty reply instead of returning `""`.

To control it yourself:

| Want | Pass |
|---|---|
| Fast replies, bulk jobs | `reasoning_effort="low"` or `thinking={"type": "disabled"}` (~10 s on a 20 KB prompt) |
| Deep answers | leave it to the client's 16000 default, or set `max_tokens` higher (~60 s) |

A fixed budget (`thinking={"type": "enabled", "budget_tokens": N}`) is rejected:
*"Use thinking.type.adaptive and output_config.effort"*.

**Requests are cut off at 120 s.** The gateway's proxy returns HTTP 524 for any
request that runs longer, however large your own timeout. A long prompt sent to
a thinking model can hit this. Shorten the input or output, or limit thinking.
Retrying the same request hits the same limit, so the client doesn't retry.

**Small `max_tokens` breaks reasoning models.** `gemini-3.5-flash` with
`max_tokens: 50` spends the budget thinking and returns HTTP 500 *"provider
returned a response with no 'choices'"* (or, once, an empty `choices` list).
Leave `max_tokens` unset or generous on any model that reasons.

## Images

Image models are called through `/chat/completions` and reply with markdown:
`![image](data:image/png;base64,...)`. Use `generate_image()`, which decodes it:

```python
fmt, data, path = generate_image("a watercolor of Kyle Field", out_path="field.png")
```

- The declared type can be wrong: `gemini-3.1-flash-lite-image` labels JPEG bytes
  `image/png`. `generate_image` sniffs the real format and fixes the file suffix
  (`field.png` → `field.jpg`), so always use the returned `path`.
- Give image models image prompts. A Gemini image model asked a text question
  returns the `"🚫 **Empty Response**"` banner (raised as `TamuAIError`); the
  gpt-image models draw *something* regardless.
- Images are slow (3–50 s) and large (30 KB–2 MB). Don't push them through
  `run_batch` with a cache file — the cache stores every image as base64 JSON.

## Bulk jobs

For anything over ~20 calls use `run_batch`, which does parallelism, retry with
backoff, a disk cache, and resume:

```python
results, failures = run_batch(
    rows,                                   # any sequence
    build_messages=lambda r: f"Classify: {r['text']}",
    model=FAST_MODEL,
    workers=8,
    cache_path="/path/to/.cache.json",      # re-runs are free for finished items
    prompt_version=1,                       # bump to invalidate after prompt edits
    parse=extract_json,                     # optional
)
```

Two properties that matter on long runs:

- **Failures are returned, not raised.** One bad row cannot throw away 400
  finished ones. Always check `failures` and report them — never let a failed
  call silently become a zero or an empty string in your output.
- **The cache makes re-running cheap**, and finished work is saved even when the
  run is interrupted (Ctrl-C), so an interrupted job just gets run again. Bump
  `prompt_version` when you change a prompt, or you will serve stale results.

~2.7–3.4 requests/sec at 8–10 workers. A general rate limit was not hit at 15
concurrent requests, but the gateway does return 429 *"Rate Limit Reached"* (seen
occasionally), and the client retries it. Start at 8.

**Shape of a real job this was built from:** grading 457 short answers from a
Canvas quiz export — one call per answer carrying that question's rubric, run at
8–10 workers, cached to disk, completed in 175 s with zero failures. The pattern
generalizes: one small single-purpose call per row beats one giant call over the
whole file, both for accuracy and because a failure then costs one row instead of
the batch.

## Reference

- [references/quirks.md](references/quirks.md) — full probe evidence with real
  response bodies. The official docs at `docs.tamus.ai` are 403-gated, so this is
  the record.
- [references/models.md](references/models.md) — roster and selection guidance.
