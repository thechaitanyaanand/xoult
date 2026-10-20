"""TLDR quality on SciTLDR-A test (617 abstracts, each with several human-written TLDRs).

Generates TLDRs with the production prompt and model chain, then reports ROUGE against
the closest human TLDR, how often the output is rejected by clean_tldr, and optionally an
LLM judge's faithfulness rate. The lead-sentence row is the classic extractive baseline:
a summarizer that can't beat "first sentence of the abstract" isn't adding much.

    uv run python -m evals.summary_eval --n 100
    uv run python -m evals.summary_eval --n 100 --judge gemini/gemini-3.5-flash
"""

import argparse
import json
import time
from pathlib import Path

import httpx
from rouge_score import rouge_scorer

from xoult import llm
from xoult.config import settings

DATA = Path("data/scitldr")
SOURCE = (
    "https://raw.githubusercontent.com/allenai/scitldr/master/SciTLDR-Data/SciTLDR-A/test.jsonl"
)

JUDGE_PROMPT_VERSION = "faithfulness-judge-v1"
JUDGE_PROMPT = """You check summaries of research abstracts for faithfulness.
A summary is faithful if every claim in it is stated in or directly implied by the abstract.
Reply with JSON only: {"faithful": true or false, "reason": "<one short sentence>"}"""


def load(n: int) -> list[dict]:
    path = DATA / "test.jsonl"
    if not path.exists():
        DATA.mkdir(parents=True, exist_ok=True)
        response = httpx.get(SOURCE, timeout=120, follow_redirects=True)
        response.raise_for_status()
        path.write_bytes(response.content)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return rows[:n]


def judge(abstract: str, summary: str, model: str) -> bool | None:
    # An outage or unparseable reply is no verdict; it shouldn't discard the run so far.
    try:
        text = llm.complete(
            [
                {"role": "system", "content": JUDGE_PROMPT},
                {"role": "user", "content": f"Abstract: {abstract}\n\nSummary: {summary}"},
            ],
            name=JUDGE_PROMPT_VERSION,
            models=[model],
            response_format={"type": "json_object"},
            max_tokens=1024,
        )
        return bool(json.loads(text)["faithful"])
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=100)
    parser.add_argument("--judge", help="model for the faithfulness judge; use a different one")
    args = parser.parse_args()

    rows = load(args.n)
    scorer = rouge_scorer.RougeScorer(["rouge1", "rouge2", "rougeL"], use_stemmer=True)

    def best(pred: str, refs: list[str]) -> dict[str, float]:
        scores = [scorer.score(ref, pred) for ref in refs]
        return {k: max(s[k].fmeasure for s in scores) for k in ("rouge1", "rouge2", "rougeL")}

    lead, model, words, verdicts, rejected = [], [], [], [], 0
    for i, row in enumerate(rows, 1):
        abstract = " ".join(row["source"])
        lead.append(best(row["source"][0], row["target"]))

        started = time.monotonic()
        pred = llm.tldr(row.get("title") or "", abstract)
        if pred is None:
            rejected += 1
        else:
            model.append(best(pred, row["target"]))
            words.append(len(pred.split()))
            if args.judge:
                verdicts.append(judge(abstract, pred, args.judge))
        print(f"\r{i}/{len(rows)}", end="", flush=True)
        time.sleep(max(0.0, 60 / settings.llm_rpm - (time.monotonic() - started)))
    print()

    def avg(results: list[dict], key: str) -> str:
        return f"{100 * sum(r[key] for r in results) / len(results):.1f}" if results else "-"

    print(f"\nmodel chain: {' -> '.join(settings.llm_models)}  prompt: {llm.TLDR_PROMPT_VERSION}")
    print("\n| system | ROUGE-1 | ROUGE-2 | ROUGE-L |\n|---|---|---|---|")
    for name, results in [("lead sentence", lead), ("xoult tldr", model)]:
        cells = " | ".join(avg(results, k) for k in ("rouge1", "rouge2", "rougeL"))
        print(f"| {name} | {cells} |")
    print(f"\nrejected by clean_tldr: {rejected}/{len(rows)}")
    if words:
        print(f"mean length: {sum(words) / len(words):.1f} words")
    if verdicts:
        judged = [v for v in verdicts if v is not None]
        rate = f"{100 * sum(judged) / len(judged):.1f}%" if judged else "-"
        print(
            f"faithful per {args.judge}: {rate} ({len(verdicts) - len(judged)} without a verdict)"
        )


if __name__ == "__main__":
    main()
