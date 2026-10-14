from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import numpy as np
from psycopg import Connection
from psycopg.rows import class_row

RANKER = "v1-linear"
WINDOW_DAYS = 7
CANDIDATES = 200


@dataclass
class Candidate:
    id: str
    title: str
    tldr: str | None
    abstract: str
    authors: list[str]
    categories: list[str]
    published_at: datetime
    similarity: float


@dataclass(frozen=True)
class Weights:
    similarity: float = 0.6
    recency: float = 0.25
    category: float = 0.15
    half_life_days: float = 3.0


DEFAULT_WEIGHTS = Weights()


def profile_vector(
    saved: np.ndarray, dismissed: np.ndarray, beta: float = 0.5
) -> np.ndarray | None:
    """Rocchio: move toward what the user saved and away from what they dismissed."""
    if len(saved) == 0:
        return None
    v = saved.mean(axis=0)
    if len(dismissed):
        v = v - beta * dismissed.mean(axis=0)
    return v / np.linalg.norm(v)


def rerank(
    candidates: list[Candidate], categories: set[str], now: datetime, w: Weights = DEFAULT_WEIGHTS
) -> list[Candidate]:
    if not candidates:
        return []
    sim = np.array([c.similarity for c in candidates])
    # Each embedding model has its own cosine range (bge-small bunches near 0.8, MiniLM
    # spreads over roughly -0.1..0.4), so raw values would make the weights depend on the
    # model. Rescale within the candidate set so they mean what they say.
    spread = np.ptp(sim)
    sim = (sim - sim.min()) / spread if spread > 0 else np.zeros_like(sim)
    age_days = np.array([(now - c.published_at).total_seconds() / 86400 for c in candidates])
    recency = 0.5 ** (np.clip(age_days, 0, None) / w.half_life_days)
    category = np.array([float(bool(categories.intersection(c.categories))) for c in candidates])

    score = w.similarity * sim + w.recency * recency + w.category * category
    return [candidates[i] for i in np.argsort(-score, kind="stable")]


SEEN = "select paper_id from events where user_id = %(user)s and kind in ('save', 'dismiss')"

BY_SIMILARITY = f"""
select id, title, tldr, abstract, authors, categories, published_at,
       1 - (embedding <=> %(profile)s) as similarity
from papers
where embedding is not null
  and published_at > now() - make_interval(days => %(days)s)
  and id not in ({SEEN})
order by embedding <=> %(profile)s
limit %(k)s
"""

# Cold start: nothing saved yet, so fall back to the newest papers in picked categories.
BY_CATEGORY = f"""
select id, title, tldr, abstract, authors, categories, published_at, 0.0 as similarity
from papers
where categories && %(categories)s
  and published_at > now() - make_interval(days => %(days)s)
  and id not in ({SEEN})
order by published_at desc
limit %(k)s
"""


def _embeddings(conn: Connection, user_id: UUID, kind: str) -> np.ndarray:
    rows = conn.execute(
        "select p.embedding from events e join papers p on p.id = e.paper_id "
        "where e.user_id = %s and e.kind = %s and p.embedding is not null "
        "order by e.created_at desc limit 200",
        (user_id, kind),
    ).fetchall()
    return np.array([r[0].to_numpy() for r in rows])


def feed(conn: Connection, user_id: UUID, categories: list[str], limit: int) -> list[Candidate]:
    profile = profile_vector(
        _embeddings(conn, user_id, "save"), _embeddings(conn, user_id, "dismiss")
    )
    params = {
        "user": user_id,
        "profile": profile,
        "categories": categories,
        "days": WINDOW_DAYS,
        "k": CANDIDATES,
    }
    with conn.transaction():
        # HNSW only looks at ef_search neighbours (default 40) before the WHERE filter
        # runs, which would silently cap the candidate pool far below CANDIDATES.
        conn.execute(f"set local hnsw.ef_search = {CANDIDATES * 2}")
        with conn.cursor(row_factory=class_row(Candidate)) as cur:
            cur.execute(BY_SIMILARITY if profile is not None else BY_CATEGORY, params)
            candidates = cur.fetchall()
    return rerank(candidates, set(categories), datetime.now(UTC))[:limit]
