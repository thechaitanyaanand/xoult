# Setup and deployment

You'll run xoult locally with Docker first, then put it online on free tiers. Budget about
60–90 minutes the first time. None of these services needs a credit card.

| Piece | Service | What runs there |
| --- | --- | --- |
| Database (Postgres + pgvector) | Supabase | Papers, users, events, embeddings |
| API + web page | Render (Docker) | `uvicorn xoult.api:app`, built from `render.yaml` |
| Daily paper ingest | GitHub Actions | `ingest.yml`, weekdays 07:00 UTC |
| CI + ranking eval gate | GitHub Actions | `ci.yml` on every push and PR |
| TLDR generation | Groq, Gemini as fallback | Called by LiteLLM during ingest |
| LLM tracing (optional) | Langfuse | Every TLDR call: prompt, tokens, latency |
| Error tracking (optional) | Sentry | API exceptions |

The ingest and the API never talk to each other. Both only read and write the database,
so either can fail or redeploy without affecting the other.

Order of work:

1. Run everything locally (Part 1)
2. Create accounts and collect keys (Part 2)
3. Create the Supabase database (Part 3)
4. Push to GitHub, add secrets, run CI and the first ingest (Part 4)
5. Deploy the API on Render (Part 5)
6. Verify production end to end (Part 6)

---

## Part 1: Run it locally with Docker

Run every command from the repo folder in PowerShell. The Docker database listens on
port **5433**, so it never collides with a PostgreSQL already installed on 5432.

1. Start Docker Desktop and wait until it shows "Engine running". In a **new** terminal,
   `docker --version` should print a version. If it says "not recognized", finish the
   Docker Desktop installer (it may ask you to restart Windows) and open a fresh terminal.

2. Install dependencies (API, ingest extras, dev and eval tools):

   ```powershell
   uv sync --all-extras
   ```

3. Create your env file:

   ```powershell
   Copy-Item .env.example .env
   ```

   Its `DATABASE_URL` already points at the Docker database. Until you have Groq/Gemini
   keys (Part 2), add these lines so TLDRs come from the local Ollama model
   (`ollama pull qwen2.5:1.5b` once):

   ```
   LLM_MODELS=["ollama/qwen2.5:1.5b"]
   LLM_RPM=600
   ```

4. Start the database and check it is healthy:

   ```powershell
   docker compose up -d db
   docker compose ps
   ```

   `STATUS` should read `healthy` within about 10 seconds.

5. Create the tables:

   ```powershell
   uv run python -m xoult.db
   ```

   Expected: `schema ok: events, papers, users`

6. Fetch today's papers, embed them, and write 20 TLDRs:

   ```powershell
   uv run python -m xoult.ingest --tldrs 20
   ```

   Expected: `feed had ~1100 new papers`, a few `embedded ...` lines, then
   `wrote 20/20 tldrs` (a few may be rejected for length). arXiv announces nothing on
   Saturday and Sunday, so the feed is empty on weekends.

7. Start the API and use the app:

   ```powershell
   uv run uvicorn xoult.api:app --reload
   ```

   - http://127.0.0.1:8000: pick categories, then work through the list. **Save** and
     **Not for me** (or the keys `s` and `x`, with `j`/`k` to move and `u` to undo), then
     **Re-rank with what you've saved**. Recommendations now say which saved paper they
     follow from.
   - **Saved** lists what you saved, with BibTeX and Remove. **Stats** shows save rate
     per ranking version.
   - http://127.0.0.1:8000/docs: auto-generated API docs

8. Run the same checks CI runs. The API test truncates tables, so it gets its own
   database:

   ```powershell
   docker compose exec db createdb -U xoult xoult_test   # once
   $env:TEST_DATABASE_URL = "postgresql://xoult:xoult@localhost:5433/xoult_test"
   uv run ruff check .
   uv run ruff format --check .
   uv run pytest
   uv run python -m evals.rank_eval --check
   ```

   Without `TEST_DATABASE_URL` the API test is skipped and the rest still run. The eval
   reuses the embeddings cached in `data/`, so it takes about a minute; on a fresh clone
   the first run downloads CiteULike and embeds 17k papers (~8 minutes).

