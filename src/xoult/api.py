import json
import logging
import threading
import time
import urllib.request
from collections import Counter
from contextlib import asynccontextmanager
from datetime import date, datetime
from html import escape
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import psycopg
import sentry_sdk
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from pydantic import BaseModel, Field, StringConstraints
from starlette.middleware.sessions import SessionMiddleware

from xoult import arxiv, rank
from xoult.config import DEV_SECRET, settings
from xoult.db import apply_schema, get_pool

if settings.cookie_secure and (settings.secret_key == DEV_SECRET or len(settings.secret_key) < 32):
    # A known or guessable key would let anyone forge a session for any user id.
    raise RuntimeError("Set SECRET_KEY to 32+ random characters when COOKIE_SECURE=true")

if settings.sentry_dsn:
    sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.1, send_default_pii=False)

log = logging.getLogger("xoult.api")
STATIC = Path(__file__).parent / "static"
CATEGORY_NAMES = {
    "cs.AI": "Artificial Intelligence",
    "cs.CL": "Computation and Language",
    "cs.CV": "Computer Vision",
    "cs.IR": "Information Retrieval",
    "cs.LG": "Machine Learning",
    "stat.ML": "Machine Learning (Statistics)",
}

# Google's sign-in button is the only third-party script, and the only other origin.
CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' https://accounts.google.com/gsi/client",
        "style-src 'self' 'unsafe-inline' https://accounts.google.com/gsi/style",
        "font-src 'self'",
        "frame-src https://accounts.google.com/gsi/",
        "connect-src 'self' https://accounts.google.com/gsi/",
        "img-src 'self' data:",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
    ]
)

PERSONAL = ("/feed", "/saved", "/me", "/events", "/users", "/auth/", "/config", "/arxiv")

# The interactive API docs are for local development; production doesn't advertise its API.
docs = (
    {} if not settings.cookie_secure else {"docs_url": None, "redoc_url": None, "openapi_url": None}
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # db/schema.sql is idempotent. Applying it here too, not only in the ingest job, means
    # a deploy that adds a table can't serve code querying it before the table exists.
    apply_schema()
    yield


app = FastAPI(title="xoult", lifespan=lifespan, **docs)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="xoult_session",
    max_age=400 * 24 * 3600,  # the longest browsers keep a cookie
    same_site="lax",  # not sent on cross-site POST/DELETE, which is the CSRF defence
    https_only=settings.cookie_secure,
)
app.add_middleware(GZipMiddleware, minimum_size=1024)  # the feed's ~40 KB of JSON shrinks ~4×
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"  # Google popup
    if not request.url.path.startswith(("/docs", "/redoc")):  # Swagger UI loads from a CDN
        response.headers["Content-Security-Policy"] = CSP
    if request.url.path.startswith(PERSONAL):
        # Someone's feed, saves or account must never sit in a proxy or a shared disk cache.
        response.headers["Cache-Control"] = "no-store"
    if settings.cookie_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


def client_ip(request: Request) -> str:
    # Render sits behind Cloudflare, which sets CF-Connecting-IP to the address it saw.
    # X-Forwarded-For (what request.client reflects) keeps whatever the client sent
    # first, so keying on it would let anyone reset their limits by inventing a header.
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "unknown"
    )


