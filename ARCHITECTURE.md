# Architecture and decisions

How xoult stores data, keeps it safe, and what would change at larger scale. Each
decision says what it costs and what would make it wrong.

## Components

| Piece | Runs on | Talks to |
| --- | --- | --- |
| Ingest (`ingest.py`): arXiv RSS → embed → TLDR → prune | GitHub Actions, weekdays 07:00 and 14:00 UTC | arXiv, LLM providers, Postgres |
| API + page (`api.py`, `static/`) | One Render container | Postgres, Google (sign-in only) |
| Postgres + pgvector | Supabase | — |

The two halves share nothing but the database, so either can fail or redeploy alone.
The API image carries no ML libraries; embedding and LLM calls happen only in ingest.

## User data

| Table | Holds | Kept |
| --- | --- | --- |
| `users` | random id, picked categories, last visit; after Google sign-in also Google's account id (`sub`) and email | Anonymous users who never saved anything: 30 days after their last visit. Everyone else: until they delete their account |
| `events` | one row per impression, click, save, dismiss, with the ranker version | Impressions: 90 days. Everything else: with the user |
| `papers` | arXiv metadata, embedding, TLDR | 30 days, unless someone saved or dismissed it |

Not stored: names, IP addresses, user agents, passwords, Google tokens. Sentry is
initialised with `send_default_pii=False`.

People control their own data from Settings: change categories, download everything
(`GET /me/export`, JSON), or delete the account (`DELETE /me`; events go with it by
`on delete cascade`).

## What xoult uses from Supabase

Only Postgres, through one secret: `DATABASE_URL`, the session pooler URI. Supabase's
other credentials belong to features xoult doesn't use, so they're deliberately left out
of every environment:

| Supabase credential | What it's for | Why xoult doesn't need it |
| --- | --- | --- |
| Project URL + publishable (anon) key | Browser SDK calling Supabase's REST API | The browser only talks to our API. RLS with no policies blocks the REST API anyway |
| Secret (service_role) key | Server SDK with admin rights, bypassing RLS | The server talks Postgres directly. An unused admin key is pure risk |
| JWT secret / JWKS | Verifying Supabase Auth tokens | Identity is Google sign-in plus our own signed cookie, not Supabase Auth |

Switching to Supabase Auth or Storage later would change that; until then, each extra
key would be one more secret to leak for nothing.

## Identity and sessions

- **Anonymous first.** Picking categories creates a `users` row and a session. Asking
  for an account before people have seen the feed would cost most of them.
