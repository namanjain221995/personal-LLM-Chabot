#!/usr/bin/env python3
"""Prove the 262,144-token window, instead of asserting it.

An environment variable saying MAIN_MODEL_MAX_LEN=262144 proves nothing. This
script builds REAL prompts with the SERVED model's own tokenizer, sends them,
and reports what actually came back — including the failures, which is the
point: KV-cache pressure shows up as a 400 or a hang at a specific size, and
that size is the number worth knowing.

    # from the host, against the published vLLM port:
    orchestrator/.venv/bin/python orchestrator/scripts/validate_long_context.py \\
        --base-url http://127.0.0.1:8000/v1

    # a quick check that skips the expensive sizes:
    ... validate_long_context.py --sizes 8192,65536

WHAT IT CHECKS AT EACH SIZE
  1. the prompt really is that many tokens — counted by vLLM's /tokenize, not
     estimated from characters;
  2. the request is accepted with room left for output;
  3. NEEDLE RETRIEVAL near the beginning, the middle and the end. A model that
     accepts 240k tokens and then answers only from the tail has a 240k context
     window in the same sense that a bucket with a hole has a capacity.

Nothing here writes to the database, touches Salesforce, or changes any
configuration. It is read-only against a running model server.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from typing import List, Optional, Sequence, Tuple

import httpx

#: Sizes worth testing. 64K/128K/200K/240K bracket the useful range: the last
#: one leaves ~22k for output inside a 262,144 window, which is the real
#: working ceiling rather than the advertised one.
DEFAULT_SIZES = (65_536, 131_072, 200_000, 240_000)

#: Where the needles go, as a fraction of the filler.
NEEDLE_POSITIONS = (0.02, 0.5, 0.97)

#: Filler that is cheap to tokenize and carries no facts of its own, so a
#: correct answer cannot come from the filler by accident.
_FILLER_SENTENCES = (
    "The quarterly operations review noted steady throughput across the region.",
    "Scheduling remained unchanged for the reporting period under discussion.",
    "No material deviation was recorded against the published baseline plan.",
    "Coordination between the delivery and enablement teams continued as usual.",
    "Documentation was refreshed to reflect the current process description.",
)


def _service_root(base_url: str) -> str:
    root = base_url.rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


async def _tokenize(
    client: httpx.AsyncClient, base_url: str, model: str, messages: Sequence[dict]
) -> Tuple[int, Optional[int]]:
    resp = await client.post(
        f"{_service_root(base_url)}/tokenize",
        json={"model": model, "messages": list(messages)},
    )
    resp.raise_for_status()
    body = resp.json()
    window = body.get("max_model_len")
    return int(body["count"]), int(window) if window else None


async def _served_model(client: httpx.AsyncClient, base_url: str) -> Tuple[str, int]:
    resp = await client.get(f"{_service_root(base_url)}/v1/models")
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if not data:
        raise RuntimeError("the model server reports no models")
    return str(data[0]["id"]), int(data[0].get("max_model_len") or 0)


def _needle(index: int, code: str) -> str:
    return (
        f"IMPORTANT RECORD {index}: the verification code for checkpoint "
        f"{index} is {code}."
    )


def _build_prompt(
    target_tokens: int, chars_per_token: float, codes: Sequence[str]
) -> Tuple[str, List[str]]:
    """Filler of roughly `target_tokens` tokens with the needles embedded."""
    target_chars = int(target_tokens * chars_per_token)
    rng = random.Random(1337)  # fixed: two runs must be comparable
    parts: List[str] = []
    length = 0
    while length < target_chars:
        line = rng.choice(_FILLER_SENTENCES)
        parts.append(line)
        length += len(line) + 1
    text = "\n".join(parts)

    placed: List[str] = []
    for i, (position, code) in enumerate(zip(NEEDLE_POSITIONS, codes), start=1):
        needle = _needle(i, code)
        placed.append(needle)
        at = min(len(text), max(0, int(len(text) * position)))
        # Snap to a line boundary so a needle is never spliced mid-sentence.
        boundary = text.rfind("\n", 0, at)
        at = boundary + 1 if boundary != -1 else at
        text = text[:at] + needle + "\n" + text[at:]
    return text, placed


async def _resize(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    target: int,
    codes: Sequence[str],
) -> Tuple[str, int]:
    """Build a prompt and iterate until its EXACT token count lands near target.

    Character estimates drift by 20-30% across content types, which at 240k
    tokens is tens of thousands of tokens of error — enough to turn a passing
    test into a 400 or a 200k test into a 170k one.
    """
    chars_per_token = 4.0
    text, _ = _build_prompt(target, chars_per_token, codes)
    for _ in range(6):
        messages = _messages(text)
        count, _window = await _tokenize(client, base_url, model, messages)
        if abs(count - target) <= max(512, target // 100):
            return text, count
        # Rescale from what we measured rather than nudging blindly.
        chars_per_token *= target / max(1, count)
        text, _ = _build_prompt(target, chars_per_token, codes)
    messages = _messages(text)
    count, _window = await _tokenize(client, base_url, model, messages)
    return text, count


def _messages(document: str) -> List[dict]:
    return [
        {
            "role": "system",
            "content": (
                "You answer strictly from the document provided. Reply with the "
                "three verification codes only, comma separated, in checkpoint "
                "order. No explanation."
            ),
        },
        {
            "role": "user",
            "content": (
                f"{document}\n\n"
                "List the verification codes for checkpoints 1, 2 and 3."
            ),
        },
    ]


async def _run_size(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    target: int,
    max_output: int,
) -> dict:
    codes = [f"TS-{target}-{i}{i}{i}{i}" for i in (7, 4, 9)]
    result: dict = {"target_tokens": target}
    try:
        document, exact = await _resize(client, base_url, model, target, codes)
    except Exception as exc:  # noqa: BLE001
        result.update(status="failed", stage="tokenize", error=str(exc)[:300])
        return result
    result["prompt_tokens"] = exact

    started = time.monotonic()
    try:
        resp = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json={
                "model": model,
                "messages": _messages(document),
                "temperature": 0.0,
                "max_tokens": max_output,
                # The reasoning pass draws from the SAME budget as the answer;
                # at this prompt size it reliably consumes all of it and returns
                # an empty string, which would read as a retrieval failure.
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
    except Exception as exc:  # noqa: BLE001
        result.update(
            status="failed",
            stage="request",
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
            latency_seconds=round(time.monotonic() - started, 1),
        )
        return result
    result["latency_seconds"] = round(time.monotonic() - started, 1)

    if resp.status_code != 200:
        result.update(
            status="failed",
            stage="request",
            http_status=resp.status_code,
            error=resp.text[:300],
        )
        return result

    answer = (
        resp.json().get("choices", [{}])[0].get("message", {}).get("content") or ""
    )
    found = [code for code in codes if code in answer]
    result.update(
        status="ok" if len(found) == len(codes) else "degraded",
        needles_found=len(found),
        needles_total=len(codes),
        missing=[c for c in codes if c not in found],
        answer_preview=answer.strip()[:160],
    )
    if result["status"] == "degraded":
        result["detail"] = (
            "the request was accepted but not every needle was recalled — the "
            "window is being ACCEPTED but not fully USED"
        )
    return result


async def main_async(args: argparse.Namespace) -> int:
    timeout = httpx.Timeout(args.timeout, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            model, served_window = await _served_model(client, args.base_url)
        except Exception as exc:  # noqa: BLE001
            print(f"could not reach {args.base_url}: {exc}", file=sys.stderr)
            return 2
        if args.model:
            model = args.model

        configured = int(os.environ.get("MAIN_MODEL_MAX_LEN") or
                         os.environ.get("MODEL_MAX_CONTEXT") or 0)
        print(f"served model        : {model}")
        print(f"served max_model_len: {served_window}")
        if configured:
            print(f"configured (env)    : {configured}")
            if configured != served_window:
                print(
                    "  ! the app is configured for a different window than the "
                    "server serves — long requests will be rejected or the "
                    "extra context wasted"
                )
        print()

        sizes = [s for s in args.sizes if not served_window or s < served_window]
        skipped = [s for s in args.sizes if s not in sizes]
        for size in skipped:
            print(
                f"skipping {size:,}: it does not fit inside the served window "
                f"({served_window:,})"
            )

        results = []
        for size in sizes:
            print(f"testing {size:,} input tokens …", flush=True)
            outcome = await _run_size(
                client, args.base_url, model, size, args.max_output
            )
            results.append(outcome)
            print(f"  {json.dumps(outcome)}", flush=True)

    print()
    ok = [r for r in results if r["status"] == "ok"]
    if ok:
        best = max(r["prompt_tokens"] for r in ok)
        print(
            f"VERIFIED: {best:,} input tokens accepted AND fully recalled "
            f"(needles at the start, middle and end)."
        )
    else:
        print("VERIFIED: nothing — no size passed.")
    for r in results:
        if r["status"] != "ok":
            print(f"  {r['target_tokens']:,}: {r['status']} — "
                  f"{r.get('detail') or r.get('error', '')[:160]}")
    if not ok:
        return 1
    if any(r["status"] != "ok" for r in results):
        print(
            "\nIf a size failed on KV-cache pressure, tune in this order:\n"
            "  1. --max-num-seqs (fewer concurrent sequences)\n"
            "  2. fewer concurrent long requests from the app\n"
            "  3. --max-num-batched-tokens\n"
            "  4. reclaim memory from the other resident models\n"
            "  5. --max-model-len — LAST, and document it."
        )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8000/v1"),
        help="OpenAI-compatible base URL of the main model (ends in /v1)",
    )
    parser.add_argument("--model", default="", help="override the served model id")
    parser.add_argument(
        "--sizes",
        default=",".join(str(s) for s in DEFAULT_SIZES),
        help="comma-separated input token counts to test",
    )
    parser.add_argument("--max-output", type=int, default=128)
    parser.add_argument(
        "--timeout", type=float, default=1800.0,
        help="per-request timeout; a 240k-token prefill is minutes, not seconds",
    )
    args = parser.parse_args(argv)
    args.sizes = [int(s) for s in str(args.sizes).split(",") if s.strip()]
    return asyncio.run(main_async(args))


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