9. Build and run the exact image Render will run:

    ```powershell
    docker build -t xoult .
    docker run --rm -p 8001:8000 -e DATABASE_URL=postgresql://xoult:xoult@host.docker.internal:5433/xoult xoult
    ```

    http://127.0.0.1:8001/health should return `{"status":"ok"}`. Use
    `host.docker.internal` because `localhost` inside a container means the container itself.

`docker compose down` stops the database and keeps the data in the `pgdata` volume.
`docker compose down -v` deletes the data too.

---

## Part 2: Accounts and keys

Collect these first. Each fills one variable, used locally in `.env` and later as a
GitHub secret.

| Key | Where to get it | Variable |
| --- | --- | --- |
| Groq | [console.groq.com](https://console.groq.com) → API Keys → Create API Key | `GROQ_API_KEY` |
| Gemini | [aistudio.google.com](https://aistudio.google.com) → Get API key → Create API key | `GEMINI_API_KEY` |
| Langfuse (optional) | [cloud.langfuse.com](https://cloud.langfuse.com) → new project → Settings → API Keys → Create | `LANGFUSE_PUBLIC_KEY` (`pk-lf-…`), `LANGFUSE_SECRET_KEY` (`sk-lf-…`), `LANGFUSE_HOST` |
| Sentry (optional) | [sentry.io](https://sentry.io) → Create Project → platform FastAPI → copy the DSN | `SENTRY_DSN` |
| Database URL | Part 3 | `DATABASE_URL` |

Notes:

- **Check the model names.** The defaults in `src/xoult/config.py` are
  `groq/openai/gpt-oss-20b`, then `gemini/gemini-3.5-flash-lite`. Confirm both appear in
  the Groq and Gemini model lists on your free plan. If one has been renamed or retired,
  change the default in `config.py`. Setting `LLM_MODELS` only in your local `.env`
  doesn't reach GitHub Actions.
- **Langfuse host** depends on the region you picked when creating the project (EU:
  `https://cloud.langfuse.com`, US: `https://us.cloud.langfuse.com`). Use the URL shown
  next to your keys.

Test the keys locally: put them in `.env`, remove the `LLM_MODELS=` line from Part 1, then:

```powershell
uv run python -m xoult.ingest --tldrs 5
```

Expected: `wrote 5/5 tldrs`. With Langfuse keys set, five `tldr-v1` traces appear under
Tracing in Langfuse within a minute.

---

## Part 3: Database on Supabase

1. [supabase.com](https://supabase.com) → New project. Name it `xoult`, generate a
   strong database password **and save it**, and pick the region closest to you
   (South Asia: Mumbai).

2. Wait 1–2 minutes for the project to finish provisioning.

3. Click **Connect** (top bar) → **Session pooler** → copy the URI. It looks like:

   ```
   postgresql://postgres.<project-ref>:[YOUR-PASSWORD]@aws-0-<region>.pooler.supabase.com:5432/postgres
   ```

   Replace `[YOUR-PASSWORD]` with your password. If the password contains `@ : / ? #`,
   URL-encode those characters (for example `@` → `%40`), or generate a password without
   them. Use the **Session pooler**, not "Direct connection": the direct host is IPv6-only
   on the free plan, and Render and GitHub runners can't reach it.

4. Create the tables from your machine:

   ```powershell
   $env:DATABASE_URL = "<the Supabase URI>"
   uv run python -m xoult.db
   Remove-Item Env:DATABASE_URL
   ```

   Expected: `schema ok: events, papers, users`. `create extension vector` in the schema
   enables pgvector; Supabase allows it.

5. **Lock down the auto-generated REST API.** Supabase serves every table in `public`
   through its Data API, and tables without Row Level Security can be read and written by
   anyone holding the project's public anon key. xoult never uses that API; it connects
   as the `postgres` owner, which RLS doesn't restrict. Turn RLS on with no policies,
   which blocks the Data API completely. In **SQL Editor**, run:

   ```sql
   alter table papers enable row level security;
   alter table users  enable row level security;
   alter table events enable row level security;
   ```

6. Check **Table Editor**: `papers`, `users` and `events` are listed, with no
   "RLS disabled" warning.

---

## Part 4: GitHub (code, secrets, CI, first ingest)

1. On github.com, create an **empty** repository named `xoult` (no README or license).
   - **Public:** unlimited free Actions minutes, and recruiters can read it.
   - **Private:** 2,000 free minutes a month. Enough, but the first eval run spends ~15.

2. Commit your work, then push it. The workflows only run on `main`, and your local branch
   is `master`, so rename it:

   ```powershell
   git branch -M main
   git remote add origin https://github.com/<your-username>/xoult.git
   git push -u origin main
   ```

3. Add the secrets: repo → **Settings → Secrets and variables → Actions → New repository
   secret**. Add each, names exactly as written:

   | Secret | Required |
   | --- | --- |
   | `DATABASE_URL` | yes (Supabase session pooler URI) |
   | `GROQ_API_KEY` | yes |
   | `GEMINI_API_KEY` | yes (fallback) |
   | `LANGFUSE_PUBLIC_KEY` | optional |
   | `LANGFUSE_SECRET_KEY` | optional |
   | `LANGFUSE_HOST` | optional |

4. Open the **Actions** tab. The push started **ci**, which runs three jobs:
   - `test`: lint, format and unit tests (~2 min)
   - `ranking-eval`: the NDCG gate. The first run embeds 17k papers (~8–15 min); later
     runs restore them from cache and take ~2 min.
   - `docker`: builds the Render image and checks the API imports in it

   All three should go green. If the workflow fails on `uv sync --locked`, run `uv lock`
   locally and commit `uv.lock`.

5. Run the first ingest by hand: **Actions → ingest → Run workflow → Run workflow**. It
   takes ~15 minutes (TLDR calls are paced at 25 a minute, capped at 300 a run). The
   logs should show `schema ok`, `feed had N new papers`, `embedded … papers` and
   `wrote K/300 tldrs`.

6. Confirm the data in Supabase **SQL Editor**:

   ```sql
   select count(*) as papers, count(embedding) as embedded, count(tldr) as with_tldr from papers;
   ```

From now on the ingest runs on its own every weekday at 07:00 UTC (12:30 IST). GitHub
can start scheduled runs late by several minutes. That's normal.

---

## Part 5: Deploy the API on Render

1. [render.com](https://render.com) → sign up with GitHub and allow access to the
   `xoult` repo.

2. **New → Blueprint** → pick the `xoult` repo. Render reads `render.yaml` and proposes
   one free web service called `xoult`.

3. Fill in the variables it asks for:
   - `DATABASE_URL`: the same Supabase session pooler URI
   - `SENTRY_DSN`: your DSN, or leave it empty

4. **Apply**. The first Docker build takes ~3–5 minutes. Under **Logs**, the deploy is
   done when you see `Uvicorn running on http://0.0.0.0:10000` (Render sets `PORT`
   itself) and the health check on `/health` passes.

5. Your app is at `https://xoult-<something>.onrender.com`. Every push to `main`
   redeploys it automatically.

How the free plan behaves:

- The service **sleeps after 15 minutes without traffic**. The next request waits
  ~30–60 seconds while it boots, and the page shows "The server may be waking up" in the
  meantime. That's expected on the free plan.
- 750 free instance hours a month covers one service running all month.

---

## Part 6: Verify production

- [ ] **ci** is green on `main` (test, ranking-eval, docker)
- [ ] The **ingest** run succeeded and the SQL count shows papers with embeddings and TLDRs
- [ ] `https://<your-app>.onrender.com/health` returns `{"status":"ok"}`
- [ ] The app loads, you can pick categories, and the feed shows today's papers with TLDRs
- [ ] After saving ~5 papers and dismissing ~5, a reload shows a feed tilted toward what you saved
- [ ] `/stats` shows impressions and a save rate for `v1-linear`
- [ ] Langfuse → Tracing shows `tldr-v1` generations with tokens and latency
- [ ] **Proof the eval gate works:** open a PR that changes `embedding_model` in
      `config.py` to `BAAI/bge-small-en-v1.5`. The `ranking-eval` job should fail with
      `ranking quality regressed` (0.0886 < floor 0.0985). Close the PR without merging.
      Link it in your README as evidence.

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `docker compose up`: port is already allocated | Something else already uses 5433 | Change the left side of `"5433:5432"` in `docker-compose.yml` and the port in `.env` to match |
| `connection refused` from `xoult.db` locally | DB container not running, or wrong port in `.env` | `docker compose ps`; `DATABASE_URL` must use port 5433 |
| Supabase: `password authentication failed` | Wrong password, or special characters not URL-encoded | Reset the DB password in Supabase settings, or encode it |
| Supabase: `Network is unreachable` / IPv6 error | Used the direct-connection URI | Use the Session pooler URI (Part 3, step 3) |
| Ingest: `arXiv returned 429, retrying` | arXiv rate limit | It retries with backoff on its own; if it gives up, re-run later |
| Ingest: `feed had 0 new papers` | Weekend or holiday, or run before ~06:00 UTC | Normal; the next weekday run picks up |
| TLDRs: `AuthenticationError` | Secret missing, misnamed, or empty | Check the secret names in Part 4, step 3 |
| TLDRs: `wrote 0/300` with `RateLimitError` | Free LLM quota used up | Lower `tldrs_per_run` in `config.py`; Gemini fallback covers some |
| TLDRs: `model not found` / decommissioned | Provider renamed or retired the model | Update `llm_models` in `config.py` (Part 2 notes) |
| Render deploy fails the health check | `DATABASE_URL` missing or wrong | Render → Environment, fix it, Manual Deploy |
| Page says "Couldn't load the feed" | Free instance waking up | Wait a minute and reload |
| No traces in Langfuse | Keys missing, or host is the wrong region | Check `LANGFUSE_HOST` matches the project's region |
| CI `ranking-eval` fails after your change | NDCG@10 dropped more than 2% | Fix it; if the change is a deliberate trade-off, see "Update the eval baseline" |
| CI fails on `uv sync --locked` | `uv.lock` is stale | `uv lock`, commit `uv.lock` |

---

## Day-to-day operations

**Change the LLM.** Edit `llm_models` in `src/xoult/config.py`. Before and after, compare
quality with a judge that is a *different* model from the one being judged (a model
grading its own output is biased toward it):

```powershell
uv run python -m evals.summary_eval --n 100 --judge gemini/gemini-3.5-flash
```

**Change the embedding model.**

1. Add the candidate to `EXTRA_MODELS` in `evals/rank_eval.py` and run
   `uv run python -m evals.rank_eval --all`.
2. If it wins, set it in `config.py` and run
   `uv run python -m evals.rank_eval --update-baseline`. Commit `config.py` and
   `evals/baseline.json` together.
3. After merging, re-embed production once from your machine:

   ```powershell
   $env:DATABASE_URL = "<the Supabase URI>"
   uv run python -m xoult.ingest --reembed --tldrs 0
   Remove-Item Env:DATABASE_URL
   ```

The model must output 384 dimensions, or `vector(384)` in `db/schema.sql` changes too.

**Update the eval baseline.** Only when a drop is a deliberate trade-off, or after an
improvement: `uv run python -m evals.rank_eval --update-baseline`, then commit
`evals/baseline.json` with a message that says why.

**Ship a new ranker** (for example the TF-IDF hybrid). Change the `RANKER` string in
`src/xoult/rank.py` (for example `v2-hybrid`). `/stats` then reports the old and new
versions separately, so you can compare live save rates.

**Add arXiv categories.** Edit `arxiv_categories` in `config.py`. More categories mean
more papers a day and more TLDR quota used.

**Keeping the free tiers alive.**

- Supabase pauses projects after about a week without activity. The weekday ingest keeps
  it active; if it does pause, click **Restore** in the dashboard.
- GitHub disables scheduled workflows after 60 days without commits to the repo. Re-enable
  `ingest` from the Actions tab if that happens.
- Free LLM quotas change. Check the Groq and Gemini consoles if TLDR counts drop.

**Back up the database** (the Supabase free plan keeps no long-term backups). You already
have the PostgreSQL 17 tools installed:

```powershell
& 'C:\Program Files\PostgreSQL\17\bin\pg_dump.exe' "<the Supabase URI>" -Fc -f xoult.dump
```
