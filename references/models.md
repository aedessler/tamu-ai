# TAMU AI — model roster and selection

Verified **2026-11-17** (previously 2026-07-23). The roster changes; always
confirm with `list_models()` or `python3 scripts/tamu_ai.py --list-models`, and
use `--check-all` to see which listed text models actually answer.

## Two gotchas about model ids

1. Every id carries a **`protected.` prefix** — `protected.gpt-5.4-mini`, not
   `gpt-5.4-mini`.
2. Ids are **not normalized**. Older Claude ids contain spaces and capitals
   (`"protected.Claude Sonnet 4.6"` — must be quoted); newer ones use hyphens
   (`protected.Claude-Sonnet-5`, `protected.Claude-Opus-5`,
   `protected.Claude-Haiku-4.5`). `protected.claude-sonnet-4.6` is a 403.

Copy ids from `list_models()`; don't guess.

## Available models

Column notes: **thinking** = extended thinking permanently on (see parameter
limits in [quirks.md](quirks.md)); **adaptive thinking** = the model decides how
long to think, and the thinking counts against `max_tokens` (the client defaults
it to 16000; pass `reasoning_effort="low"` for fast bulk work); **fixed
sampling** = `temperature` must be 1 and `top_p` is unsupported.

**Claude** (Bedrock)

```
protected.Claude-Opus-5        new 2026-11; all params work; adaptive thinking
protected.Claude-Sonnet-5      new 2026-11; all params work; adaptive thinking
protected.Claude Opus 4.8      all params work
protected.Claude Opus 4.7      all params work
protected.Claude Opus 4.6      thinking
protected.Claude Opus 4.5      thinking
protected.Claude Opus 4.1      thinking
protected.Claude Sonnet 4.6    thinking
protected.Claude Sonnet 4.5    thinking
protected.Claude-Haiku-4.5     temperature OR top_p, not both
protected.Claude Sonnet 4      LISTED BUT UNUSABLE — 404 NotFound / 429 on every call
```

`protected.Claude 3.5 Haiku` was removed from the roster after 2026-07.

**GPT** (Azure)

```
protected.gpt-5.6-luna   protected.gpt-5.6-sol   protected.gpt-5.6-terra   new 2026-11; fixed sampling
protected.gpt-5.5        fixed sampling
protected.gpt-5.4        protected.gpt-5.4-mini  protected.gpt-5.4-nano
protected.gpt-5.2        protected.gpt-5.1
protected.gpt-5          protected.gpt-5-mini    protected.gpt-5-nano
protected.gpt-4.1        protected.gpt-4.1-mini  protected.gpt-4.1-nano    protected.gpt-4o
```

**Reasoning (o-series)**

```
protected.o3    protected.o3-mini    protected.o4-mini
```

**Gemini** (Vertex)

```
protected.gemini-3.5-flash        reasons; small max_tokens -> HTTP 500
protected.gemini-3.1-flash-lite
protected.gemini-2.5-pro          reasons
protected.gemini-2.5-flash        reasons
protected.gemini-2.5-flash-lite
```

**Image generation** (through `/chat/completions`; see SKILL.md "Images")

```
protected.gemini-3.1-flash-image        ~10 s, PNG 1024x1024           new 2026-11
protected.gemini-3.1-flash-lite-image   ~3 s, JPEG labelled as PNG     new 2026-11
protected.gpt-image-2                   ~15 s, PNG 1024x1024           new 2026-11
protected.gpt-image-1.5                 ~25 s, PNG 1024x1024           new 2026-11
protected.gpt-image-1-mini              ~30-50 s, PNG 1024x1024        new 2026-11
```

**Other**

```
protected.llama3.2
protected.text-embedding-3-small   ← LISTED BUT UNUSABLE, the endpoint is 403
```

## Choosing

| Job shape | Reach for | Why |
|---|---|---|
| Bulk mechanical work — classify, extract, tag thousands of rows | `protected.gpt-5.4-mini`, `protected.gemini-3.1-flash-lite`, `protected.Claude-Haiku-4.5` | ~1 s/call, accept standard parameters |
| Judgment — grading to a rubric, nuanced classification, anything where consistency with a human matters | `protected.Claude-Sonnet-5`, `protected.Claude-Opus-5` | Newest Claude models, and unlike Sonnet 4.6 they accept every sampling parameter. They think adaptively, so a hard item can take ~60 s; use `reasoning_effort="low"` for bulk runs. Rubric agreement was measured on Sonnet 4.6 (r = 0.86–0.95 vs. a human grader across 457 answers), so re-check on a sample before switching a validated pipeline |
| Hard reasoning, math, multi-step planning | `protected.gpt-5.6-sol`, `protected.gpt-5.5`, `protected.o3` | Built for it |
| **Guaranteed structured JSON** | any **GPT** or **Gemini** model | `response_format` works there and is ignored on Claude |
| Long context dumps | `protected.gemini-2.5-pro`, Claude models | Large windows |
| Images | `protected.gemini-3.1-flash-image` (default), `protected.gpt-image-2` | Fastest good-quality PNGs measured; `-lite-image` is fastest but returns JPEG |

The three `gpt-5.6` variants (`luna`, `sol`, `terra`) behaved identically on
every probe — same parameters, ~1–2 s latency, correct answers. Nothing from the
API distinguishes them; compare them on your own task before choosing.

Measured latency on short calls (2026-11-17): most text models answer in
0.3–2 s; `protected.Claude-Haiku-4.5` once took 13.8 s on a trivial prompt.

## Cost

No pricing or quota information is exposed through the API, and the docs are
403-gated. Access is free to TAMU affiliates with a NetID, but treat quota as
unknown rather than unlimited: cache results so a re-run is free, and test a
prompt on 8–10 items before launching hundreds.

## A note on picking by name

Model quality does not track version numbers across families, and the roster gets
new entries regularly. When a job matters, run 8–10 items through two candidates
and compare the actual output — the quiz-grading work did exactly this
(Sonnet 4.6 vs. gpt-5.4-mini) before committing to a 457-call run.
