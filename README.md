# xoult

A personalized daily arXiv feed. Every weekday it pulls the new cs.CL / cs.IR / cs.LG
announcements (1,000+ papers), embeds them, writes a one-line TLDR for each, and ranks
them per reader from what they save and dismiss.

It's a small recommender system with LLM augmentation, run entirely on free tiers.

## How it works

```
GitHub Actions cron (weekdays)                       Render (free web service)
  arXiv RSS ──► parse ──► Postgres  ◄──────────────── FastAPI ◄── browser
                 │        (pgvector)                     │
                 ├─► fastembed (bge-small, local ONNX)   ├─ stage 1: pgvector ANN against the
                 └─► LiteLLM: Groq ─► Gemini fallback    │  user's Rocchio profile (200 candidates)
                      (TLDRs, traced in Langfuse)        └─ stage 2: rerank on similarity,
                                                            recency, category match
```

- **Ingest** (`src/xoult/ingest.py`): one RSS request per day, idempotent inserts, then
  embeds and summarizes whatever is missing. Every step is resumable, so a failed run is
  fixed by running it again.
- **Ranking** (`src/xoult/rank.py`): a user's profile is the mean of their saved papers'
  embeddings pushed away from dismissed ones (Rocchio). New users get the newest papers
  in the categories they picked.
- **Feedback loop**: every impression, click, save and dismiss is logged with the ranker
  version that produced it, so ranking changes can be compared on live save rate
  (`GET /stats`).

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
| **centroid / all-MiniLM-L6-v2 (production)** | **0.1005** | **0.2728** |
| hybrid: 0.7 · MiniLM + 0.3 · TF-IDF (z-scored) | 0.1138 | 0.2999 |

What this changed:

- **Embedding model.** The project started on bge-small. MiniLM-L6 is 13% better here
  and embeds 3.5× faster on CPU. Its training data includes scientific citation pairs,
  which is close to "papers saved together". arctic-embed-s, built for short query to
  document search, does worst on this symmetric document-to-document task.
- **Multi-interest profiles don't help.** Scoring by the closest single saved paper
  (max-sim) loses to the plain centroid for every model.
- **Lexical signal still matters.** TF-IDF alone matches the best embedding, and the
  hybrid beats both (+13% over production). It isn't in production yet because it needs
  lexical scoring in Postgres; that's the next ranker version.

The production configuration is gated in CI: a PR that drops NDCG@10 by more than 2%
against `evals/baseline.json` fails.

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

Product reasoning and what was deliberately left out: [PRODUCT.md](PRODUCT.md).
