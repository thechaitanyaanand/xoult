import threading
import time
from collections import Counter
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

from xoult import rank
from xoult.config import DEV_SECRET, settings
from xoult.db import get_pool

if settings.cookie_secure and (settings.secret_key == DEV_SECRET or len(settings.secret_key) < 32):
    # A known or guessable key would let anyone forge a session for any user id.
    raise RuntimeError("Set SECRET_KEY to 32+ random characters when COOKIE_SECURE=true")

if settings.sentry_dsn:
    sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.1, send_default_pii=False)

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

PERSONAL = ("/feed", "/saved", "/me", "/events", "/users", "/auth/", "/config")

# The interactive API docs are for local development; production doesn't advertise its API.
docs = (
    {} if not settings.cookie_secure else {"docs_url": None, "redoc_url": None, "openapi_url": None}
)
app = FastAPI(title="xoult", **docs)
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
LIMITS = (signup_limit, signup_global_limit, auth_limit, read_limit, write_limit)

Category = Annotated[str, StringConstraints(pattern=r"^[a-z-]+(\.[A-Za-z-]+)?$", max_length=20)]


class Categories(BaseModel):
    categories: list[Category] = Field(min_length=1, max_length=10)


class Event(BaseModel):
    paper_id: str = Field(max_length=32)
    kind: Literal["click", "save", "dismiss"]  # impressions are only logged server-side


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
    private = ["/feed", "/saved", "/me", "/events", "/users", "/auth/"]
    lines = ["User-agent: *", "Allow: /$", *(f"Disallow: {p}" for p in private)]
    return PlainTextResponse(
        "\n".join([*lines, "", f"Sitemap: {_origin(request)}/sitemap.xml", ""])
    )


def _days(conn, category: str) -> list[date]:
    """Announcement days still worth a page for a category, newest first.

    Older days have been pruned down to the papers someone saved, which would make
    thin pages, so they 404 and drop out of the index.
    """
    return [
        d
        for (d,) in conn.execute(
            "select distinct (published_at at time zone 'UTC')::date from papers "
            "where %s = any(categories) and published_at > now() - make_interval(days => %s) "
            "order by 1 desc",
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
            f"<p>{escape(summary)}</p></li>"
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
                "where published_at = (select max(published_at) from papers) group by c"
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


@app.get("/me")
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
    data = {
        "user": {**user.model_dump(mode="json"), "created_at": created.isoformat()},
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
            "select published_at, count(*) from papers "
            "group by published_at order by published_at desc limit 1"
        ).fetchone() or (None, 0)
        _latest = (time.monotonic() + 300, value)
    return value


@app.get("/feed", dependencies=[Depends(read_limit)])
def get_feed(user: Me, limit: Annotated[int, Query(ge=1, le=50)] = 20) -> Feed:
    with get_pool().connection() as conn:
        items = rank.feed(conn, user.id, user.categories, limit)
        # One statement for all impressions: executemany would be a round trip per row.
        conn.execute(
            "insert into events (user_id, paper_id, kind, ranker) "
            "select %s, unnest(%s::text[]), 'impression', %s",
            (user.id, [item.id for item in items], rank.RANKER),
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
