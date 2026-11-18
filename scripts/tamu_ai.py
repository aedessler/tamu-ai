#!/usr/bin/env python3
"""
Client for the TAMU AI gateway (https://chat-api.tamu.ai/openai).

OpenAI-compatible on the surface, but with enough gateway-specific behavior that
a plain OpenAI snippet will fail in confusing ways. This module encodes what was
established by probing the live API -- see ../references/quirks.md.

Use as a module (sys.path does not expand "~", hence expanduser):

    import os, sys
    sys.path.insert(0, os.path.expanduser("~/.claude/skills/tamu-ai/scripts"))
    from tamu_ai import chat, list_models, run_batch, extract_json, generate_image

    print(chat([{"role": "user", "content": "hi"}], model="protected.gpt-5.4-mini"))

Or as a CLI:

    python3 tamu_ai.py --check
    python3 tamu_ai.py --check-all
    python3 tamu_ai.py --list-models
    python3 tamu_ai.py --chat "explain lapse rate in one sentence"
    python3 tamu_ai.py --image "a red circle" -o circle.png

The API key is NEVER stored in this file. It is resolved from, in order:
  1. $TAMU_AI_API_KEY
  2. ~/.config/tamu-ai/api-key
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

BASE_URL = "https://chat-api.tamu.ai/openai"
KEY_FILE = Path.home() / ".config" / "tamu-ai" / "api-key"

# No default model on purpose: pick per task. See ../references/models.md.
# These are well-characterized points on the speed/judgment tradeoff.
FAST_MODEL = "protected.gpt-5.4-mini"         # ~1.0 s/call, all standard params work
JUDGMENT_MODEL = "protected.Claude-Sonnet-5"  # ~1.5-2 s/call, all standard params work
IMAGE_MODEL = "protected.gemini-3.1-flash-image"  # ~10 s/image, PNG

DEFAULT_TIMEOUT = 180
DEFAULT_WORKERS = 8


class TamuAIError(Exception):
    """Any TAMU AI failure. `.retryable` says whether trying again could help."""

    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Key resolution -- never writes, never echoes
# ---------------------------------------------------------------------------

def resolve_key() -> str:
    """Find the API key. Raises with instructions rather than prompting."""
    key = os.environ.get("TAMU_AI_API_KEY", "").strip()
    if key:
        return key
    if KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key
    raise TamuAIError(
        "No TAMU AI API key found.\n"
        "  Set it for this shell:   export TAMU_AI_API_KEY='sk-...'\n"
        f"  Or save it once:         mkdir -p {KEY_FILE.parent} && "
        f"printf %s 'sk-...' > {KEY_FILE} && chmod 600 {KEY_FILE}\n"
        "  Get a key from chat.tamu.ai -> Settings -> API Key."
    )


def _headers(api_key=None) -> dict:
    return {
        "accept": "application/json",
        "Authorization": f"Bearer {api_key or resolve_key()}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Per-model parameter guards
#
# These sets are what the live gateway did on 2026-11-17, model by model -- NOT
# a family rule. "Claude" does not imply thinking: Opus 4.7/4.8, Opus 5,
# Sonnet 5 and Haiku 4.5 accept every sampling parameter. Unknown models get no
# guarding; the gateway now returns a clear HTTP 400 for a rejected parameter.
# ---------------------------------------------------------------------------

# Bedrock runs these with extended thinking permanently enabled.
THINKING_LOCKED_MODELS = frozenset({
    "protected.Claude Opus 4.1",
    "protected.Claude Opus 4.5",
    "protected.Claude Opus 4.6",
    "protected.Claude Sonnet 4.5",
    "protected.Claude Sonnet 4.6",
})

# Azure reasoning deployments: temperature is fixed at 1, top_p is unsupported.
FIXED_SAMPLING_MODELS = frozenset({
    "protected.gpt-5.5",
    "protected.gpt-5.6-luna",
    "protected.gpt-5.6-sol",
    "protected.gpt-5.6-terra",
})

# Adaptive thinking: the model decides how long to think, and that thinking
# counts against the output budget. The gateway's implicit budget is 4096
# tokens, which a hard prompt can spend entirely on thinking -- the reply is
# then HTTP 200 with EMPTY content and finish_reason "length". When the caller
# sets no budget and no thinking control, the client sends a larger one.
ADAPTIVE_THINKING_MODELS = frozenset({
    "protected.Claude-Opus-5",
    "protected.Claude-Sonnet-5",
})
ADAPTIVE_DEFAULT_MAX_TOKENS = 16000
_THINKING_CONTROLS = ("max_tokens", "max_completion_tokens", "reasoning_effort", "thinking")

# Each of temperature / top_p works alone; Bedrock rejects the pair together.
ONE_SAMPLER_MODELS = frozenset({
    "protected.Claude-Haiku-4.5",
})

_THINKING_BANNED = {
    "temperature": "may only be 1 when thinking is enabled",
    "top_p": "must be >= 0.95 or unset",
    "max_tokens": "must exceed the thinking budget",
    "max_completion_tokens": "must exceed the thinking budget",
}
_FIXED_SAMPLING_BANNED = {
    "temperature": "only the default (1) is supported",
    "top_p": "not supported by this model",
}


def is_thinking_model(model: str) -> bool:
    """True for models that run with extended thinking always enabled."""
    return model in THINKING_LOCKED_MODELS


def is_claude_model(model: str) -> bool:
    return "claude" in model.lower()


def is_image_model(model: str) -> bool:
    """Image-generation models. They answer chat calls with a markdown image."""
    return "image" in model.lower()


def _guard_params(model: str, params: dict, quiet: bool = False) -> dict:
    """Strip params a model rejects; raise on ones that are silently ignored."""
    # response_format is accepted by every Claude model and then NOT enforced:
    # replies come back as ```json-fenced markdown (in July 2026 Sonnet 4.6
    # returned a bare "{}"). Dropping it quietly would leave the caller
    # believing they have guaranteed JSON. Refuse loudly instead.
    if is_claude_model(model) and "response_format" in params:
        raise TamuAIError(
            f"response_format is not enforced on {model}: the gateway accepts "
            "it and the model still replies in free-form (often ```json-fenced) "
            "text.\n"
            "  Fix: ask for JSON in the prompt text instead, and parse the reply "
            "with extract_json() (it tolerates ```json fences).\n"
            f"  Or use a GPT model ({FAST_MODEL}), where response_format works."
        )

    if model in ADAPTIVE_THINKING_MODELS and not any(k in params for k in _THINKING_CONTROLS):
        return {**params, "max_tokens": ADAPTIVE_DEFAULT_MAX_TOKENS}

    if model in ONE_SAMPLER_MODELS and "temperature" in params and "top_p" in params:
        if not quiet:
            print(f"  [tamu_ai] dropped 'top_p' for {model} (temperature and "
                  "top_p cannot both be set; keeping temperature)", file=sys.stderr)
        return {k: v for k, v in params.items() if k != "top_p"}

    if model in THINKING_LOCKED_MODELS:
        banned = _THINKING_BANNED
    elif model in FIXED_SAMPLING_MODELS:
        banned = _FIXED_SAMPLING_BANNED
    else:
        return params

    cleaned = dict(params)
    for name, why in banned.items():
        if name in cleaned:
            if name == "temperature" and cleaned[name] == 1:
                continue  # temperature=1 is the one allowed value
            if (banned is _THINKING_BANNED and name == "top_p"
                    and isinstance(cleaned[name], (int, float)) and cleaned[name] >= 0.95):
                continue  # thinking models accept top_p >= 0.95
            cleaned.pop(name)
            if not quiet:
                print(f"  [tamu_ai] dropped {name!r} for {model} ({why})",
                      file=sys.stderr)
    return cleaned


# ---------------------------------------------------------------------------
# Response checking -- four distinct failure shapes
# ---------------------------------------------------------------------------

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_ERROR_CODE_RE = re.compile(r"Error Code:\s*(\d{3})")
# Gateway-authored messages: "🚫 **Rate Limit Reached**\n\n..."
_GATEWAY_BANNER_RE = re.compile(r"^\s*\U0001F6AB\s*\*\*(.+?)\*\*\s*", re.DOTALL)


def _clean_gateway_text(text: str) -> str:
    """'🚫 **Error**\\n\\nlitellm...' -> 'Error: litellm...' on one line."""
    text = _GATEWAY_BANNER_RE.sub(lambda m: m.group(1) + ": ", str(text), count=1)
    return re.sub(r"\s+", " ", text).strip()


def _check_response(resp) -> dict:
    """Validate a gateway response and return its parsed body.

    The gateway fails in several ways and only some look like errors:
      1. HTTP 4xx/5xx with {"detail": "..."}  <- the usual shape (2026-11)
      2. HTTP 200 with {"error": {...}}        <- upstream errors (2026-07)
      3. HTTP 200 whose reply TEXT is a gateway error, e.g.
         "🚫 **Empty Response**"                 <- caught in chat()
      4. transport timeouts                     <- handled by the retry loop
      5. HTTP 200 with an empty reply            <- caught in chat()
    """
    try:
        payload = resp.json() if resp.content else {}
    except ValueError:
        payload = {}

    if isinstance(payload, dict) and payload.get("error"):
        err = payload["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        # Upstream 429/5xx surfaced inside a 200 body is still worth retrying.
        retryable = any(s in msg.lower() for s in ("rate limit", "429", "timeout",
                                                   "overloaded", "503", "502"))
        raise TamuAIError(f"gateway error: {msg}", retryable=retryable)

    if isinstance(payload, dict) and payload.get("detail"):
        detail = str(payload["detail"])
        match = _ERROR_CODE_RE.search(detail)
        code = int(match.group(1)) if match else resp.status_code
        msg = _clean_gateway_text(detail)
        if "no 'choices'" in detail:
            # Reasoning models (e.g. gemini-3.5-flash) spend max_tokens on
            # thinking and then have nothing left to answer with. Deterministic.
            raise TamuAIError(
                f"gateway rejected request (HTTP {code}): {msg[:300]}\n"
                "  Likely cause: max_tokens too small for a reasoning model -- "
                "raise it or leave it unset.", retryable=False)
        if code == 524:
            # Cloudflare in front of the gateway cuts any request at 120 s.
            # Retrying the same request just hits the same wall.
            raise TamuAIError(
                f"gateway timed out (HTTP 524): {msg[:200]}\n"
                "  The gateway proxy cuts requests off at 120 s. Shorten the input "
                "or output, or limit thinking (reasoning_effort=\"low\").",
                retryable=False)
        raise TamuAIError(f"gateway rejected request (HTTP {code}): {msg[:500]}",
                          retryable=code in _RETRYABLE_STATUS)

    if resp.status_code >= 400:
        raise TamuAIError(f"HTTP {resp.status_code}: {resp.text[:300]}",
                          retryable=resp.status_code in _RETRYABLE_STATUS)

    return payload


# ---------------------------------------------------------------------------
# Core calls
# ---------------------------------------------------------------------------

def list_models(api_key=None, timeout=30) -> list[str]:
    """Every model id available to this key. The roster changes -- always check."""
    resp = requests.get(f"{BASE_URL}/models", headers=_headers(api_key), timeout=timeout)
    payload = _check_response(resp)
    return sorted(m["id"] for m in payload.get("data", []))


def chat_raw(messages, model, api_key=None, session=None, retries=4,
             timeout=DEFAULT_TIMEOUT, quiet=False, **params) -> dict:
    """One chat completion; returns the full response JSON (including `usage`).

    Always sends stream=False: the gateway streams SSE when `stream` is omitted.
    """
    body = {"model": model, "messages": messages,
            **_guard_params(model, params, quiet), "stream": False}
    headers = _headers(api_key)
    poster = (session or requests).post

    last = None
    for attempt in range(retries):
        try:
            return _check_response(
                poster(f"{BASE_URL}/chat/completions", headers=headers,
                       json=body, timeout=timeout))
        except TamuAIError as exc:
            last = exc
            if not exc.retryable:
                raise
        except requests.RequestException as exc:
            last = TamuAIError(f"{type(exc).__name__}: {exc}", retryable=True)
        if attempt < retries - 1:
            time.sleep(2**attempt + 0.5)
    raise last or TamuAIError("unknown failure")


def chat(messages, model, **kwargs) -> str:
    """One chat completion; returns just the reply text.

    `messages` may also be a plain string, treated as a single user turn.
    For image models the text is a markdown image with a base64 data URI;
    use generate_image() / extract_images() to get the bytes.
    """
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    payload = chat_raw(messages, model, **kwargs)
    try:
        choice = payload["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise TamuAIError(f"unexpected response shape: {str(payload)[:300]}") from exc
    if not (content or "").strip():
        # Failure shape 5: HTTP 200, empty reply. Seen when a thinking model
        # spends the whole output budget thinking. Never return "" as an answer.
        hint = ""
        if choice.get("finish_reason") == "length":
            hint = ("\n  The output budget ran out before any reply text (thinking "
                    "counts against it). Raise max_tokens, or pass "
                    "reasoning_effort=\"low\" to think less.")
        raise TamuAIError(f"{model} returned an empty reply "
                          f"(finish_reason={choice.get('finish_reason')!r}).{hint}")
    # Failure shape 3: an HTTP 200 whose reply text is a gateway error banner,
    # e.g. an image model given a prompt it can't draw.
    if _GATEWAY_BANNER_RE.match(content):
        raise TamuAIError(f"gateway error in reply from {model}: "
                          f"{_clean_gateway_text(content)[:300]}")
    return content


def extract_json(text: str):
    """Parse the first JSON object or array out of a model reply.

    Claude models sometimes wrap JSON in ```json fences and sometimes don't --
    the same prompt produced both. This tolerates fences, prose before or after
    the value, and stray braces in that prose.
    """
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text,
                       flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\{\[]", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
            return value
        except json.JSONDecodeError:
            continue
    raise TamuAIError(f"no JSON found in reply: {text[:200]!r}")


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

_DATA_URI_RE = re.compile(r"data:image/([\w.+-]+);base64,([A-Za-z0-9+/=]+)")
_MAGIC = [(b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpeg"),
          (b"GIF8", "gif"), (b"RIFF", "webp")]


def _sniff_image(data: bytes, declared: str) -> str:
    # gemini-3.1-flash-lite-image labels JPEG bytes as image/png; trust the bytes.
    for magic, fmt in _MAGIC:
        if data.startswith(magic):
            return fmt
    return declared.lower()


def extract_images(text: str) -> list[tuple[str, bytes]]:
    """Every base64 image in a reply, as (format, bytes). Format is sniffed."""
    images = []
    for declared, b64 in _DATA_URI_RE.findall(text or ""):
        data = base64.b64decode(b64)
        images.append((_sniff_image(data, declared), data))
    return images


def generate_image(prompt, model=IMAGE_MODEL, out_path=None, **kwargs):
    """Generate one image through /chat/completions.

    Returns (format, bytes, written_path). /images/generations is blocked (403),
    so image models are reached through chat. If out_path is given the image is
    written there -- with the suffix corrected when it disagrees with the real
    format -- and written_path says where; otherwise written_path is None.
    """
    reply = chat(prompt, model, **kwargs)
    images = extract_images(reply)
    if not images:
        raise TamuAIError(f"{model} returned no image: {reply[:200]!r}")
    fmt, data = images[0]
    if out_path is None:
        return fmt, data, None
    out = Path(out_path)
    ext = ".jpg" if fmt == "jpeg" else f".{fmt}"
    if out.suffix.lower() not in ({".jpg", ".jpeg"} if fmt == "jpeg" else {ext}):
        out = out.with_suffix(ext)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return fmt, data, out


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

class _Cache:
    """Thread-safe JSON cache so a re-run never re-pays for finished work."""

    def __init__(self, path, enabled=True):
        self.path = Path(path) if path else None
        self.enabled = enabled and self.path is not None
        self.lock = threading.Lock()
        self.data = {}
        self.dirty = False
        if self.enabled and self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def get(self, key):
        if not self.enabled:
            return None
        with self.lock:
            return self.data.get(key)

    def put(self, key, value):
        if not self.enabled:
            return
        with self.lock:
            self.data[key] = value
            self.dirty = True

    def save(self):
        if not self.enabled or not self.dirty:
            return
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            tmp.replace(self.path)
            self.dirty = False


def run_batch(items, build_messages, model, workers=DEFAULT_WORKERS,
              cache_path=None, prompt_version=1, parse=None, api_key=None,
              progress_every=25, quiet=False, **params):
    """Run one chat call per item, in parallel, with retry + resume.

    items          : any sequence; each element is passed to build_messages
    build_messages : item -> messages list (or a plain string)
    parse          : optional reply -> value transform, e.g. extract_json
    cache_path     : JSON file; results are keyed by (prompt_version, model,
                     messages), so a re-run costs nothing for finished items and
                     editing a prompt invalidates only what actually changed.
                     Finished work is saved even if the run is interrupted.
    returns        : (results, failures) where results is a list aligned with
                     `items` (None where the call failed) and failures is a list
                     of (index, item, error_string)

    Failures are RETURNED, not raised: one bad item must never discard a long run.
    """
    items = list(items)
    cache = _Cache(cache_path, enabled=cache_path is not None)
    results = [None] * len(items)
    failures = []
    done = 0
    lock = threading.Lock()
    session = requests.Session()
    key = api_key or resolve_key()
    _guard_params(model, params, quiet)  # fail fast, and warn once not per item
    t0 = time.time()

    def work(idx_item):
        idx, item = idx_item
        messages = build_messages(item)
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        ck = hashlib.sha256(json.dumps(
            [prompt_version, model, messages, sorted(params.items())],
            sort_keys=True, default=str).encode()).hexdigest()
        hit = cache.get(ck)
        if hit is not None:
            return idx, item, hit, None
        try:
            reply = chat(messages, model, api_key=key, session=session,
                         quiet=True, **params)
            value = parse(reply) if parse else reply
            cache.put(ck, value)
            return idx, item, value, None
        except Exception as exc:  # noqa: BLE001 - collected, not raised
            return idx, item, None, f"{type(exc).__name__}: {exc}"

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [pool.submit(work, pair) for pair in enumerate(items)]
        for fut in as_completed(futures):
            idx, item, value, err = fut.result()
            with lock:
                done += 1
                if err:
                    failures.append((idx, item, err))
                else:
                    results[idx] = value
                if not quiet and (done % progress_every == 0 or done == len(items)):
                    rate = done / max(time.time() - t0, 1e-3)
                    print(f"  {done}/{len(items)} done ({rate:.1f}/s, "
                          f"{len(failures)} failed)", flush=True)
                if done % 50 == 0:
                    cache.save()
    finally:
        # On Ctrl-C: drop queued calls instead of waiting for them, and keep
        # everything that already finished so a re-run picks up from here.
        pool.shutdown(wait=False, cancel_futures=True)
        cache.save()
    return results, failures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def check_all(workers=DEFAULT_WORKERS) -> int:
    """One round-trip per listed text model. Returns the number that failed."""
    models = [m for m in list_models()
              if not is_image_model(m) and "embedding" not in m.lower()]
    session = requests.Session()
    key = resolve_key()

    def probe(model):
        t0 = time.time()
        try:
            reply = chat("Reply with exactly: ok", model, api_key=key,
                         session=session, retries=1, timeout=60, quiet=True)
            return model, True, f"{time.time() - t0:4.1f}s  {reply.strip()[:40]!r}"
        except Exception as exc:  # noqa: BLE001
            return model, False, f"{type(exc).__name__}: {str(exc)[:120]}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = sorted(pool.map(probe, models))
    width = max(len(m) for m in models)
    for model, ok, note in rows:
        print(f"{'OK  ' if ok else 'FAIL'} {model:<{width}}  {note}")
    failed = sum(not ok for _, ok, _ in rows)
    print(f"\n{len(rows) - failed}/{len(rows)} text models answered. "
          "Image and embedding models were skipped.")
    return failed


def main() -> int:
    ap = argparse.ArgumentParser(
        description="TAMU AI client", formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-models", action="store_true")
    ap.add_argument("--chat", metavar="TEXT", help="send one message")
    ap.add_argument("--image", metavar="PROMPT", help="generate one image")
    ap.add_argument("-o", "--out", default="image.png",
                    help="output path for --image (suffix fixed to real format)")
    ap.add_argument("--check", action="store_true",
                    help="verify the key, list models, and do one round-trip")
    ap.add_argument("--check-all", action="store_true",
                    help="round-trip every listed text model and report which work")
    ap.add_argument("-m", "--model", default=None,
                    help=f"model id (default: {FAST_MODEL}; {IMAGE_MODEL} for --image)")
    ap.add_argument("--system", help="optional system prompt")
    args = ap.parse_args()

    try:
        if args.check:
            model = args.model or FAST_MODEL
            models = list_models()
            print(f"Key OK. {len(models)} models available.")
            print(f"Round-trip on {model}:")
            print("  " + chat("Reply with exactly: ok", model).strip()[:200])
            return 0
        if args.check_all:
            return 1 if check_all() else 0
        if args.list_models:
            for m in list_models():
                print(m)
            return 0
        if args.image:
            _, data, path = generate_image(args.image, args.model or IMAGE_MODEL,
                                           out_path=args.out)
            print(f"Wrote {path} ({len(data):,} bytes)")
            return 0
        if args.chat:
            msgs = ([{"role": "system", "content": args.system}] if args.system else [])
            msgs.append({"role": "user", "content": args.chat})
            print(chat(msgs, args.model or FAST_MODEL))
            return 0
        ap.print_help()
        return 0
    except TamuAIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
