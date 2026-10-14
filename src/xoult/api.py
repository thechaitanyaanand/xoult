from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import psycopg
import sentry_sdk
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StringConstraints

from xoult import rank
from xoult.config import settings
from xoult.db import get_pool

if settings.sentry_dsn:
    sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.1)

app = FastAPI(title="xoult")
INDEX = Path(__file__).parent / "static" / "index.html"

Category = Annotated[str, StringConstraints(pattern=r"^[a-z-]+(\.[A-Za-z-]+)?$", max_length=20)]


class NewUser(BaseModel):
    categories: list[Category] = Field(min_length=1, max_length=10)


class Event(BaseModel):
    user_id: UUID
    paper_id: str = Field(max_length=32)
    kind: Literal["click", "save", "dismiss"]  # impressions are only logged server-side


class FeedItem(BaseModel):
    id: str
    title: str
    tldr: str | None
    abstract: str
    authors: list[str]
    categories: list[str]
    published_at: datetime


class Feed(BaseModel):
    ranker: str
    items: list[FeedItem]


# Endpoints are sync on purpose: psycopg calls block, and FastAPI runs sync handlers
# in a threadpool, which is plenty for this load and simpler than async drivers.


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(INDEX)


@app.get("/health")
def health() -> dict:
    with get_pool().connection() as conn:
        conn.execute("select 1")
    return {"status": "ok"}


@app.get("/categories")
def categories() -> list[str]:
    return settings.arxiv_categories


@app.post("/users", status_code=201)
def create_user(body: NewUser) -> dict:
    with get_pool().connection() as conn:
        (user_id,) = conn.execute(
            "insert into users (categories) values (%s) returning id", (body.categories,)
        ).fetchone()
    return {"id": user_id}


@app.get("/feed")
def get_feed(user_id: UUID, limit: Annotated[int, Query(ge=1, le=50)] = 20) -> Feed:
    with get_pool().connection() as conn:
        row = conn.execute("select categories from users where id = %s", (user_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "unknown user")
        items = rank.feed(conn, user_id, row[0], limit)
        with conn.cursor() as cur:
            cur.executemany(
                "insert into events (user_id, paper_id, kind, ranker) "
                "values (%s, %s, 'impression', %s)",
                [(user_id, item.id, rank.RANKER) for item in items],
            )
    return Feed(ranker=rank.RANKER, items=[FeedItem(**vars(item)) for item in items])


@app.post("/events", status_code=204)
def log_event(event: Event) -> None:
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
                {"user": event.user_id, "paper": event.paper_id, "kind": event.kind},
            )
    except psycopg.errors.ForeignKeyViolation:
        raise HTTPException(404, "unknown user or paper") from None


@app.get("/stats")
def stats() -> list[dict]:
    """Online metrics per ranker over the last 14 days. Public on purpose."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            select ranker,
                   count(distinct user_id) as users,
                   count(*) filter (where kind = 'impression') as impressions,
                   count(*) filter (where kind = 'click') as clicks,
                   count(*) filter (where kind = 'save') as saves,
                   count(*) filter (where kind = 'dismiss') as dismisses
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
