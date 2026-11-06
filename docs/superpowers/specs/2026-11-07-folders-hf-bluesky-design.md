# Folders, Hugging Face signals, Bluesky posts

Date: 2026-11-07. Status: approved 2026-11-07.

## Why

xoult has 4 users, all friends told in passing. The goal is real daily readers, which is
also the resume story: live save-rate numbers only mean something with real traffic.
The first real audience is the author's HCI lab (cs.HC was added for them; they share
papers in a WhatsApp group). These three pieces serve that:

- **Folders** make the library useful to people who read for a thesis or a reading
  group, and a **shared folder link** posted to the lab group advertises xoult each time.
- **Hugging Face (HF) signals** show which new papers people are talking about and
  where the code is, at the cost of one unauthenticated request per day.
- **Bluesky posts** are free reach beyond the lab: many ML and HCI researchers are there.

## Goals

1. A reader can group saved papers into named folders and filter Saved by folder.
2. A reader can share a folder as a link; anyone with the link sees a read-only page.
3. Paper entries show HF upvotes and a code link when HF has them.
4. Each weekday the ingest job posts the new day's papers to a xoult Bluesky account.

## Non-goals

- Notes on papers (planned next, separately).
- Folders changing ranking beyond what saving already does. A paper in a folder is a
  saved paper and shapes the ranking exactly like any other save; the folder adds nothing.
