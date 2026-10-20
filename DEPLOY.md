# Setup and deployment

This guide goes from an empty machine to xoult running online. You run everything
locally first, then deploy to free tiers. None of these services needs a credit card.
Budget 1.5–2 hours the first time.

| Piece | Service | What runs there |
| --- | --- | --- |
| Database (Postgres + pgvector) | Supabase, Mumbai (`ap-south-1`) | Papers, users, events, embeddings |
| API + web page | Render (Docker), Singapore: the nearest Render region to Mumbai | `uvicorn xoult.api:app`, from `render.yaml` |
| Paper ingest | GitHub Actions | `ingest.yml`, weekdays 07:00 and 14:00 UTC |
| CI + ranking eval gate | GitHub Actions | `ci.yml` on every push and PR |
| TLDRs | Groq first, then Gemini, OpenRouter, Meta (all free tiers) | Called during ingest only |
| Sign-in (optional) | Google Identity Services | Verifies who someone is; nothing else |
| LLM tracing (optional) | Langfuse | Every TLDR call: prompt, tokens, latency |
| Error tracking (optional) | Sentry | API exceptions |

The ingest and the API share nothing but the database, so either can fail or redeploy
alone. How data is stored, the security model, and why there's no Kubernetes, Kafka,
cache layer or separate vector database are in [ARCHITECTURE.md](ARCHITECTURE.md).

Order of work:

| Part | What | Time |
| --- | --- | --- |
| 0 | Install the tools | 15 min |
| 1 | Run everything locally | 20 min |
| 2 | Create accounts and collect keys | 20 min |
| 3 | Google sign-in (optional) | 10 min |
| 4 | Create the Supabase database | 10 min |
| 5 | Push to GitHub, add secrets, run CI and the first ingest | 25 min |
| 6 | Deploy on Render | 10 min |
| 7 | Verify production | 10 min |

---

## Part 0: Install the tools