class RateLimit:
    """At most `limit` requests per fixed window, per client IP or for everyone together.

    Kept in process memory, which is right for the single free instance. With several
    instances each would count separately; move the counter to Redis at that point.
    """

    def __init__(self, limit: int, seconds: int = 60, per_ip: bool = True):
        self.limit, self.seconds, self.per_ip = limit, seconds, per_ip
        self.window = 0
        self.counts: Counter[str] = Counter()
        self.lock = threading.Lock()

    def __call__(self, request: Request) -> None:
        key = client_ip(request) if self.per_ip else "*"
        with self.lock:
            window = int(time.time() // self.seconds)
            if window != self.window:
                self.window, self.counts = window, Counter()
            self.counts[key] += 1
            over = self.counts[key] > self.limit
        if over:
            raise HTTPException(
                429, "Too many requests. Try again shortly.", {"Retry-After": str(self.seconds)}
            )


signup_limit = RateLimit(5)
# Bots spread over many IPs slip past per-IP limits; this bounds how fast anonymous
# profiles can fill the database, whoever creates them.
signup_global_limit = RateLimit(300, seconds=3600, per_ip=False)
auth_limit = RateLimit(10)
read_limit = RateLimit(60)
write_limit = RateLimit(120)
# arXiv asks API clients for at most one request every three seconds, and everyone here
# shares one server address.
arxiv_limit = RateLimit(10)
arxiv_global_limit = RateLimit(20, per_ip=False)
# Saved papers are never pruned, so hand-added ones are permanent rows. Without a cap,
# one session could fill the free 500 MB database (where every write fails) in days:
# 1,500 a day is ~9 MB, months of headroom; a researcher's import brings ~20 at once.
# ponytail: counted in memory, so a restart resets it; it bounds a day of abuse, not a
# week. Count `papers.ingested_at` of hand-added rows in SQL if that ever matters.
new_papers_daily_limit = RateLimit(1500, seconds=86400, per_ip=False)
MAX_SAVED = 1000
MAX_FOLLOWS = 200
IMPORT_SIZE = 20  # someone's most recent preprints: enough to describe their work
LIMITS = (
    signup_limit,
    signup_global_limit,
    auth_limit,
    read_limit,
    write_limit,
    arxiv_limit,
    arxiv_global_limit,
    new_papers_daily_limit,
)

Category = Annotated[str, StringConstraints(pattern=r"^[a-z-]+(\.[A-Za-z-]+)?$", max_length=20)]


class Categories(BaseModel):
    categories: list[Category] = Field(min_length=1, max_length=10)


class Event(BaseModel):
    paper_id: str = Field(max_length=32)
    kind: Literal["click", "save", "dismiss"]  # impressions are only logged server-side


class AddPaper(BaseModel):
    paper_id: str = Field(max_length=300)  # an id or a pasted link


class AddPapers(BaseModel):
    paper_ids: list[Annotated[str, StringConstraints(max_length=40)]] = Field(
        min_length=1, max_length=IMPORT_SIZE
    )


class Follow(BaseModel):
    author: str = Field(min_length=1, max_length=200)


class GoogleCredential(BaseModel):
    credential: str = Field(max_length=4096)


class Paper(BaseModel):
    id: str
    title: str
    tldr: str | None
    abstract: str
    authors: list[str]
    categories: list[str]
    published_at: datetime


class FeedItem(Paper):
    because: str | None = None
    followed: str | None = None  # set on picks shown because their author is followed


class SavedPaper(Paper):
    new: bool  # false if it was already in the reader's library


class Feed(BaseModel):
    ranker: str
    latest_date: datetime | None  # most recent arXiv announcement in the database
    latest_count: int
    items: list[FeedItem]


class User(BaseModel):
    id: UUID
    categories: list[str]
    email: str | None
    signed_in: bool


def current_user(request: Request) -> User:
    uid = request.session.get("uid")
    if uid:
        # Look the user up and mark them active in one round trip.
        with get_pool().connection() as conn:
            row = conn.execute(
                "update users set last_seen_at = now() where id = %s "
                "returning id, categories, email, google_sub is not null",
                (uid,),
            ).fetchone()
        if row:
            return User(id=row[0], categories=row[1], email=row[2], signed_in=row[3])
        request.session.clear()  # the account was deleted
    raise HTTPException(401, "No session. Pick categories to start.")


Me = Annotated[User, Depends(current_user)]

# Endpoints are sync on purpose: psycopg calls block, and FastAPI runs sync handlers
# in a threadpool, which is plenty for this load and simpler than async drivers.


def _origin(request: Request) -> str:
    return (settings.public_url or str(request.base_url)).rstrip("/")


@app.get("/", include_in_schema=False)
def index(request: Request) -> HTMLResponse:
    # Absolute URLs for link previews (og:url, og:image) need the public origin.
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(page.replace("{{origin}}", _origin(request)))


@app.get("/robots.txt", include_in_schema=False)
def robots(request: Request) -> PlainTextResponse:
    # Search and AI crawlers get the landing page and llms.txt; the per-user API is
    # useless to them and only spends their crawl budget and our rate limits.
    private = ["/feed", "/saved", "/me", "/events", "/users", "/auth/", "/arxiv"]
    lines = ["User-agent: *", "Allow: /$", *(f"Disallow: {p}" for p in private)]
    return PlainTextResponse(
        "\n".join([*lines, "", f"Sitemap: {_origin(request)}/sitemap.xml", ""])
    )


# arXiv announces a batch under one timestamp, hundreds of papers each weekday. Papers
# readers add by hand carry their own submission times, so "the newest timestamp" alone
# could be a single hand-added preprint; a batch is a timestamp many papers share.
BATCH = 5
LATEST_BATCH = (
    "(select published_at from papers group by published_at "
    f"having count(*) > {BATCH} order by published_at desc limit 1)"
)


def _days(conn, category: str) -> list[date]:
    """Announcement days still worth a page for a category, newest first.

    Older days have been pruned down to the papers someone saved, which would make
    thin pages, so they 404 and drop out of the index.
    """
    return [
        d
        for (d,) in conn.execute(
            "select (published_at at time zone 'UTC')::date from papers "
            "where %s = any(categories) and published_at > now() - make_interval(days => %s) "
            f"group by 1 having count(*) > {BATCH} order by 1 desc",
            (category, settings.keep_papers_days),
        ).fetchall()
    ]


@app.get("/sitemap.xml", include_in_schema=False, dependencies=[Depends(read_limit)])
def sitemap(request: Request) -> Response:
    origin = _origin(request)
    urls = [f"<url><loc>{origin}/</loc><changefreq>daily</changefreq></url>"]
    with get_pool().connection() as conn:
        for c in settings.arxiv_categories:
            days = _days(conn, c)
            if days:
                urls.append(
                    f"<url><loc>{origin}/papers/{c}</loc><lastmod>{days[0]}</lastmod>"
                    "<changefreq>daily</changefreq></url>"
                )
            urls += [
                f"<url><loc>{origin}/papers/{c}/{d}</loc><lastmod>{d}</lastmod></url>" for d in days
            ]
    urls += [f"<url><loc>{origin}/privacy</loc></url>", f"<url><loc>{origin}/llms.txt</loc></url>"]
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  {u}\n" for u in urls)
        + "</urlset>\n"
    )
    return Response(xml, media_type="application/xml")


