# tamu-ai

A [Claude Code](https://claude.com/claude-code) skill for using **Texas A&M's AI
gateway** (`chat-api.tamu.ai`) as an LLM backend — for one-off calls, for large
parallel jobs like grading, classifying, or extracting across thousands of rows,
and for image generation.

Free to TAMU affiliates with a NetID. Fronts Claude, GPT, Gemini, o-series, Llama
and image-generation models behind an OpenAI-compatible API.

## Why this exists

The gateway looks like the OpenAI API but diverges in ways that are easy to get
wrong and slow to diagnose. The official docs at `docs.tamus.ai` are 403-gated to
non-browser clients, so everything here was established by probing the live API:

- **It streams unless told not to.** Omit `"stream": false` and a one-word reply
  arrives as `data: {...}` SSE chunks, so `resp.json()` fails.
- **Errors arrive in several shapes** — `detail` bodies on 4xx/5xx, `error`
  bodies on HTTP 200 (how it behaved in mid-2026), and HTTP 200 replies whose
  *text* is the error (`"🚫 **Empty Response**"`).
- **`response_format: json_object` is ignored on every Claude model.** It is
  accepted, and the reply is ordinary (often ` ```json `-fenced) text. In mid-2026
  it was worse: a bare `{}`.
- **Sampling parameters are rejected per model, not per family.** Older
  Sonnet/Opus models run with extended thinking always on and reject
  `temperature`, `top_p` and `max_tokens`; `gpt-5.5`/`gpt-5.6` reject
  `temperature` and `top_p`; Haiku 4.5 rejects the two together; Opus 4.7+,
  Opus 5 and Sonnet 5 accept everything.
- **`/embeddings` and `/images/generations` return 403** even though embedding
  and image models are advertised. Images work through chat instead.
- **JSON output is inconsistently fenced** — the same prompt returned
  ` ```json `-wrapped output on one call and bare JSON on the next.

The skill encodes all of this so you don't rediscover it.

## Install

```bash
git clone https://github.com/aedessler/tamu-ai.git ~/.claude/skills/tamu-ai
```

Requires Python 3.9+ and `requests` (`pip3 install requests`).

That's it — Claude Code picks up skills in `~/.claude/skills/` automatically.
Then just say **"use TAMU AI"** in any session.

To install for a single project instead, clone into `.claude/skills/tamu-ai/`
inside that project.

## API key

**No key is stored in this repo.** The client resolves one from, in order:

1. `$TAMU_AI_API_KEY`
2. `~/.config/tamu-ai/api-key`
3. Otherwise it fails with setup instructions — and Claude will ask you for one.

Get a key from [chat.tamu.ai](https://chat.tamu.ai) → Settings → API Key. To set
it up once so future sessions don't ask:

```bash
mkdir -p ~/.config/tamu-ai
printf %s 'sk-YOUR-KEY' > ~/.config/tamu-ai/api-key
chmod 600 ~/.config/tamu-ai/api-key
```

Or per-shell: `export TAMU_AI_API_KEY='sk-YOUR-KEY'`

Pass the key as an environment variable, never as a command-line flag — argv is
readable by other processes via `ps`.

## Verify

```bash
python3 ~/.claude/skills/tamu-ai/scripts/tamu_ai.py --check
```

```
Key OK. 42 models available.
Round-trip on protected.gpt-5.4-mini:
  ok
```

To see which listed text models actually answer (the list includes some that
don't), run `--check-all`.

## Use it directly

The client works standalone, without Claude Code:

```bash
python3 scripts/tamu_ai.py --list-models
python3 scripts/tamu_ai.py --chat "explain lapse rate in one sentence"
python3 scripts/tamu_ai.py --chat "hello" -m protected.Claude-Sonnet-5
python3 scripts/tamu_ai.py --image "a red circle on white" -o circle.png
```

```python
import os, sys
sys.path.insert(0, os.path.expanduser("~/.claude/skills/tamu-ai/scripts"))
from tamu_ai import chat, run_batch, extract_json, generate_image, FAST_MODEL

chat("summarize this in one line", model=FAST_MODEL)

# Bulk: parallel, retried, cached, resumable
results, failures = run_batch(
    rows,
    build_messages=lambda r: f"Classify this: {r}",
    model=FAST_MODEL,
    workers=8,
    cache_path=".cache.json",   # re-runs are free for finished items
    parse=extract_json,         # optional
)

# Images come back through chat as base64; this decodes and saves one
fmt, data, path = generate_image("a red circle on white", out_path="circle.png")
```

`run_batch` **returns** failures rather than raising, so one bad row can't discard
a long run, and the disk cache (saved even on Ctrl-C) makes an interrupted job
cheap to restart. Measured
at ~2.7–3.4 requests/sec with 8–10 workers.

## Contents

| Path | What |
|---|---|
| `SKILL.md` | Trigger rules, key handling, model selection, the traps |
| `scripts/tamu_ai.py` | Client, parallel batch runner, image helper. Importable module and CLI. |
| `references/quirks.md` | Full probe evidence with real response bodies |
| `references/models.md` | Model roster and selection guidance |

## Caveats

Model ids carry a `protected.` prefix and are not normalized — older Claude ids
contain spaces and capitals (`"protected.Claude Sonnet 4.6"`), while newer ones
use hyphens (`protected.Claude-Sonnet-5`, `protected.Claude-Haiku-4.5`). Copy ids from
`--list-models` rather than guessing; a wrong id returns `403 Model not found`.

The roster and the gateway's behavior both change. `references/` records what was
true on **2026-07-23** and **2026-11-17**; re-probe if something looks off.

## License

MIT