- **Session = signed cookie** (`xoult_session`, Starlette `SessionMiddleware`): holds
  only the user id, signed with `SECRET_KEY`, `HttpOnly` (page scripts can't read it),
  `SameSite=Lax`, `Secure` in production. The user id never appears in a URL, so it
  doesn't end up in server logs, browser history or referrers.
- **Why not a sessions table:** a signed cookie needs no lookup and no cleanup job. The
  cost is that one session can't be revoked on its own. Deleting the account or
  rotating `SECRET_KEY` revokes everything, and that's enough here.
- **The key is the identity.** Anonymous users exist only through their cookie, so a
  rotated `SECRET_KEY` strands them. Render generates it once (`render.yaml`) and the app
  refuses to start in production with a missing, short or dev key.

### Google sign-in

Optional upgrade, shown once `GOOGLE_CLIENT_ID` is set. It uses Google Identity Services:

1. Google's button returns a signed ID token (a JWT) to the page.
2. The page posts it to `POST /auth/google`.
3. The server checks signature, expiry, issuer and audience against our client id
   (`google.oauth2.id_token.verify_oauth2_token`) and requires a verified email.
4. Accounts are keyed on `sub`, Google's stable id, never on email (emails change).

No client secret, access token or refresh token is involved: xoult only needs to know
who you are, not to act on your behalf. Linking rules, all in one transaction:

| Situation | Result |
| --- | --- |
| Anonymous, first sign-in | The anonymous row becomes the account. Saves stay |
| Anonymous, account exists (second device) | This device's events move into the account; the anonymous row is deleted; the account keeps its categories unless it had none |
| No session, new Google account | New account with no categories; the page asks for them next |
| Two sign-ins racing for a new account | The insert is an upsert on `google_sub`, so both get the same row |

## API security

| Threat | Defence |
| --- | --- |
| Forged session | HMAC-signed cookie; a 32+ character secret is required in production |
| CSRF | `SameSite=Lax`: browsers don't send the cookie on cross-site POST/PATCH/DELETE. As a second layer, JSON POSTs, PATCH and DELETE need a CORS preflight the API never grants, and HTML forms can't produce them |
| XSS | All text goes through `textContent`, never `innerHTML`. CSP `script-src 'self'` plus Google's script only, so no inline scripts; `HttpOnly` keeps the cookie away from scripts anyway |
| Clickjacking | `frame-ancestors 'none'` |
| Abuse / flooding | Per-IP limits per minute: sign-up 5, sign-in 10, reads 60, writes 120. A 429 comes with `Retry-After` |
| Bots on many IPs | A global cap of 300 new anonymous profiles an hour bounds database growth whatever the IP count. Profiles that never save are pruned after 30 days. Creating one needs JavaScript (a JSON POST), which stops plain scrapers. No CAPTCHA: it would tax every real visitor for a threat not yet seen; Cloudflare Turnstile is the drop-in if the cap starts tripping |
| Crawlers | `robots.txt` allows the landing page and `llms.txt`, disallows the per-user API |
| Bad input | Pydantic validates every body and query (category pattern, id lengths, event kinds); SQL is always parameterised |
| Supabase's public REST API | RLS on, with no policies (DEPLOY.md, Part 3); the app connects as the owner |
| Vulnerable dependencies | Dependabot for Python, Actions and the Docker base image; versions pinned in `uv.lock` |
| Leaked secrets | Only in env vars and GitHub/Render secrets; `.env` is gitignored |

Known limits: the rate limiter lives in process memory (right for one instance; see
Scale), and `/stats` is public on purpose (aggregates only).

## LLM calls

`llm.complete` tries models in order (`settings.llm_models`). Free tiers come first,
providers alternate so one outage doesn't block two steps in a row, and the paid one
comes last:

1. `groq/openai/gpt-oss-20b`, about 1 s
2. `gemini/gemini-3.5-flash-lite`, about 1 s
3. `openrouter/nvidia/nemotron-3-super-120b-a12b:free`, about 3 s
4. `gemini/gemini-3.5-flash` (its own free quota)
5. `openrouter/google/gemma-4-31b-it:free` (free models share capacity, often busy)
6. `meta_llama/Llama-4-Maverick-17B-128E-Instruct-FP8`
7. `xai/grok-4.20-non-reasoning`, which bills per token

A rate-limited model is skipped for 60 s. A failing one (bad key, no credits, outage) is
skipped for 10 minutes. Without this, every call under load would first retry the
overloaded provider with backoff. Measured at 4× Groq's rate limit: 60 of 60 TLDRs
written, with Groq rested twice.

The free quota is smaller than arXiv's output, so TLDRs go first to papers already
shown to someone, then to followed categories, then to the newest. The 14:00 UTC run
spends the next quota on what people saw that morning.

## Caching

The expensive things are already computed once and stored, which is the caching that
matters here:

| What | Cost to produce | Stored where | Reused by |
| --- | --- | --- | --- |
| TLDR | one LLM call, 1–3 s | `papers.tldr` | every reader, forever |
| Embedding | ~10 ms of CPU | `papers.embedding` | every feed request |
| Embedding model weights | ~90 MB download | GitHub Actions cache | every ingest run |
| CiteULike embeddings (eval) | ~8 min | Actions cache, keyed on the model | every CI run |

What is deliberately **not** cached:

- **The personal feed** (~34 ms of compute). It changes with every save and dismiss, so
  a cache would need invalidating on every event, and would also have to track what each
  person was already shown. That's more code and more bugs than it saves. Network
  distance mattered far more than compute: the database is a region away from the app,
  so `/feed` was cut from about 14 database round trips to about 7, measured by adding
  25 ms to every database reply:
  - autocommit connections, so no separate BEGIN and COMMIT round trips;
  - the session lookup also stamps "last seen", in the same query;
  - saves and dismisses come back in one query, not two;
  - impressions are logged in one statement, not one per paper;
  - embeddings cross the network only for the 20 papers shown;
  - "latest announcement" is cached for 5 minutes, since it changes only when ingest
    runs.
- **`/config`, `/stats`, `/saved`, `/me`:** 2–4 ms each. A cache would cost more than
  it saves.
- **No Redis.** With one instance, process memory already holds what needs holding (the
  rate-limit counters). Redis becomes necessary at the second instance, for shared rate
  limits, not before.

HTTP caching:
- Personal responses (`/feed`, `/saved`, `/me`, `/events`, `/auth/*`, `/config`) send
  `Cache-Control: no-store`, so no proxy, CDN or shared machine keeps a copy of someone's
  data.
- Static files carry ETags, so a returning browser revalidates with a 304 instead of
  downloading them again.
- Responses over 1 KB are gzipped. The feed's ~40 KB of JSON shrinks about 4×, which
  matters on slow phones.

What would change this:
- **Many readers per category combination:** cache the unranked candidate pool per
  (categories, day) and rank per user in memory.
- **Global audience or heavy traffic:** put Cloudflare's free CDN in front for static
  files and DDoS/bot filtering. It needs a custom domain.

## Scale: what we don't run, and when we would

Each item has a measured trigger and a migration path that the current design keeps
open.

### Kubernetes

- **Not now:** one stateless container. Render already gives health checks, restarts,
  rolling deploys and TLS. A cluster would add a control plane, manifests and on-call
  cost for zero user benefit.
- **Trigger:** several independently scaled services, more than about 3 API instances,
  or multi-region.
- **Path:** the API is already stateless (session in the cookie, data in Postgres) and
  ships as one Docker image, so it runs unchanged on any orchestrator. The only
  in-process state is the rate limiter's counter. Move that to Redis first; that step
  is also what makes a second instance correct.

### Kafka

- **Not now:** the event volume is about 20 impression rows per feed load, which
  Postgres absorbs without noticing. One producer and one consumer don't need a broker.
- **Trigger:** sustained thousands of event writes a second, or several consumers of the
  same stream (real-time features, a warehouse, a training pipeline).
- **Path:** `events` is already an append-only log with a fixed schema and the ranker
  version on each row. The cheaper steps come first: batch the impression inserts,
  partition `events` by month, then change-data-capture (Debezium) from Postgres. Kafka
  replaces the insert only when a consumer needs it in real time.

### A separate vector database (Qdrant, Pinecone, Weaviate)

- **Not now:** the feed ranks one week of papers in the reader's categories, a few
  thousand vectors. Exact distances over those take milliseconds and are 100% accurate,
  so even the HNSW index was dropped. Keeping vectors in Postgres also means one backup,
  one transaction, and joins with events.
- **Trigger:** ranking over the whole archive (full-text-plus-semantic search), more
  than about 5–10M vectors, or filtered ANN at high QPS. Free storage binds first: the
  500 MB Supabase plan holds about 100k papers, which is why papers expire after 30 days.
- **Path:** all vector access goes through `rank.feed` and `embed.embed`. Swapping
  the store changes those two places.

### Fine-tuning

- **Embedding model, not yet:** choosing among pretrained models already moved
  NDCG@10 from 0.0886 to 0.1005, and the shipped embedding + TF-IDF hybrid to 0.1118
  (README), at no training cost.
  - **Trigger:** about 10k co-saved pairs from real users, enough to train with an
    in-batch contrastive loss and still hold out an eval split.
  - **Path:** the CiteULike eval and the CI gate decide whether a tuned model ships. A
    model with a different dimension also needs the schema changed and `--reembed`.
- **LLM for TLDRs, no:** prompting a strong model already writes good TLDRs, and the
  fallback chain covers quotas.
  - **Trigger:** the summary eval (SciTLDR, ROUGE, faithfulness judge) showing a gap
    that prompting can't close, or LLM cost becoming the bill.
  - **Path:** distill the current model's TLDRs into a small open model. The same
    eval decides.