# TLDRs are written by an LLM from the abstract and can be wrong; readers should know.
AI_LABEL = '<span class="ai">AI summary</span>'


@app.get("/papers/{category}", include_in_schema=False, dependencies=[Depends(read_limit)])
@app.get("/papers/{category}/{day}", include_in_schema=False, dependencies=[Depends(read_limit)])
def papers_page(request: Request, category: str, day: date | None = None) -> HTMLResponse:
    """One category's papers for one day, as plain HTML that search engines can index.

    The bare category URL shows the newest day, so it can rank for "new cs.CL papers".
    """
    # ponytail: two queries per crawl hit, uncached; add a cache keyed on (category, day)
    # if crawlers ever show up in database load.
    if category not in settings.arxiv_categories:
        raise HTTPException(404, "Unknown category")
    with get_pool().connection() as conn:
        days = _days(conn, category)
        shown = day or (days[0] if days else None)
        if shown not in days:
            raise HTTPException(404, "No papers for that day")
        rows = conn.execute(
            "select id, title, tldr, abstract, authors from papers "
            "where %s = any(categories) and (published_at at time zone 'UTC')::date = %s "
            "order by id",
            (category, shown),
        ).fetchall()

    origin, name = _origin(request), CATEGORY_NAMES.get(category, category)
    when = f"{shown:%A} {shown.day} {shown:%B %Y}"
    title = f"New {category} papers on arXiv, {shown.day} {shown:%b %Y}: {name} | xoult"
    description = (
        f"All {len(rows)} new arXiv {category} ({name}) papers announced on {when}, "
        "each with a one-line TLDR."
    )
    items = []
    for pid, ptitle, tldr, abstract, authors in rows:
        summary = tldr or (
            abstract[:280].rsplit(" ", 1)[0] + "…" if len(abstract) > 280 else abstract
        )
        byline = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
        items.append(
            f'<li><h2><a href="https://arxiv.org/abs/{escape(pid)}">{escape(ptitle)}</a></h2>'
            f'<p class="meta">{escape(byline)} · arXiv:{escape(pid)}</p>'
            f"<p>{AI_LABEL if tldr else ''}{escape(summary)}</p></li>"
        )
    others = " ".join(
        f'<a href="/papers/{c}">{c}</a>' for c in settings.arxiv_categories if c != category
    )
    archive = " ".join(
        f'<a href="/papers/{category}/{d}">{d:%d %b}</a>' for d in days if d != shown
    )
    body = (
        f"<h1>New {escape(name)} ({category}) papers, {when}</h1>"
        f'<p class="lede">{len(rows)} papers announced on arXiv. '
        f'<a class="cta" href="/">Get them ranked for you, free →</a></p>'
        f"<ol>{''.join(items)}</ol>"
        f"<nav>Earlier days: {archive or 'none yet'}</nav>"
        f"<nav>Other categories: {others}</nav>"
    )
    path = f"/papers/{category}" + (f"/{day}" if day else "")
    return _page(origin, path, title, description, body)  # body escaped piece by piece above


