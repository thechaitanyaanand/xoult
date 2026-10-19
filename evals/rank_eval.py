"""Offline ranking eval on CiteULike-a (5,551 users' real paper libraries, 16,980 papers).

For each user, hide 20% of their library, rank every paper they haven't saved, and check
whether the hidden ones come out on top. This measures stage one of the feed (how a
profile is built from saved papers); CiteULike has no dates or arXiv categories, so the
recency and category terms of the reranker are tuned on live data instead.

    uv run python -m evals.rank_eval                 # production config only
    uv run python -m evals.rank_eval --all           # every baseline and embedding model
    uv run python -m evals.rank_eval --check         # CI gate against evals/baseline.json
"""

import argparse
import csv
import json
import random
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import numpy as np

from evals.metrics import ndcg_at_k, recall_at_k
from xoult import lexical, rank
from xoult.config import settings
from xoult.embed import document_text, embed
from xoult.rank import profile_vector

DATA = Path("data/citeulike-a")
SOURCE = "https://raw.githubusercontent.com/js05212/citeulike-a/master/"
BASELINE = Path(__file__).with_name("baseline.json")
TOLERANCE = 0.02  # relative drop in NDCG@10 that fails the gate
EXTRA_MODELS = [
    "sentence-transformers/all-MiniLM-L6-v2",
    "snowflake/snowflake-arctic-embed-s",
]

Scorer = Callable[[list[list[int]]], np.ndarray]


def fetch(name: str) -> Path:
    path = DATA / name
    if not path.exists():
        DATA.mkdir(parents=True, exist_ok=True)
        response = httpx.get(SOURCE + name, timeout=120, follow_redirects=True)
        response.raise_for_status()
        path.write_bytes(response.content)
    return path


def load() -> tuple[list[str], list[list[int]]]:
    csv.field_size_limit(10**7)
    with fetch("raw-data.csv").open(encoding="utf-8", errors="replace") as f:
        docs = [document_text(r["raw.title"], r["raw.abstract"]) for r in csv.DictReader(f)]
    users = [
        [int(i) for i in line.split()[1:]]  # first number is the count; ids are 0-based
        for line in fetch("users.dat").read_text().splitlines()
        if line.strip()
    ]
    return docs, users


def split(users: list[list[int]], seed: int = 0) -> list[tuple[list[int], set[int]]]:
    rng = random.Random(seed)
    out = []
    for items in users:
        items = items[:]
        rng.shuffle(items)
        n_test = max(1, round(len(items) * 0.2))
        out.append((items[n_test:], set(items[:n_test])))
    return out


def evaluate(score: Scorer, splits, n_items: int, chunk: int = 256) -> dict:
    ndcgs, recalls = [], []
    for start in range(0, len(splits), chunk):
        batch = splits[start : start + chunk]
        scores = score([train for train, _ in batch]).astype(np.float32)
        for row, (train, test) in zip(scores, batch, strict=True):
            row[train] = -np.inf  # already in the library, never recommend
            top = np.argpartition(-row, 50)[:50]
            ranked = top[np.argsort(-row[top])].tolist()
            ndcgs.append(ndcg_at_k(ranked, test, 10))
            recalls.append(recall_at_k(ranked, test, 50))
    return {"ndcg@10": float(np.mean(ndcgs)), "recall@50": float(np.mean(recalls))}


def random_scorer(n_items: int) -> Scorer:
    rng = np.random.default_rng(0)
    return lambda trains: rng.random((len(trains), n_items))


def popularity_scorer(splits, n_items: int) -> Scorer:
    counts = np.bincount([i for train, _ in splits for i in train], minlength=n_items)
    return lambda trains: np.tile(counts.astype(np.float32), (len(trains), 1))


def tfidf_scorer(docs: list[str]) -> Scorer:
    from scipy import sparse
    from sklearn.feature_extraction.text import TfidfVectorizer

    x = TfidfVectorizer(sublinear_tf=True, stop_words="english", min_df=2).fit_transform(docs)

    def score(trains):
        rows = np.repeat(np.arange(len(trains)), [len(t) for t in trains])
        cols = np.concatenate(trains)
        weights = np.concatenate([np.full(len(t), 1 / len(t)) for t in trains])
        centroid = sparse.csr_matrix((weights, (rows, cols)), shape=(len(trains), x.shape[0])) @ x
        return (centroid @ x.T).toarray()

    return score


