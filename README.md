# xoult

A personalized daily arXiv feed. Every weekday it pulls the new announcements in six
ML categories (cs.CL, cs.IR, cs.LG, cs.AI, cs.CV, stat.ML: about 1,900 papers), embeds
them, writes one-line TLDRs, and ranks them per reader from what they save and dismiss.

It's a small recommender system with LLM augmentation, run entirely on free tiers.
No account is needed to start; Google sign-in keeps saves across devices.

## How it works

```
GitHub Actions cron (weekdays)                        Render (free web service)
  arXiv RSS ──► parse ──► Postgres  ◄────────────────── FastAPI ◄── browser
                 │        (pgvector)                      │         (signed session cookie)
                 ├─► fastembed (MiniLM-L6, local ONNX)    ├─ stage 1: top 200 by exact cosine to
                 └─► LLM chain with cooldowns:            │  the reader's Rocchio profile, over a
                     Groq → Gemini → OpenRouter → …       │  week of papers in their categories
                     (TLDRs, traced in Langfuse)          └─ stage 2: 50/50 embedding + TF-IDF
                                                             relevance, recency, category match
```

- **Ingest** (`src/xoult/ingest.py`): one RSS request per day, idempotent inserts, then
  embeds and summarizes whatever is missing, writing TLDRs first for papers people are
  actually shown. Every step is resumable, so a failed run is fixed by running it again.
- **Ranking** (`src/xoult/rank.py`, ranker `v2-hybrid`): a reader's profile is the mean
  of their saved papers' embeddings, pushed away from dismissed ones (Rocchio). The 200
  closest papers are reranked by embedding similarity blended 50/50 with TF-IDF
  similarity to the saved papers (`lexical.py`). TF-IDF catches exact method and dataset
  names that embeddings blur. New readers get the newest papers in their categories.
- **Explanations**: each recommendation names the saved paper it is closest to ("because
  you saved X"), since an averaged profile can't say why on its own.
- **Feedback loop**: every impression, click, save and dismiss is logged with the ranker
  version that produced it, so ranking changes can be compared on live save rate
  (`GET /stats`).

Data storage, the security model, Google sign-in, the LLM fallback chain, and why
there's no Kubernetes, Kafka, separate vector database or fine-tuning (with the point
at which each would be worth it) are in [ARCHITECTURE.md](ARCHITECTURE.md).

## Evaluation

**Ranking**: CiteULike-a, 5,551 real users' paper libraries. 20% of each library is held
out and ranked against all 16,980 papers (`uv run python -m evals.rank_eval --all`).

| Method | NDCG@10 | Recall@50 |
|---|---|---|
| random | 0.0005 | 0.0028 |
| popularity | 0.0117 | 0.0348 |
| TF-IDF centroid | 0.1016 | 0.2703 |
| centroid / snowflake-arctic-embed-s | 0.0637 | 0.1904 |
| centroid / bge-small-en-v1.5 | 0.0886 | 0.2457 |
| max-sim / bge-small-en-v1.5 | 0.0770 | 0.2462 |
| max-sim / all-MiniLM-L6-v2 | 0.0822 | 0.2605 |
| centroid / all-MiniLM-L6-v2 (v1 production) | 0.1005 | 0.2728 |
| hybrid over all papers: 0.7 · MiniLM + 0.3 · TF-IDF | 0.1138 | 0.2999 |
| **hybrid, as served: top 200, then 0.5 · MiniLM + 0.5 · TF-IDF (v2 production)** | **0.1118** | **0.2960** |

What this changed:

- **Embedding model.** The project started on bge-small. MiniLM-L6 is 13% better here
  and embeds 3.5× faster on CPU. Its training data includes scientific citation pairs,
  which is close to "papers saved together". arctic-embed-s, built for short query to
  document search, does worst on this symmetric document-to-document task.
- **Multi-interest profiles don't help.** Scoring by the closest single saved paper
  (max-sim) loses to the plain centroid for every model.
- **Lexical signal still matters, and shipped as v2.** TF-IDF alone matches the best
  embedding, and a hybrid beats both. Scoring every paper that way is too slow for a
  request, so production reranks only the top 200 by embedding. That version was
  measured on its own (IDF and z-scores from the reranked set only): +11.3% NDCG@10 over
  v1. Lexical weights tried: 0.2 → +6.9%, 0.3 → +9.4%, **0.5 → +11.3%**, 0.6 → +9.6%,
  0.7 → +7.3%.

The production row is computed by the same functions the API calls, and gated in CI: a
PR that drops NDCG@10 more than 2% below `evals/baseline.json` fails.

**TLDRs**: SciTLDR-A test set, ROUGE against the closest human-written TLDR, rejection
rate, and an LLM judge for faithfulness (`uv run python -m evals.summary_eval --n 100
--judge <a different model>`).

## Running it locally

Requires [uv](https://docs.astral.sh/uv/) and either Docker or a free Supabase project.

```sh
uv sync --all-extras
cp .env.example .env          # add API keys; point DATABASE_URL at Supabase if not using Docker
docker compose up -d db       # skip if using Supabase
uv run python -m xoult.db     # applies db/schema.sql
uv run python -m xoult.ingest # fetch today's papers (needs GROQ/GEMINI keys, or set LLM_MODELS to an Ollama model)
uv run uvicorn xoult.api:app --reload
```

Tests: `uv run pytest`. Lint: `uv run ruff check . && uv run ruff format --check .`

## Deploying (all free tiers)

| Piece | Where |
|---|---|
| Postgres + pgvector | Supabase |
| API + UI | Render, from `render.yaml` (Docker) |
| Daily ingest | GitHub Actions, `.github/workflows/ingest.yml` |
| LLM | Groq, falling back to Gemini |
| Tracing / errors | Langfuse / Sentry |

Step-by-step setup and deployment: [DEPLOY.md](DEPLOY.md). Product reasoning and what was
deliberately left out: [PRODUCT.md](PRODUCT.md).