def _page(origin: str, path: str, title: str, description: str, body: str) -> HTMLResponse:
    """A plain server-rendered page in the papers.html shell. `body` must be safe HTML."""
    page = (STATIC / "papers.html").read_text(encoding="utf-8")
    for key, value in {
        "title": escape(title),
        "description": escape(description),
        "canonical": origin + path,
        "origin": origin,
        "body": body,
    }.items():
        page = page.replace("{{" + key + "}}", value)
    return HTMLResponse(page, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/privacy", include_in_schema=False)
def privacy(request: Request) -> HTMLResponse:
    return _page(
        _origin(request),
        "/privacy",
        "Privacy | xoult",
        "What xoult stores about readers, why, how long, and how to download or delete it.",
        (STATIC / "privacy.html").read_text(encoding="utf-8"),
    )


@app.get("/googlebf62e30f7f3ae670.html", include_in_schema=False)
def google_verification() -> HTMLResponse:
    # Proves ownership to Google Search Console; keep it, or the property unverifies.
    return HTMLResponse("google-site-verification: googlebf62e30f7f3ae670.html")


@app.get("/llms.txt", include_in_schema=False)
def llms_txt(request: Request) -> PlainTextResponse:
    """A plain-language summary for AI assistants and AI search (llmstxt.org)."""
    categories = ", ".join(f"{c} ({CATEGORY_NAMES.get(c, c)})" for c in settings.arxiv_categories)
    origin = _origin(request)
    text = f"""# xoult

> A free, personalized daily feed of new arXiv papers in machine learning. Each weekday it
> ranks the new announcements for every reader from what they save and dismiss, and gives
> each paper a one-line TLDR.

xoult covers {categories}. That is about 1,900 new papers a weekday.

- No account is needed to start: readers pick categories and get a ranked list at once.
  Optional Google sign-in keeps saved papers across devices.
- Each recommendation says which saved paper it follows from ("because you saved X").
- TLDRs are written by an LLM from the abstract; the abstract and arXiv link are always shown.
- Readers can download or delete all their data from Settings.

## How the ranking works

- Papers are embedded with all-MiniLM-L6-v2. A reader's profile is the mean of their saved
  papers moved away from dismissed ones (Rocchio). The 200 papers from the past week in
  their categories that are closest to it are reranked by embedding similarity blended
  50/50 with TF-IDF similarity to the saved papers, plus recency and category match.
- Offline evaluation on CiteULike-a (5,551 users' libraries): NDCG@10 of 0.1118 for the
  production ranker, against 0.1005 for embeddings alone and 0.1016 for TF-IDF alone.

## Links

- [Home]({origin}/): the feed
- [Daily list]({origin}/papers/cs.CL): every paper from the newest announcement in one
  category, with TLDRs; /papers/<category> for the others
- [Ranking stats]({origin}/stats): public save rate per ranker version, JSON, last 14 days
"""
    return PlainTextResponse(text)


@app.get("/health")
def health() -> dict:
    with get_pool().connection() as conn:
        conn.execute("select 1")
    return {"status": "ok"}


@app.get("/config", dependencies=[Depends(read_limit)])
def config(request: Request) -> dict:
    """What the page needs on first load, before it knows who the visitor is."""
    with get_pool().connection() as conn:
        counts = dict(
            conn.execute(
                "select c, count(*) from papers, unnest(categories) c "
                f"where published_at = {LATEST_BATCH} group by c"
            ).fetchall()
        )
    return {
        "google_client_id": settings.google_client_id,
        "has_session": "uid" in request.session,  # lets a first visit skip a 401 from /me
        "categories": [
            {"id": c, "name": CATEGORY_NAMES.get(c, c), "daily": counts.get(c, 0)}
            for c in settings.arxiv_categories
        ],
    }


@app.post(
    "/users", status_code=201, dependencies=[Depends(signup_limit), Depends(signup_global_limit)]
)
def create_user(body: Categories, request: Request) -> dict:
    with get_pool().connection() as conn:
        (user_id,) = conn.execute(
            "insert into users (categories) values (%s) returning id", (body.categories,)
        ).fetchone()
    request.session["uid"] = str(user_id)
    return {"id": user_id}


def verify_google_token(credential: str) -> dict:
    # Checks Google's signature, expiry, issuer and that the token was minted for us.
    return id_token.verify_oauth2_token(
        credential, google_requests.Request(), settings.google_client_id
    )


@app.post("/auth/google", dependencies=[Depends(auth_limit)])
def google_sign_in(body: GoogleCredential, request: Request) -> dict:
    if not settings.google_client_id:
        raise HTTPException(404, "Google sign-in is not configured")
    try:
        info = verify_google_token(body.credential)
    except ValueError:
        raise HTTPException(401, "Google sign-in failed. Try again.") from None
    if not info.get("email_verified"):
        raise HTTPException(401, "That Google account has no verified email.")
    sub, email = info["sub"], info["email"]

    current = request.session.get("uid")
    with get_pool().connection() as conn, conn.transaction():
        existing = conn.execute("select id from users where google_sub = %s", (sub,)).fetchone()
        anon = None
        if current:
            anon = conn.execute(
                "select id, categories from users where id = %s and google_sub is null",
                (current,),
            ).fetchone()
        if existing:
            uid = existing[0]
            if anon:
                # Already has an account, signing in on a new device or after browsing
                # anonymously: fold that activity in rather than losing it.
                conn.execute("update events set user_id = %s where user_id = %s", (uid, anon[0]))
                conn.execute("delete from users where id = %s", (anon[0],))
                conn.execute(
                    "update users set categories = %s where id = %s and categories = '{}'",
                    (anon[1], uid),
                )
            conn.execute("update users set email = %s where id = %s", (email, uid))
        elif anon:
            uid = anon[0]
            conn.execute(
                "update users set google_sub = %s, email = %s where id = %s", (sub, email, uid)
            )
        else:
            # New account straight from sign-in; categories are picked next. The upsert
            # covers two sign-ins for the same account racing each other.
            (uid,) = conn.execute(
                "insert into users (google_sub, email) values (%s, %s) "
                "on conflict (google_sub) do update set email = excluded.email returning id",
                (sub, email),
            ).fetchone()
    request.session["uid"] = str(uid)
    return {"id": uid}


@app.post("/auth/logout", status_code=204)
def logout(request: Request) -> None:
    request.session.clear()


@app.get("/me", dependencies=[Depends(read_limit)])
def me(user: Me) -> dict:
    with get_pool().connection() as conn:
        (saved,) = conn.execute(
            "select count(distinct paper_id) from events where user_id = %s and kind = 'save'",
            (user.id,),
        ).fetchone()
    return {**user.model_dump(), "saved": saved}


@app.patch("/me", status_code=204, dependencies=[Depends(write_limit)])
def update_me(body: Categories, user: Me) -> None:
    with get_pool().connection() as conn:
        conn.execute("update users set categories = %s where id = %s", (body.categories, user.id))


@app.get("/me/export", dependencies=[Depends(read_limit)])
def export_me(user: Me) -> JSONResponse:
    """Everything stored about this user, as a download."""
    with get_pool().connection() as conn:
        created = conn.execute("select created_at from users where id = %s", (user.id,)).fetchone()[
            0
        ]
        events = conn.execute(
            "select e.kind, e.paper_id, p.title, e.ranker, e.created_at "
            "from events e join papers p on p.id = e.paper_id "
            "where e.user_id = %s order by e.created_at",
            (user.id,),
        ).fetchall()
        follows = conn.execute(
            "select author, created_at from follows where user_id = %s order by created_at",
            (user.id,),
        ).fetchall()
    data = {
        "user": {**user.model_dump(mode="json"), "created_at": created.isoformat()},
        "follows": [{"author": a, "at": at.isoformat()} for a, at in follows],
        "events": [
            {"kind": k, "paper_id": pid, "title": t, "ranker": r, "at": at.isoformat()}
            for k, pid, t, r, at in events
        ],
    }
    headers = {"Content-Disposition": 'attachment; filename="xoult-data.json"'}
    return JSONResponse(data, headers=headers)


@app.delete("/me", status_code=204, dependencies=[Depends(write_limit)])
def delete_me(user: Me, request: Request) -> None:
    with get_pool().connection() as conn:
        conn.execute("delete from users where id = %s", (user.id,))  # events cascade
    request.session.clear()


_latest: tuple[float, tuple] = (0.0, (None, 0))


def _latest_announcement(conn) -> tuple:
    """(date, paper count) of the newest arXiv batch. Changes only when ingest runs."""
    global _latest
    expires, value = _latest
    if time.monotonic() > expires:
        value = conn.execute(
            f"select published_at, count(*) from papers where published_at = {LATEST_BATCH} "
            "group by published_at"
        ).fetchone() or (None, 0)
        _latest = (time.monotonic() + 300, value)
    return value


@app.get("/feed", dependencies=[Depends(read_limit)])
def get_feed(user: Me, limit: Annotated[int, Query(ge=1, le=50)] = 20) -> Feed:
    with get_pool().connection() as conn:
        items = rank.feed(conn, user.id, user.categories, limit)
        # One statement for all impressions: executemany would be a round trip per row.
        # Picks shown for a followed author log as their own ranker, so /stats compares
        # their save rate with the regular ranking's directly.
        conn.execute(
            "insert into events (user_id, paper_id, kind, ranker) "
            "select %s, i, 'impression', r from unnest(%s::text[], %s::text[]) as t(i, r)",
            (
                user.id,
                [item.id for item in items],
                [rank.FOLLOWS if item.followed else rank.RANKER for item in items],
            ),
        )
        latest = _latest_announcement(conn)
    return Feed(
        ranker=rank.RANKER,
        latest_date=latest[0],
        latest_count=latest[1],
        items=[FeedItem.model_validate(item, from_attributes=True) for item in items],
    )


@app.get("/saved", dependencies=[Depends(read_limit)])
def get_saved(user: Me) -> list[Paper]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            select p.id, p.title, p.tldr, p.abstract, p.authors, p.categories, p.published_at
            from papers p
            join (
                select paper_id, max(created_at) as saved_at from events
                where user_id = %s and kind = 'save' group by paper_id
            ) s on s.paper_id = p.id
            order by s.saved_at desc
            """,
            (user.id,),
        ).fetchall()
    return [Paper(**dict(zip(Paper.model_fields, row, strict=True))) for row in rows]


# A paper only shapes the feed once it has an embedding, and only the ingest job has the
# model (this image stays small enough for 512 MB). So a save wakes that job. The
# `embedding is null` rows are the queue and the dispatch is just a wake-up: if one is
# lost to a restart or a GitHub outage, the next scheduled run embeds the paper anyway.
# Saves within EMBED_DELAY share one run instead of starting one each.
EMBED_DELAY = 60
_embed_timer: threading.Timer | None = None
_embed_lock = threading.Lock()


def request_embedding() -> None:
    global _embed_timer
    if not settings.github_token:
        return
    with _embed_lock:
        if _embed_timer and _embed_timer.is_alive():
            return  # a run is already on its way; it will pick this paper up too
        _embed_timer = threading.Timer(EMBED_DELAY, dispatch_embed)
        _embed_timer.daemon = True
        _embed_timer.start()


def dispatch_embed() -> None:
    url = (
        f"https://api.github.com/repos/{settings.github_repo}"
        "/actions/workflows/ingest.yml/dispatches"
    )
    request = urllib.request.Request(
        url,
        data=json.dumps({"ref": "main", "inputs": {"embed_only": "true"}}).encode(),
        headers={
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        urllib.request.urlopen(request, timeout=10).close()
    except Exception:
        # The save itself already succeeded; the scheduled run is the fallback.
        log.exception("couldn't start the embedding run")


ArxivLimits = [Depends(arxiv_limit), Depends(arxiv_global_limit)]
ARXIV_DOWN = "arXiv isn't answering right now. Try again in a minute."


@app.get("/arxiv", dependencies=ArxivLimits)
def search_arxiv(user: Me, q: Annotated[str, Query(min_length=2, max_length=300)]) -> list[Paper]:
    """Find papers to add by hand: a pasted arXiv link or id, or words from the title."""
    try:
        return [Paper(**p) for p in arxiv.search(q)]
    except arxiv.Unavailable:
        raise HTTPException(503, ARXIV_DOWN) from None


def _save(user: User, request: Request, paper_ids: list[str]) -> list[SavedPaper]:
    """Save arXiv papers, fetching any that are new here in one arXiv request.

    A paper new to the database has no embedding yet, so this wakes the ingest job to
    embed it; it shapes the feed a few minutes later. Papers arXiv doesn't know are
    skipped.
    """
    with get_pool().connection() as conn:
        (saved,) = conn.execute(
            "select count(distinct paper_id) from events where user_id = %s and kind = 'save'",
            (user.id,),
        ).fetchone()
        if saved + len(paper_ids) > MAX_SAVED:
            raise HTTPException(
                409, f"Your library is full ({MAX_SAVED} papers). Remove some first."
            )
        rows = conn.execute(
            "select id, title, tldr, abstract, authors, categories, published_at "
            "from papers where id = any(%s)",
            (paper_ids,),
        ).fetchall()
    papers = {r[0]: Paper(**dict(zip(Paper.model_fields, r, strict=True))) for r in rows}
    missing = [i for i in paper_ids if i not in papers]
    if missing:
        # Only papers that aren't stored yet cost an arXiv request, so they share its
        # limits, and each one counts toward the daily cap on new rows.
        for limit in (arxiv_limit, arxiv_global_limit, *[new_papers_daily_limit] * len(missing)):
            limit(request)
        try:
            papers |= {p["id"]: Paper(**p) for p in arxiv.get_many(missing)}
        except arxiv.Unavailable:
            raise HTTPException(503, ARXIV_DOWN) from None
    found = [papers[i] for i in paper_ids if i in papers]
    if not found:
        raise HTTPException(404, "No arXiv paper with that id")

    out = []
    with get_pool().connection() as conn, conn.transaction():
        for paper in found:
            if paper.id in missing:
                conn.execute(
                    "insert into papers (id, title, abstract, authors, categories, "
                    "published_at) values (%s, %s, %s, %s, %s, %s) on conflict (id) do nothing",
                    (paper.id, paper.title, paper.abstract, paper.authors, paper.categories,
                     paper.published_at),
                )  # fmt: skip
            # Idempotent: the search box on Today can't know what's already saved.
            new = conn.execute(
                "insert into events (user_id, paper_id, kind) "
                "select %(user)s, %(paper)s, 'save' where not exists (select 1 from events "
                "where user_id = %(user)s and paper_id = %(paper)s and kind = 'save')",
                {"user": user.id, "paper": paper.id},
            ).rowcount
            out.append(SavedPaper(**paper.model_dump(), new=bool(new)))
    if set(missing) & {p.id for p in found}:
        request_embedding()
    return out


@app.post("/saved", status_code=201, dependencies=[Depends(write_limit)])
def add_saved(body: AddPaper, user: Me, request: Request) -> SavedPaper:
    """Save any arXiv paper, not just one from the feed."""
    paper_id = arxiv.parse_id(body.paper_id)
    if not paper_id:
        raise HTTPException(422, "Not an arXiv id")
    return _save(user, request, [paper_id])[0]


@app.post("/saved/batch", status_code=201, dependencies=[Depends(write_limit)])
def add_saved_batch(body: AddPapers, user: Me, request: Request) -> list[SavedPaper]:
    """Save several papers at once, e.g. a researcher's own from an import."""
    ids = list(dict.fromkeys(filter(None, map(arxiv.parse_id, body.paper_ids))))
    if not ids:
        raise HTTPException(422, "No arXiv ids")
    return _save(user, request, ids)


INDEX_DOWN = "The paper index isn't answering. Try again in a minute."


# A researcher's own papers are the best description of their interests, and seed the
# feed on day one. Looked up by ORCID iD, or by name with the reader picking themselves
# among same-named authors. Nothing about the reader is stored or sent: only what they
# typed, from this server.
@app.get("/import/authors", dependencies=ArxivLimits)
def import_authors(user: Me, q: Annotated[str, Query(min_length=2, max_length=100)]) -> list[dict]:
    try:
        return arxiv.find_authors(q)
    except arxiv.Unavailable:
        raise HTTPException(503, INDEX_DOWN) from None


@app.get("/import/papers", dependencies=ArxivLimits)
def import_papers(
    user: Me,
    orcid: Annotated[str | None, Query(max_length=60)] = None,
    author: Annotated[str | None, Query(pattern=r"^A\d{1,12}$")] = None,
) -> list[Paper]:
    """Someone's most recent arXiv preprints, to review before saving."""
    orcid = orcid and arxiv.parse_orcid(orcid)
    if not orcid and not author:
        raise HTTPException(422, "Give an ORCID iD or an author")
    try:
        ids = arxiv.authored_arxiv_ids(orcid=orcid, author=author, limit=IMPORT_SIZE)
        with get_pool().connection() as conn:
            rows = conn.execute(
                "select id, title, tldr, abstract, authors, categories, published_at "
                "from papers where id = any(%s)",
                (ids,),
            ).fetchall()
        papers = {r[0]: Paper(**dict(zip(Paper.model_fields, r, strict=True))) for r in rows}
        papers |= {p["id"]: Paper(**p) for p in arxiv.get_many([i for i in ids if i not in papers])}
    except arxiv.Unavailable:
        raise HTTPException(503, INDEX_DOWN) from None
    return [papers[i] for i in ids if i in papers]


@app.get("/authors", dependencies=[Depends(read_limit)])
def authors(user: Me) -> dict:
    """Authors you follow, and ones worth following: in two or more of your saved papers."""
    with get_pool().connection() as conn:
        following = [
            a for (a,) in conn.execute(
                "select author from follows where user_id = %s order by created_at", (user.id,)
            ).fetchall()
        ]  # fmt: skip
        suggested = conn.execute(
            """
            select a, count(distinct p.id) from events e
            join papers p on p.id = e.paper_id, unnest(p.authors) a
            where e.user_id = %s and e.kind = 'save'
            group by a having count(distinct p.id) >= 2
            order by 2 desc, a limit 12
            """,
            (user.id,),
        ).fetchall()
    return {
        "following": following,
        "suggested": [{"name": a, "saved": n} for a, n in suggested if a not in following],
    }


@app.post("/follows", status_code=204, dependencies=[Depends(write_limit)])
def follow(body: Follow, user: Me) -> None:
    with get_pool().connection() as conn:
        (n,) = conn.execute(
            "select count(*) from follows where user_id = %s", (user.id,)
        ).fetchone()
        if n >= MAX_FOLLOWS:
            raise HTTPException(409, f"You follow {MAX_FOLLOWS} authors already.")
        conn.execute(
            "insert into follows (user_id, author) values (%s, %s) on conflict do nothing",
            (user.id, body.author),
        )


@app.delete("/follows", status_code=204, dependencies=[Depends(write_limit)])
def unfollow(user: Me, author: Annotated[str, Query(max_length=200)]) -> None:
    with get_pool().connection() as conn:
        conn.execute("delete from follows where user_id = %s and author = %s", (user.id, author))


@app.post("/events", status_code=204, dependencies=[Depends(write_limit)])
def log_event(event: Event, user: Me) -> None:
    # Attribute the action to whichever ranker last showed this paper to this user.
    try:
        with get_pool().connection() as conn:
            conn.execute(
                """
                insert into events (user_id, paper_id, kind, ranker)
                values (%(user)s, %(paper)s, %(kind)s, (
                    select ranker from events
                    where user_id = %(user)s and paper_id = %(paper)s and kind = 'impression'
                    order by created_at desc limit 1
                ))
                """,
                {"user": user.id, "paper": event.paper_id, "kind": event.kind},
            )
    except psycopg.errors.ForeignKeyViolation:
        raise HTTPException(404, "Unknown paper") from None


@app.delete("/events", status_code=204, dependencies=[Depends(write_limit)])
def undo_event(
    user: Me,
    paper_id: Annotated[str, Query(max_length=32)],
    kind: Literal["save", "dismiss"],
) -> None:
    """Undo a save or dismiss, e.g. a misclick or removing a paper from the saved list.

    The rows are deleted rather than countered with a new event, so the profile and
    the save-rate metric both behave as if the action never happened.
    """
    with get_pool().connection() as conn:
        conn.execute(
            "delete from events where user_id = %s and paper_id = %s and kind = %s",
            (user.id, paper_id, kind),
        )


@app.get("/stats", dependencies=[Depends(read_limit)])
def stats() -> list[dict]:
    """Online metrics per ranker over the last 14 days. Public on purpose."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            select ranker,
                   count(distinct user_id) as users,
                   -- A paper shown to someone five times (each re-rank shows it again)
                   -- is one chance to save it, not five, or save rate reads too low.
                   count(distinct (user_id, paper_id)) filter (where kind = 'impression'),
                   count(distinct (user_id, paper_id)) filter (where kind = 'click'),
                   count(distinct (user_id, paper_id)) filter (where kind = 'save'),
                   count(distinct (user_id, paper_id)) filter (where kind = 'dismiss')
            from events
            where created_at > now() - interval '14 days' and ranker is not null
            group by ranker order by ranker
            """
        ).fetchall()
    out = []
    for ranker, users, impressions, clicks, saves, dismisses in rows:
        out.append(
            {
                "ranker": ranker,
                "users": users,
                "impressions": impressions,
                "ctr": round(clicks / impressions, 4) if impressions else None,
                "save_rate": round(saves / impressions, 4) if impressions else None,
                "dismiss_rate": round(dismisses / impressions, 4) if impressions else None,
            }
        )
    return out