| Tool | Why | Install (Windows) | Check |
| --- | --- | --- | --- |
| Git | Version control | [git-scm.com](https://git-scm.com/download/win) | `git --version` |
| uv | Installs Python 3.12 and every dependency from `uv.lock` | `powershell -c "irm https://astral.sh/uv/install.ps1 \| iex"` | `uv --version` |
| Docker Desktop | Local Postgres + pgvector; builds the production image | [docker.com](https://www.docker.com/products/docker-desktop/) | `docker --version` |
| Ollama (optional) | Local LLM for TLDRs without API keys | [ollama.com](https://ollama.com), then `ollama pull qwen2.5:1.5b` | `ollama list` |

You don't need to install Python yourself; uv fetches 3.12. Open a **new** terminal after
installing, so the updated PATH is picked up.

---

## Part 1: Run it locally

Run every command from the repo folder in PowerShell. The Docker database listens on
port **5433**, so it never collides with a PostgreSQL already installed on 5432.

1. **Get the code and dependencies**

   ```powershell
   git clone https://github.com/<your-username>/xoult.git   # skip if you already have it
   cd xoult
   uv sync --all-extras
   ```

2. **Create your env file**

   ```powershell
   Copy-Item .env.example .env
   ```

   `DATABASE_URL` already points at the Docker database. Fill in API keys after Part 2.
   Until then, uncomment the two Ollama lines (`LLM_MODELS`, `LLM_RPM`) to write TLDRs
   with the local model.

   Variable names must be exactly as in `.env.example`: uppercase, underscores, no spaces.
   Windows forgives lowercase, but GitHub Actions and Render run Linux and won't.

3. **Start the database**

   ```powershell
   docker compose up -d db
   docker compose ps          # STATUS should read "healthy" within ~10 s
   uv run python -m xoult.db  # creates the tables; prints "schema ok: events, papers, users"
   ```

4. **Fetch today's papers**

   ```powershell
   uv run python -m xoult.ingest --tldrs 50
   ```

   Expected: `feed had ~1900 new papers`, several `embedded …` lines, `wrote …/50 tldrs`,
   then a `pruned …` line. arXiv announces nothing on Saturday and Sunday, so the feed is
   empty on weekends. Running it twice is safe: every step skips work already done.

5. **Start the app**

   ```powershell
   uv run uvicorn xoult.api:app --port 8000
   ```

   Open **http://localhost:8000**. Use `localhost`, not `127.0.0.1`, or Google sign-in
   fails with `origin_mismatch`. Stop the server with **Ctrl+C** in its terminal. On
   Windows, closing the window can leave a server process holding the port (see
   Troubleshooting).

6. **Try every feature**

   - Pick categories, then **Start reading**. The welcome page shows each category's
     daily volume.
   - Work through the list: **Save** / **Not for me**, or the keys `j` `k` (move), `s`
     (save), `x` (not for me), `o` (open), `a` (abstract), `u` (undo).
   - **Re-rank with what you've saved.** Recommendations now say "Because you saved …".
   - **Saved:** BibTeX and Remove. **Stats:** save rate per ranker version.
   - **Settings:** change categories, switch to the **Wide** layout (two columns on
     screens 1080 px and wider), sign in (after Part 3), download or delete your data.
   - Discovery files: http://localhost:8000/robots.txt, /sitemap.xml, /llms.txt
   - API docs: http://localhost:8000/docs

7. **Run the checks CI runs.** The API tests truncate tables, so they get their own
   database:

   ```powershell
   docker compose exec db createdb -U xoult xoult_test   # once
   $env:TEST_DATABASE_URL = "postgresql://xoult:xoult@localhost:5433/xoult_test"
   uv run ruff check .
   uv run ruff format --check .
   uv run pytest
   uv run python -m evals.rank_eval --check
   ```

   Without `TEST_DATABASE_URL` the API tests are skipped and the rest still run. The
   first eval run downloads CiteULike and embeds 17k papers (~8 minutes); later runs use
   the cache in `data/` and take about a minute.

8. **Run the production image locally.** This mirrors what Render runs, including the
   production-only checks:

   ```powershell
   docker build -t xoult .
   $key = uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
   docker run --rm -p 8001:8000 `
     -e DATABASE_URL=postgresql://xoult:xoult@host.docker.internal:5433/xoult `
     -e COOKIE_SECURE=true -e SECRET_KEY=$key xoult
   ```

   http://localhost:8001/health returns `{"status":"ok"}`. `host.docker.internal` is how
   a container reaches your machine; inside it, `localhost` means the container itself.
   (`COOKIE_SECURE=true` makes browsers drop the cookie over plain http, so use this run
   only to check health and headers, not to click around.)

`docker compose down` stops the database and keeps its data. `docker compose down -v`
deletes the data too.

---

## Part 2: Accounts and keys

Each key goes in `.env` locally and later in GitHub and Render secrets.

| Key | Where | Variable | Needed? |
| --- | --- | --- | --- |
| Groq | [console.groq.com](https://console.groq.com) → API Keys | `GROQ_API_KEY` | Yes, the main LLM |
| Gemini | [aistudio.google.com](https://aistudio.google.com) → Get API key | `GEMINI_API_KEY` | Recommended: takes over when Groq is rate limited |
| OpenRouter | [openrouter.ai](https://openrouter.ai) → Keys | `OPENROUTER_API_KEY` | Optional. Without credit only `:free` models work, and xoult uses only those |
| Meta Llama API | [llama.developer.meta.com](https://llama.developer.meta.com) → API keys | `LLAMA_API_KEY` | Optional |
| Session secret | `uv run python -c "import secrets; print(secrets.token_urlsafe(32))"` | `SECRET_KEY` | Production only; Render generates it for you |
| Google client ID | Part 3 | `GOOGLE_CLIENT_ID` | Optional: sign-in is hidden without it |
| Langfuse | [cloud.langfuse.com](https://cloud.langfuse.com) → project → Settings → API Keys | `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | Optional |
| Sentry | [sentry.io](https://sentry.io) → Create Project → FastAPI → DSN | `SENTRY_DSN` | Optional |
| Database URL | Part 4 | `DATABASE_URL` | Yes |

**How the LLM keys are used.** `llm_models` in `src/xoult/config.py` is tried top to
bottom: Groq, Gemini Flash-Lite, OpenRouter (free Nemotron), Gemini Flash, OpenRouter
(free Gemma), Meta Llama. A model that's rate limited is skipped for 60 s; one with a
bad key, no credits or an outage, for 10 minutes. You can run with Groq alone; every
extra key adds headroom.

**Check the keys work** (one real call per model in the chain):

```powershell
uv run python -m xoult.ingest --tldrs 5
```

Expected: `wrote 5/5 tldrs`. A line like `meta_llama/… failed with APIError; trying the next
model` names a provider whose key is wrong or has no credits. The chain carries on
without it.

**Langfuse host** matches the region you picked for the project (EU:
`https://cloud.langfuse.com`, US: `https://us.cloud.langfuse.com`). With keys set,
`tldr-v1` traces appear under Tracing within a minute of an ingest.

---

## Part 3: Google sign-in (optional)

People can use xoult without an account; signing in keeps their saves across devices.
xoult uses Google's **ID-token flow**: Google's button hands the page a signed token,
and the server checks it. Nothing redirects, and the client secret is never used.

1. [console.cloud.google.com](https://console.cloud.google.com) → create a project
   named `xoult`.
2. **APIs & Services → OAuth consent screen** ("Google Auth Platform" in newer consoles)
   → app name `xoult`, your support email, audience **External**.
3. **Clients → Create client** → type **Web application**:

   | Field | Local testing | Add after deploying (Part 6) |
   | --- | --- | --- |
   | Authorized JavaScript origins | `http://localhost:8000` and `http://localhost` | `https://<your-app>.onrender.com` |
   | Authorized redirect URIs | none | none |

   Type origins exactly: scheme, host and port, with no trailing `/` and no path.
4. Copy the **Client ID** (ends in `.apps.googleusercontent.com`) into
   `GOOGLE_CLIENT_ID` in `.env`. You don't need the client secret; don't store it.
5. **Audience → Publish app.** While in "Testing", only test users you add can sign in.
   xoult requests only `openid email profile`, which needs no Google verification review.
6. Restart the server (Ctrl+C, then start it again; `.env` is read at startup) and open
   **http://localhost:8000**. The **Sign in with Google** button appears on the welcome
   page and in Settings.

Test it:
1. Save a paper, then sign in. Settings shows "Signed in with Google as …" and the save
   is still there.
2. In a private window, save a different paper and sign in with the same account. Both
   saves now appear: the two devices merged into one account.

Origin changes in Google's console can take from 5 minutes to a few hours to apply.

---

## Part 4: Database on Supabase

1. [supabase.com](https://supabase.com) → **New project** → name `xoult`, generate a
   strong password **and save it**, region **South Asia (Mumbai)**, `ap-south-1`.
   - **About regions.** The app runs on Render, which has no India region, so
     `render.yaml` pins it to **Singapore**, the closest one. Every feed request makes
     several database round trips. Mumbai–Singapore is a short hop; a US or Europe
     region on either side would make each trip cross a continent.
   - For the lowest latency, create the project in **Southeast Asia (Singapore)**
     instead, next to Render. A project's region can't be changed after creation.

2. Wait 1–2 minutes while it provisions.

3. **Connect** (top bar) → **Session pooler** → copy the URI. For a Mumbai project it looks
   like:

   ```
   postgresql://postgres.<project-ref>:[YOUR-PASSWORD]@aws-0-ap-south-1.pooler.supabase.com:5432/postgres
   ```

   - Copy the host exactly as Supabase shows it. Projects in the same region sit on
     different pooler clusters (`aws-0-…`, `aws-1-…`). The wrong one answers "tenant/user
     not found".
   - Replace `[YOUR-PASSWORD]`. If the password contains `@ : / ? #`, URL-encode them
     (`@` → `%40`) or generate one without them.
   - Use the **Session pooler**, not "Direct connection": the direct host
     (`db.<project-ref>.supabase.co`) is IPv6-only on the free plan, and Render and
     GitHub runners can't reach it.
   - This URI is the **only** Supabase value xoult needs. The project URL, publishable
     key, secret key and JWT/JWKS settings are for Supabase's own SDKs and Auth, which
     xoult doesn't use. Leave them out
     ([ARCHITECTURE.md](ARCHITECTURE.md#what-xoult-uses-from-supabase)).
   - **Test it on your machine before pasting it anywhere else.** A wrong password used by
     a server that retries trips Supabase's lockout (`ECIRCUITBREAKER`) for the whole
     project:

     ```powershell
     $env:DATABASE_URL = "<the Supabase URI>"
     uv run python -m xoult.db      # "schema ok: …" means the URI is right
     Remove-Item Env:DATABASE_URL
     ```

4. Create the tables and load today's papers from your machine:

   ```powershell
   $env:DATABASE_URL = "<the Supabase URI>"
   uv run python -m xoult.db
   uv run python -m xoult.ingest --tldrs 100
   Remove-Item Env:DATABASE_URL
   ```

5. **Check Supabase's automatic REST API is locked.** Supabase serves every `public` table
   over HTTP, and without Row Level Security anyone holding the project's public anon key
   could read and write them. `db/schema.sql` turns RLS on with no policies, which shuts
   that API out. xoult connects as the tables' owner, which RLS doesn't restrict. In
   **Table Editor**, `papers`, `users` and `events` are listed with no "RLS disabled"
   warning.

**Storage:** each paper takes about 4.5 KB. Ingest deletes papers older than 30 days
that nobody saved or dismissed, so the database levels off around 190 MB of the free
500 MB. Check it under **Database → Usage**.

---

## Part 5: GitHub (code, secrets, CI, first ingest)

1. On github.com create an **empty** repository `xoult`: no README, license or
   .gitignore.
   - **Public:** unlimited free Actions minutes, and recruiters can read the code.
   - **Private:** 2,000 free minutes a month, which is enough.

2. Push. The workflows run on `main`; if your local branch is `master`, rename it:

   ```powershell
   git branch -M main
   git remote add origin https://github.com/<your-username>/xoult.git
   git push -u origin main
   ```

3. **Settings → Secrets and variables → Actions → New repository secret.** Add each,
   named exactly:

   | Secret | Needed? |
   | --- | --- |
   | `DATABASE_URL` | Yes: the Supabase session pooler URI |
   | `GROQ_API_KEY` | Yes |
   | `GEMINI_API_KEY` | Recommended |
   | `OPENROUTER_API_KEY`, `LLAMA_API_KEY` | Optional fallbacks |
   | `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | Optional |

   Ingest is the only workflow that uses these. CI needs no secrets: its tests use a
   throwaway database and no LLM.

4. **Actions** tab: the push started **ci**, with three jobs.

   | Job | What it proves | Time |
   | --- | --- | --- |
   | `test` | Lint, format, unit tests, and the API tests against a real pgvector database | ~2 min |
   | `ranking-eval` | NDCG@10 on CiteULike hasn't dropped more than 2% | first run ~10–15 min, then ~2 |
   | `docker` | The production image builds and the API imports in it | ~2 min |

   If a job fails on `uv sync --locked`, run `uv lock` locally and commit `uv.lock`.

5. **Actions → ingest → Run workflow.** About 15 minutes: TLDRs are paced at 25 a
   minute, 300 a run. The log shows `schema ok`, `feed had N new papers`, `embedded …`,
   `wrote K/300 tldrs`, and `pruned …`.

6. **Dependabot** (`.github/dependabot.yml`) now opens weekly PRs for Python packages and
   monthly ones for Actions and the Docker base image. CI tests each one; merge when
   green.

From now on ingest runs on its own every weekday at 07:00 UTC (12:30 IST). A second run at
14:00 UTC writes TLDRs for papers people were shown that morning. GitHub sometimes
starts scheduled runs several minutes late; that's normal.

---

## Part 6: Deploy on Render

1. [render.com](https://render.com) → sign up with GitHub and grant access to the
   `xoult` repo.

2. **New → Blueprint** → pick `xoult`. Render reads `render.yaml`: one free Docker web
   service, region Singapore (the nearest Render region to a Mumbai database), health
   check on `/health`.

3. Fill in the variables it asks for:

   | Variable | Value |
   | --- | --- |
   | `DATABASE_URL` | The Supabase session pooler URI |
   | `GOOGLE_CLIENT_ID` | Your client ID, or empty to hide sign-in |
   | `PUBLIC_URL` | The app's address, e.g. `https://xoult-abcd.onrender.com`; fill it in once the first deploy shows the URL |
   | `SENTRY_DSN` | Your DSN, or empty |
   | `GITHUB_TOKEN` | Optional. GitHub → Settings → Developer settings → Fine-grained tokens → this repo only, **Actions: Read and write**. Lets a paper a reader adds by hand count within minutes; empty means it waits for the next scheduled ingest. Set `GITHUB_REPO` too if your fork isn't `thechaitanyaanand/xoult` |

   `render.yaml` sets the rest:
   - **`SECRET_KEY`** is generated once by Render. **Never regenerate it:** anonymous
     users exist only through cookies signed with it, so a new key cuts them off from
     their saves.
   - **`COOKIE_SECURE=true`** turns on HTTPS-only cookies and HSTS. The app refuses to
     start in this mode without a strong `SECRET_KEY`.

4. **Apply.** The first build takes ~3–5 minutes. It's live when the log shows
   `Uvicorn running on http://0.0.0.0:10000` and the health check passes.

5. Finish the wiring:
   - Set `PUBLIC_URL` to the real address. Render redeploys on its own.
   - Add `https://<your-app>.onrender.com` to the Google client's **Authorized JavaScript
     origins** (Part 3, step 3).

Every push to `main` redeploys automatically.

**The free plan sleeps.** After 15 minutes without traffic the service stops, and the
next visit waits 30–60 s while it boots; the page says the server may be waking up.
To keep it awake, a free UptimeRobot monitor requests `/health` every 5 minutes
(HTTP(s) monitor, 5-minute interval). `/health` also touches the database, so Supabase
never pauses either. GitHub Actions cron isn't used for this: its schedules run late or
get skipped, and any gap over 15 minutes lets the service sleep.

Render's free terms allow this. Only heavy *outbound* traffic gets a service flagged,
and these pings are tiny inbound requests. The real limit is hours: awake all month
uses ~744 of the workspace's 750 free hours, so this must be the workspace's **only**
free web service. Run out and Render suspends every free service until the next month.

**Custom domain (optional).** Render → Settings → Custom Domains, then add that
`https://` origin to Google's console and `PUBLIC_URL`.

---

## Part 7: Verify production

- [ ] **ci** is green on `main` (test, ranking-eval, docker)
- [ ] The **ingest** run succeeded; in Supabase:
      `select count(*), count(embedding), count(tldr) from papers;` shows all three > 0
- [ ] `https://<your-app>.onrender.com/health` returns `{"status":"ok"}`
- [ ] The app loads, categories show daily counts, and the feed shows today's papers with TLDRs
- [ ] After ~5 saves and ~5 "Not for me", re-ranking shows "Because you saved …"
- [ ] Settings → Wide works on a laptop-sized window
- [ ] **Sign in with Google** works, and saves survive signing in
- [ ] Settings → **Download my data** downloads JSON. Try **Delete everything** on a
      test account
- [ ] `/robots.txt`, `/sitemap.xml` and `/llms.txt` show your `PUBLIC_URL`, not `localhost`
- [ ] Pasting the link into WhatsApp or Slack shows the preview card
- [ ] Browser dev tools → Network → `/feed` response has `cache-control: no-store`, and
      the document has `content-security-policy` and `strict-transport-security`
- [ ] Langfuse → Tracing shows `tldr-v1` generations (if configured)
- [ ] **Proof the eval gate works:** open a PR that sets `LEXICAL_WEIGHT = 0.0` in
      `src/xoult/rank.py`, which turns the hybrid back into v1's embeddings-only ranking.
      `ranking-eval` should fail with `ranking quality regressed` (0.1005 < floor
      0.1096). Close it without merging and link it in your README as evidence.

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `docker` not recognized | Installer unfinished, or the terminal is older than the install | Finish Docker Desktop setup (it may restart Windows); open a new terminal |
| `docker compose up`: port is already allocated | Something else uses 5433 | Change the left side of `"5433:5432"` in `docker-compose.yml` and the port in `.env` |
| `connection refused` from `xoult.db` | Database container not running, or wrong port | `docker compose ps`; `DATABASE_URL` must use 5433 locally |
| Server changes don't show; old behaviour persists | A previous server process still holds port 8000 (Windows keeps orphaned workers alive) | `Get-NetTCPConnection -LocalPort 8000 -State Listen` shows its PID; `Stop-Process -Id <pid> -Force`, then start again. Stop servers with Ctrl+C |
| `localhost` is slow to answer from scripts | Windows tries IPv6 (`::1`) first; the server listens on IPv4 | Browsers fall back instantly; in scripts use `127.0.0.1` |
| Supabase: `password authentication failed` | Wrong password, or special characters not URL-encoded | Reset the database password, or encode it |
| Supabase: `Network is unreachable` / IPv6 | Used the direct-connection URI | Use the Session pooler URI (Part 4, step 3) |
| Ingest: `arXiv returned 429, retrying` | arXiv rate limit | Retries with backoff on its own; if it gives up, run again later |
| Ingest: `feed had 0 new papers` | Weekend, holiday, or before ~06:00 UTC | Normal; the next weekday run picks up |
| Ingest: `<model> failed with …; trying the next model` | That provider is rate limited, out of credit, or its key is wrong | Expected now and then; if one provider always fails, check its key |
| Ingest: `wrote 0/300 tldrs` | Every provider failed | Check the keys (Part 2); lower `tldrs_per_run` if quotas are tight |
| Render deploy fails the health check | `DATABASE_URL` missing or wrong | Render → Environment, fix it, **Manual Deploy** |
| Render crash: `Set SECRET_KEY to 32+ random characters` | `SECRET_KEY` missing or short while `COOKIE_SECURE=true` | Let the Blueprint generate it, or set 32+ random characters |
| Feed takes ~1 s or more in production | App and database on different continents | Keep `region: singapore` in `render.yaml` for a Mumbai or Singapore database (Part 4, step 1) |
| Supabase: `(ECIRCUITBREAKER) too many authentication failures` | Something kept connecting with a wrong password (usually Render or the ingest workflow) until Supabase blocked logins | Fix the password in `DATABASE_URL` everywhere (`.env`, GitHub secret, Render), suspend or fix whatever was retrying, then wait a few minutes for the block to lift. `python -m xoult.db` locally confirms the URI first |
| Password "correct" but still `password authentication failed` | `[YOUR-PASSWORD]` replaced inside the brackets, or special characters not URL-encoded | The brackets are part of the placeholder: `…:MyPass123@aws-0…`, not `…:[MyPass123]@…`. Or reset to an alphanumeric password |
| Supabase: `tenant/user postgres.<ref> not found` | Wrong pooler cluster in the host (`aws-0` vs `aws-1`) | Copy the host exactly from Supabase → Connect → Session pooler |
| Page says "Couldn't reach the server" | Free instance waking up | Wait a minute, then **Try again** |
| Google button missing | `GOOGLE_CLIENT_ID` unset, or the server wasn't restarted after setting it | Set it, restart, reload |
| Google: `Error 400: origin_mismatch` | The address bar's origin isn't in Authorized JavaScript origins | Add it exactly (scheme, host, port, no `/`); locally open `http://localhost:8000`, not 127.0.0.1; wait a few minutes after saving |
| Google: only you can sign in | Consent screen still in "Testing" | Audience → **Publish app** |
| `429 Too Many Requests` while testing | Rate limits: 5 sign-ups a minute per IP, 300 an hour overall | Wait; limits are the `RateLimit(...)` lines in `api.py` |
| Everyone logged out after a deploy | `SECRET_KEY` changed | Restore the old key; anonymous saves come back with it |
| No traces in Langfuse | Keys missing, or the wrong region host | `LANGFUSE_HOST` must match the project's region |
| CI `ranking-eval` fails | NDCG@10 dropped more than 2% | Fix it; for a deliberate trade-off, see "Update the eval baseline" |
| CI fails on `uv sync --locked` | `uv.lock` is stale | `uv lock`, then commit `uv.lock` |
| Supabase project paused | A week with no activity | Dashboard → **Restore**; the weekday ingest normally prevents this |
| Scheduled ingest stopped running | GitHub disables schedules after 60 days without commits | Actions → ingest → **Enable workflow** |

---

## Day-to-day operations

**Change the LLM.** Edit `llm_models` in `src/xoult/config.py`. Compare quality before
and after with a judge that isn't the model being judged:

```powershell
uv run python -m evals.summary_eval --n 100 --judge gemini/gemini-3.5-flash
```

**Rotate an API key.** Create the new key, update it in `.env`, GitHub secrets, and
Render (if it's used there), then revoke the old one. Never rotate `SECRET_KEY` this way
(see Part 6).

**Change the embedding model.**

1. Add the candidate to `EXTRA_MODELS` in `evals/rank_eval.py` and run
   `uv run python -m evals.rank_eval --all`.
2. If it wins, set it in `config.py`, run
   `uv run python -m evals.rank_eval --update-baseline`, and commit `config.py` with
   `evals/baseline.json`.
3. After merging, re-embed production once:

   ```powershell
   $env:DATABASE_URL = "<the Supabase URI>"
   uv run python -m xoult.ingest --reembed --tldrs 0
   Remove-Item Env:DATABASE_URL
   ```

   The model must output 384 dimensions, or `vector(384)` in `db/schema.sql` changes too.

**Update the eval baseline.** Only after an improvement or for a deliberate trade-off:
`uv run python -m evals.rank_eval --update-baseline`, then commit `evals/baseline.json`
with a message saying why.

**Ship a new ranker** (for example the TF-IDF hybrid). Change `RANKER` in
`src/xoult/rank.py` (for example `v2-hybrid`). `/stats` then shows old and new versions
side by side.

**Add arXiv categories.** Edit `arxiv_categories` in `config.py` (and `CATEGORY_NAMES`
in `api.py` for its display name). More categories mean more papers and more TLDR quota.

**Schema changes.** `db/schema.sql` is safe to re-run. New columns use
`alter table … add column if not exists`, and ingest applies the schema before every
run. For changes that aren't additive (renames, type changes), introduce a migration
tool at that point.

**Back up the database.** The free plan keeps no long-term backups:

```powershell
& 'C:\Program Files\PostgreSQL\17\bin\pg_dump.exe' "<the Supabase URI>" -Fc -f xoult.dump
```

Restore with `pg_restore -d "<URI>" xoult.dump`.