- Using HF upvotes in the ranker. They are displayed only. The offline eval
  (CiteULike) can't measure them, and a ranker change should be compared on live save
  rate, which needs more readers first. Revisit then. (Builder's call: recorded here.)
- Collaborative folders (others adding papers), folder search, rename. Add when asked.
- Any third-party script, tracker, or new Python dependency.

## Anti-goals (failure even if it works)

- A shared folder page that reveals who made it (email, Google id, other folders).
- A shared folder that search engines index without the owner knowing.
- A Bluesky account that posts duplicates, posts on days with nothing new, or floods.
- Ingest failing because HF or Bluesky is down. Both are best-effort; arXiv ingest,
  embeddings, TLDRs and prune must run regardless.

## Constraints

- Free tiers: Render free web service (512 MB), Supabase free Postgres (500 MB),
  GitHub Actions cron. No card anywhere.
- Follow existing patterns: `db/schema.sql` is idempotent and re-run on startup and
  by ingest; sync FastAPI handlers with psycopg; `RateLimit` dependencies; RLS enabled
  on every table; vanilla JS with `el()` in `app.js`; server-rendered public pages via
  `_page()` and `papers.html`; `httpx` for outbound HTTP in ingest.
- Everything a user stores must appear in `/me/export` and be removed by `DELETE /me`.
- Privacy page (`static/privacy.html`) must describe shared folders.

## Design

### Folders: data

```sql
create table if not exists folders (
    id         bigint generated always as identity primary key,
    user_id    uuid not null references users (id) on delete cascade,
    name       text not null check (length(name) between 1 and 60),
    share_id   text unique,  -- null = private; random token when shared
    created_at timestamptz not null default now(),
    unique (user_id, name)
);
create table if not exists folder_papers (
    folder_id  bigint not null references folders (id) on delete cascade,
    paper_id   text not null references papers (id) on delete cascade,
    added_at   timestamptz not null default now(),
    primary key (folder_id, paper_id)
);
alter table folders enable row level security;
alter table folder_papers enable row level security;
```

- A paper can be in several folders (like GitHub Lists). Only saved papers go in
  folders: adding requires an existing save event for that user and paper.
- Removing a save (`DELETE /events?kind=save`) also deletes that paper from the user's
  folders, in the same handler.
- Saved papers are never pruned (`prune()` keeps papers with save events), so folder
  rows never lose their paper while it's saved.
- Limits: 50 folders per user (409 past that), name 1–60 chars, trimmed.
- `share_id`: `secrets.token_urlsafe(9)` (12 chars). Turning sharing off sets it to
  null, so old links die; turning it on again makes a new token.

### Folders: API (all `Me`-authenticated, `write_limit`/`read_limit` as for saves)

- `GET /folders` → `[{id, name, shared: share_id or null, paper_ids: [...]}]`
- `POST /folders {name}` → 201 the folder. 409 on duplicate name or over the cap.
- `DELETE /folders/{id}` → 204. Papers stay saved.
- `PUT /folders/{id}/papers/{paper_id}` → 204, idempotent. 404 if folder isn't the
  user's or paper isn't saved by them.
- `DELETE /folders/{id}/papers/{paper_id}` → 204.
- `POST /folders/{id}/share` → `{share_id}` (creates one if null).
  `DELETE /folders/{id}/share` → 204, sets null.
- Every query filters by `user_id = me`, so ids from other users 404.
- `/me/export` gains `"folders": [{name, shared, created_at, papers: [ids]}]`.
  `DELETE /me` already cascades.
- `PERSONAL` (no-store cache paths) gains `/folders`.

### Folders: shared page

- `GET /f/{share_id}`, `read_limit`, server-rendered with `_page()`: folder name as
  h1, a lede "N papers, shared from xoult", then the same `<li>` markup as the daily
  pages (title → arXiv, byline, TLDR with AI label), and the existing
  "Get them ranked for you, free →" call to action. 404 for unknown or revoked tokens.
- Never shows anything about the owner.
- `<meta name="robots" content="noindex">` and not in the sitemap: a shared link is
  unlisted, not published. `_page()` needs a way to add that meta tag (e.g. an optional
  `robots` placeholder in `papers.html`, empty for existing pages).
- `Cache-Control: no-store` rather than the hour `_page()` sets, so revoking sharing
  takes effect at once (Cloudflare sits in front).

### Folders: UI (Saved page, `renderSaved`)

- A row above the list: `All (n)`, one button per folder with its count, `+ New folder`
  (inline text input, Enter to create). Picking one filters the list client-side; the
  existing text filter keeps working within it.
- Each saved entry gets a `Folders` control: a native `<details>` with one checkbox per
  folder. Ticking calls PUT/DELETE and updates counts.
- With a folder selected: `Share link` (creates the token, then the same share-sheet /
  clipboard path as the paper Share button, with the `/f/...` URL), `Stop sharing` when
  shared, and `Delete folder` behind `confirm()`.
- Folders load with `/saved` (two requests in parallel) and live in `savedCache`.

### Folders: signing in to an existing account (builder's call)

`google_sign_in` folds an anonymous user's events into an existing account and then
deletes the anonymous user, which would cascade-delete their folders (and already
drops their follows today). In the same transaction, before that delete:

- Move each anonymous folder to the account. If the account already has a folder with
  that name, move its papers into that folder (`on conflict do nothing`) and drop the
  anonymous one; its share token dies, the account folder's token is kept.
- The 50-folder cap only blocks creating folders; a merge may exceed it.
- Move follows too (`insert ... select ... on conflict do nothing`), fixing the
  existing loss.
- Test: anonymous user with a shared folder signs in to an existing account; the
  folder, its papers and its link survive.

### Hugging Face signals

- Schema: `alter table papers add column if not exists hf_upvotes int;` and
  `add column if not exists code_url text;`.
- New `ingest.hf_signals()`, run after `store_new_papers()` in every full run: for each
  of the last 7 days, `GET https://huggingface.co/api/daily_papers?date=YYYY-MM-DD&limit=100`
  (7 requests; anonymous limit is 500 per 5 minutes). For each item whose `paper.id` is
  in `papers`, set `hf_upvotes = paper.upvotes` and `code_url = paper.githubRepo` if
  present, else `paper.projectPage`, else unchanged. Upvotes grow for days after
  listing, hence re-reading a week.
- Any HTTP or parse error: log a warning and return. Never raise into `main()`.
- The endpoint is undocumented (not in HF's OpenAPI spec); verified live on
  2026-11-07, returning ~31 papers for 2026-11-06 with `upvotes`, `projectPage`,
  sometimes `githubRepo`. If it changes, the step logs and does nothing.
- API: `Paper` gains `hf_upvotes: int | None` and `code_url: str | None`; every query
  building `Paper`/`FeedItem` selects them (feed in `rank.py`, `/saved`, saves).
- UI, in `entry()` links: `▲ 88 on Hugging Face` linking to
  `https://huggingface.co/papers/{id}`, and `Code` linking to `code_url`, each only when
  present. `code_url` comes from a third party: render only `https://` URLs, as an
  `<a rel="noopener noreferrer">`. Daily pages show the same two links in `.meta`.
- Footer credit: "Upvotes and code links from Hugging Face."

### Bluesky posts

- New module `src/xoult/bluesky.py`, called at the end of every full ingest run (after
  TLDRs and prune). It posts for the newest announcement day when both hold (builder's
  call):
  - that day isn't in `bluesky_posts` yet, and
  - at least 90% of that day's papers have TLDRs.
  In practice the 07:00 run writes 300 TLDRs, short of a ~600-700 paper day, and the
  14:00 UTC run (9:00 US Eastern, 19:30 IST) completes it, so posts usually go out from
  the afternoon run (measured 2026-11-06: 684 of 691 category listings had TLDRs by
  evening). A morning crash or a re-run then can't skip or double a day. A day that
  never reaches 90% (LLM quota gone) isn't posted, which is the right failure.
- Marker table, not user data, so export and delete don't touch it:
  `create table if not exists bluesky_posts (day date primary key, uri text not null,
  created_at timestamptz not null default now());` with RLS enabled. The row is
  inserted after a successful post. The `ingest` concurrency group already stops two
  full runs overlapping.
- Needs `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD` (settings in `config.py`, GitHub
  secrets passed only to the Ingest step). Missing → skip silently.
- AT Protocol over plain `httpx`, no SDK: `com.atproto.server.createSession`, then
  `com.atproto.repo.createRecord` with an `app.bsky.feed.post` record holding text and an `app.bsky.embed.external` card (uri, title, description; no
  thumbnail upload).
- What gets posted (decided by the user: an LLM picks interesting papers and writes a
  catchy post in Bluesky's register). Details below are the builder's calls:
  - **Candidates** from the newest announcement day, deduped, at most ~25: the top 10
    by `hf_upvotes`, the top 8 by xoult saves that day, and 8 cs.HC papers (newest
    first) so HCI is always in the running. Each candidate carries id, title, TLDR,
    primary category and upvotes.
  - **One LLM call** through `llm.complete()` with Gemini first
    (`gemini/gemini-3.5-flash`, then `-flash-lite`, then the rest of `llm_models` as
    fallback). Prompt version `bsky-v1`, traced in Langfuse like TLDRs. It returns JSON:
    a root post (a hook about the day) and 3 picks, each `{id, text}`.
  - **Shape:** a thread. The root post's text plus a link card to `/`; one reply per pick,
    its text plus a link card to `https://arxiv.org/abs/{id}`. Links ride in cards, so
    they don't count against the text.
  - **Voice:** written for researchers on Bluesky: conversational, specific, a little
    playful; at most one emoji and one hashtag per post; no hype words ("groundbreaking",
    "revolutionary"); claims only from the title and TLDR, never invented results.
    Titles and TLDRs go in as data, not instructions.
  - **Validation before posting:** valid JSON; every pick `id` is in the candidate list
    (no hallucinated papers); each text ≤ 280 graphemes (headroom under 300). On any
    failure retry once, then fall back to a plain template ("N new arXiv papers in ML
    and HCI today, each with a one-line TLDR" plus the top-upvoted titles).
  - **Credit:** the root post names nothing about users. Picks never use reader data
    beyond aggregate save counts.
- Errors: log and continue; the run still succeeds.

## Testing

- DB-backed tests in the existing style (`tests/test_api.py`, which skip without a
  local Postgres): folder CRUD, user isolation (another user's folder id → 404),
  adding an unsaved paper → 404, unsave removes from folders, cap, export includes
  folders, shared page renders and 404s after revoke, shared page has noindex.
- `hf_signals` parsing against a small fixture JSON; a failing HTTP call doesn't raise.
- Bluesky: the post-building function (text length ≤ 280 graphemes, card attached) tested without the network.
- Run the DB tests against the local Docker Postgres (port 5433) before handing over.

## What the user provides

- A Bluesky account for xoult and an app password (Settings → Privacy and security →
  App passwords), added as repository secrets `BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD`.
- Nothing for Hugging Face.

## Left to the builder

Exact copy, CSS, button placement within the existing styles, test layout, and how the
three pieces are split into commits (commits only when the user asks).