def centroid_scorer(vectors: np.ndarray) -> Scorer:
    empty = np.empty((0, vectors.shape[1]))
    return lambda trains: np.stack([profile_vector(vectors[t], empty) for t in trains]) @ vectors.T


def maxsim_scorer(vectors: np.ndarray) -> Scorer:
    # Score by the single closest saved paper, so users with several distinct
    # interests aren't averaged into one muddy point between them.
    return lambda trains: np.stack([(vectors[t] @ vectors.T).max(axis=0) for t in trains])


def hybrid_scorer(vectors: np.ndarray, docs: list[str]) -> Scorer:
    """Production's two stages, built from the functions the API calls (rank.feed).

    Stage 1 keeps the CANDIDATES papers nearest the reader's profile; stage 2 reorders
    them by embedding + TF-IDF relevance, with IDF from the saved papers and those
    candidates only. Everything below the cut keeps its stage-1 order.
    """
    tokens = [lexical.tokenize(d) for d in docs]
    empty = np.empty((0, vectors.shape[1]))

    def score(trains):
        out = np.empty((len(trains), vectors.shape[0]), dtype=np.float32)
        for row, train in zip(out, trains, strict=True):
            sim = vectors @ profile_vector(vectors[train], empty)
            sim[train] = -np.inf
            top = np.argpartition(-sim, rank.CANDIDATES)[: rank.CANDIDATES]
            lex = lexical.scores([tokens[i] for i in train], [tokens[i] for i in top])
            row[:] = sim - 1e6  # below every reranked candidate, in stage-1 order
            row[top] = rank.relevance(sim[top], lex)
        return out

    return score


def embeddings(docs: list[str], model: str) -> np.ndarray:
    cache = DATA / f"emb-{model.replace('/', '__')}.npy"
    if not cache.exists():
        started = time.monotonic()
        np.save(cache, embed(docs, model=model, parallel=4))
        took = time.monotonic() - started
        print(f"embedded {len(docs)} docs with {model} in {took:.0f}s", flush=True)
    return np.load(cache)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="run baselines and extra models")
    parser.add_argument("--check", action="store_true", help="fail if below evals/baseline.json")
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()

    docs, users = load()
    splits = split(users)
    n = len(docs)
    print(f"{len(users)} users, {n} papers, {sum(len(t) for _, t in splits)} held-out saves")

    production = f"hybrid top-{rank.CANDIDATES} / {settings.embedding_model}"
    results = {}

    def run(name: str, scorer: Scorer) -> None:
        # Report each row as soon as it's done; embedding a model can take a while on CPU.
        results[name] = r = evaluate(scorer, splits, n)
        mark = " (production)" if name == production else ""
        print(f"| {name}{mark} | {r['ndcg@10']:.4f} | {r['recall@50']:.4f} |", flush=True)

    print("\n| method | NDCG@10 | Recall@50 |\n|---|---|---|", flush=True)
    if args.all:
        run("random", random_scorer(n))
        run("popularity", popularity_scorer(splits, n))
        run("tf-idf centroid", tfidf_scorer(docs))
    for model in [settings.embedding_model] + (EXTRA_MODELS if args.all else []):
        vectors = embeddings(docs, model)
        run(f"centroid / {model}", centroid_scorer(vectors))
        if model == settings.embedding_model:
            run(production, hybrid_scorer(vectors, docs))
        if args.all:
            run(f"max-sim / {model}", maxsim_scorer(vectors))

    if args.update_baseline:
        BASELINE.write_text(json.dumps({production: results[production]}, indent=2) + "\n")
        print(f"\nwrote {BASELINE}")
    if args.check:
        baseline = json.loads(BASELINE.read_text())
        # Compare against whatever config the baseline was recorded with, so swapping
        # the embedding model is judged against the old one rather than skipped.
        ((name, expected),) = baseline.items()
        got = results[production]["ndcg@10"]
        floor = expected["ndcg@10"] * (1 - TOLERANCE)
        print(
            f"\ngate: {got:.4f} vs baseline {expected['ndcg@10']:.4f} ({name}), floor {floor:.4f}"
        )
        if got < floor:
            sys.exit("ranking quality regressed")


if __name__ == "__main__":
    main()
