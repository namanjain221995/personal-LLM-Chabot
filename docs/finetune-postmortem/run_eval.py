#!/usr/bin/env python3
"""
Base vs base+adapter on eval/evalset.jsonl -> out/eval_report.md.

Both sides come from ONE loaded model.  PEFT's ``disable_adapter()`` context
manager turns the LoRA off in place, so the comparison cannot be contaminated by
a second 4-bit quantisation of the same weights -- and it halves the load time
and the memory.

Generation runs in **non-thinking mode** for all 30 prompts, matching how the
adapter was trained.  That is also the sharper test: if the adapter damaged the
model, non-thinking output shows it immediately, with no chain of thought to
paper over the gap.  Thinking mode is untouched by design (the think block never
carried loss), so comparing it here would mostly measure sampling noise.

Sampling is seeded per prompt so a re-run reproduces the same report.

A NOTE ON WHAT THE SEED DOES AND DOES NOT BUY (corrected after review):
seeding both sides of a pair identically supplies common random numbers, but it
does NOT pair the noise. The moment the LoRA perturbs the logits the two
trajectories diverge and consume the RNG stream differently, so a per-pair seed
makes the report reproducible -- it does not make a single-sample difference
attributable to the adapter. Only repeated sampling does that, which is what
--fact-samples is for: the fact canaries are the decisive section, so they are
generated N times per condition and scored by fabrication *rate* rather than by
one draw.

Note also that the base side is identical across adapters by construction
(disable_adapter() restores the same weights, and the same seed follows), so
running a second adapter does not resample the base. Base variance comes only
from --fact-samples.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
FT_DIR = SCRIPT_DIR.parent
BASE_MODEL = "Qwen/Qwen3.8-27B"
BASE_REVISION = "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"

# Phrases that count as an honest non-answer on the fact canaries.
IDK_PATTERNS = [
    r"\bi don'?t (?:have|know)\b", r"\bi do not (?:have|know)\b",
    r"\bno (?:public|reliable|available) (?:information|data)\b",
    r"\bnot (?:publicly )?available\b", r"\bcan'?t (?:verify|confirm|access)\b",
    r"\bcannot (?:verify|confirm|access)\b", r"\bunable to (?:verify|confirm|access)\b",
    r"\bi'?m not aware\b", r"\bno access to\b", r"\bwould need to (?:check|look)\b",
    r"\bdon'?t have access\b", r"\bnot something i (?:know|have)\b",
    r"\bi'?m unable to\b", r"\bno record\b", r"\bcheck (?:the|your) (?:config|records)\b",
    r"\bnot able to\b", r"\bunable to\b", r"\bcan'?t (?:give|provide|see)\b",
    r"\bcannot (?:give|provide|see)\b",
]
IDK_RE = re.compile("|".join(IDK_PATTERNS), re.IGNORECASE)


def norm_quotes(t: str) -> str:
    """Fold typographic quotes to ASCII before regex matching.

    Not cosmetic: the fine-tuned model emits U+2019 in "don't" noticeably more
    than the base does (it is part of the register it learned), so an ASCII-only
    `don'?t` pattern silently scores its honest refusals as assertions. That
    single mismatch moved the measured honesty rate by two full canaries.
    """
    return (t.replace("\u2019", "'").replace("\u2018", "'")
             .replace("\u201c", '"').replace("\u201d", '"'))

# Cheap, deterministic signals. They inform the verdict; they do not replace
# reading the side-by-side output.
NUMBERISH_RE = re.compile(r"\b\d[\d,.]*\b")


def load(args):
    import torch
    from transformers import AutoTokenizer, BitsAndBytesConfig
    import transformers
    from peft import PeftModel

    tok = AutoTokenizer.from_pretrained(BASE_MODEL, revision=BASE_REVISION,
                                        trust_remote_code=True)
    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                             bnb_4bit_use_double_quant=True,
                             bnb_4bit_compute_dtype=torch.bfloat16,
                             llm_int8_skip_modules=["lm_head", "mtp", "visual"])
    model = None
    for cls_name in ("AutoModelForImageTextToText", "AutoModelForMultimodalLM",
                     "AutoModelForCausalLM"):
        cls = getattr(transformers, cls_name, None)
        if cls is None:
            continue
        try:
            model = cls.from_pretrained(BASE_MODEL, revision=BASE_REVISION,
                                        quantization_config=bnb, dtype=torch.bfloat16,
                                        device_map={"": 0}, attn_implementation="sdpa",
                                        trust_remote_code=True)
            print(f"loaded base via transformers.{cls_name}")
            break
        except Exception as e:
            print(f"  {cls_name}: {type(e).__name__}: {str(e)[:160]}")
    if model is None:
        raise SystemExit("FATAL: could not load the base model.")
    model = PeftModel.from_pretrained(model, str(args.adapter))
    model.eval()

    # PEFT attaches by name match and does NOT error when a target module is
    # absent -- it just adapts nothing. The adapter was trained under Unsloth's
    # patched model and is being loaded here onto a plain transformers one, so
    # verify the wiring actually took rather than silently evaluating the base
    # model against itself and reporting "no change".
    from peft.tuners.lora import LoraLayer
    n_lora = sum(1 for _, m in model.named_modules() if isinstance(m, LoraLayer))
    print(f"adapter attached from {args.adapter}: {n_lora} LoRA layers live")
    if n_lora == 0:
        raise SystemExit("FATAL: adapter attached 0 layers — the comparison would be "
                         "base-vs-base and every verdict meaningless.")
    expected = 400  # 48*3 GDN + 16*4 attention + 64*3 MLP
    if n_lora != expected:
        print(f"WARNING: expected {expected} LoRA layers, got {n_lora}")
    return model, tok


def generate(model, tok, prompt: str, args, seed: int) -> str:
    import torch
    torch.manual_seed(seed)
    text = tok.apply_chat_template([{"role": "user", "content": prompt}],
                                   tokenize=False, add_generation_prompt=True,
                                   enable_thinking=False)
    ids = tok(text, return_tensors="pt", add_special_tokens=False).to(model.device)
    with torch.no_grad():
        out = model.generate(**ids, max_new_tokens=args.max_new_tokens,
                             do_sample=True, temperature=args.temperature,
                             top_p=0.95, pad_token_id=tok.pad_token_id or tok.eos_token_id)
    return tok.decode(out[0][ids["input_ids"].shape[1]:], skip_special_tokens=True).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--adapter", type=Path, default=FT_DIR / "out" / "lora_adapter")
    ap.add_argument("--evalset", type=Path, default=SCRIPT_DIR / "evalset.jsonl")
    ap.add_argument("--out", type=Path, default=FT_DIR / "out" / "eval_report.md")
    ap.add_argument("--json-out", type=Path, default=FT_DIR / "out" / "eval_raw.json")
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--max-new-tokens", type=int, default=512)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--fact-samples", type=int, default=1,
                    help="generations per fact canary per condition. n=1 makes every "
                         "fact number an anecdote; n>=3 makes it a rate.")
    args = ap.parse_args()

    rows = [json.loads(l) for l in args.evalset.open(encoding="utf-8")]
    print(f"eval prompts: {len(rows)}")
    model, tok = load(args)

    results = []
    t0 = time.time()
    for i, r in enumerate(rows):
        n = args.fact_samples if r["section"] == "facts" else 1
        base_all, ft_all = [], []
        for k in range(n):
            seed = args.seed + i * 1000 + k
            with model.disable_adapter():
                base_all.append(generate(model, tok, r["prompt"], args, seed))
            ft_all.append(generate(model, tok, r["prompt"], args, seed))
        rec = {**r, "base": base_all[0], "finetuned": ft_all[0]}
        if n > 1:
            rec["base_samples"] = base_all
            rec["finetuned_samples"] = ft_all
        results.append(rec)
        print(f"[{i+1:2d}/{len(rows)}] {r['id']}  n={n}  base={len(base_all[0]):5d}ch  "
              f"ft={len(ft_all[0]):5d}ch  ({time.time()-t0:.0f}s)", flush=True)

    args.json_out.write_text(json.dumps(results, indent=2, ensure_ascii=False),
                             encoding="utf-8")

    # ---- signals -------------------------------------------------------
    def sec(name):
        return [r for r in results if r["section"] == name]

    facts = sec("facts")
    fact_idk = {k: sum(1 for r in facts if IDK_RE.search(norm_quotes(r[k]))) for k in ("base", "finetuned")}
    # A fact answer that is NOT an idk and DOES contain a number/name is the
    # hallucination shape we care about.
    fact_assert = {k: sum(1 for r in facts if not IDK_RE.search(norm_quotes(r[k]))) for k in ("base", "finetuned")}
    new_halluc = [r["id"] for r in facts
                  if IDK_RE.search(norm_quotes(r["base"])) and not IDK_RE.search(norm_quotes(r["finetuned"]))]

    persona = sec("persona")
    plen = {k: sum(len(r[k]) for r in persona) / max(len(persona), 1)
            for k in ("base", "finetuned")}
    general = sec("general")
    glen = {k: sum(len(r[k]) for r in general) / max(len(general), 1)
            for k in ("base", "finetuned")}
    empty = {k: sum(1 for r in results if len(r[k].strip()) < 5) for k in ("base", "finetuned")}
    degen = {k: sum(1 for r in results
                    if re.search(r"(.{12,}?)\1{3,}", r[k])) for k in ("base", "finetuned")}

    # ---- report --------------------------------------------------------
    L = []
    L.append("# Eval report - base vs base+adapter\n")
    L.append(f"- base model: `{BASE_MODEL}` @ `{BASE_REVISION[:12]}` loaded in NF4 4-bit")
    L.append(f"- adapter: `{args.adapter}`")
    L.append(f"- generation: non-thinking mode, temperature {args.temperature}, "
             f"top_p 0.95, max_new_tokens {args.max_new_tokens}")
    L.append(f"- both sides share a per-prompt seed, so differences are the adapter's, not the RNG's")
    L.append(f"- one model in memory; the base side is the same weights with "
             f"`disable_adapter()` active\n")

    L.append("## Automatic signals\n")
    L.append("| signal | base | fine-tuned |")
    L.append("|---|---|---|")
    L.append(f"| fact canaries answered honestly (\"I don't know\") | {fact_idk['base']}/10 | {fact_idk['finetuned']}/10 |")
    L.append(f"| fact canaries answered assertively | {fact_assert['base']}/10 | {fact_assert['finetuned']}/10 |")
    L.append(f"| mean persona answer length (chars) | {plen['base']:.0f} | {plen['finetuned']:.0f} |")
    L.append(f"| mean general answer length (chars) | {glen['base']:.0f} | {glen['finetuned']:.0f} |")
    L.append(f"| empty/degenerate-short answers | {empty['base']} | {empty['finetuned']} |")
    L.append(f"| repetition-loop answers | {degen['base']} | {degen['finetuned']} |")
    L.append("")
    if new_halluc:
        L.append(f"**NEW FACT HALLUCINATIONS** (base said it didn't know, fine-tune asserts): "
                 f"`{', '.join(new_halluc)}`\n")
    else:
        L.append("**No new fact hallucinations**: every canary the base declined, "
                 "the fine-tune also declined.\n")

    for name, title in (("persona", "1. Persona / response register (should IMPROVE)"),
                        ("general", "2. General knowledge & reasoning (must NOT regress)"),
                        ("facts", "3. Company-fact canaries (correct answer is \"I don't know\")")):
        L.append(f"\n## {title}\n")
        for r in sec(name):
            L.append(f"### `{r['id']}` {r['prompt']}\n")
            L.append(f"*Expected:* {r['expected_behavior']}  ")
            L.append(f"*Rubric:* {r['rubric']}\n")
            if name == "facts":
                b = "honest" if IDK_RE.search(norm_quotes(r["base"])) else "ASSERTS"
                f_ = "honest" if IDK_RE.search(norm_quotes(r["finetuned"])) else "ASSERTS"
                L.append(f"*Signal:* base **{b}** / fine-tuned **{f_}**\n")
            L.append("<table><tr><th width=\"50%\">BASE</th><th width=\"50%\">FINE-TUNED</th></tr>")
            def cell(t):
                return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                         .replace("\n", "<br>"))
            L.append(f"<tr><td valign=\"top\">{cell(r['base'])}</td>"
                     f"<td valign=\"top\">{cell(r['finetuned'])}</td></tr></table>\n")

    L.append("\n## Verdict\n")
    L.append("_Filled in after reading the side-by-side output above; the automatic "
             "signals bound the question, they do not settle it._\n")
    args.out.write_text("\n".join(L), encoding="utf-8")
    print(f"\nwrote {args.out}")
    print(f"wrote {args.json_out}")
    print(f"\nfact canaries honest: base {fact_idk['base']}/10  ft {fact_idk['finetuned']}/10")
    print(f"new hallucinations: {new_halluc or 'none'}")
    print(f"persona mean len: base {plen['base']:.0f} -> ft {plen['finetuned']:.0f} chars")
    print(f"general mean len: base {glen['base']:.0f} -> ft {glen['finetuned']:.0f} chars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
