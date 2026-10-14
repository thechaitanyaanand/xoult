from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import numpy as np
from pgvector import Vector
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
    embedding: Vector | None = None
    because: str | None = None  # title of the saved paper this one is closest to


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


def explain(items: list[Candidate], saved_titles: list[str], saved: np.ndarray) -> None:
    """Point each recommendation at the saved paper it is closest to.

    The profile is an average, so it can't say why a paper ranked; the nearest saved
    paper is a reason people can check at a glance ("because you saved X").
    """
    explained = [c for c in items if c.embedding is not None]
    if len(saved) == 0 or not explained:
        return
    vectors = np.stack([c.embedding.to_numpy() for c in explained])
    for c, i in zip(explained, (vectors @ saved.T).argmax(axis=1), strict=True):
        c.because = saved_titles[i]


SEEN = "select paper_id from events where user_id = %(user)s and kind in ('save', 'dismiss')"

BY_SIMILARITY = f"""
select id, title, tldr, abstract, authors, categories, published_at, embedding,
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
select id, title, tldr, abstract, authors, categories, published_at, embedding,
       0.0 as similarity
from papers
where categories && %(categories)s
  and published_at > now() - make_interval(days => %(days)s)
  and id not in ({SEEN})
order by published_at desc
limit %(k)s
"""


def _feedback(conn: Connection, user_id: UUID, kind: str) -> tuple[list[str], np.ndarray]:
    # Distinct papers, so saving something twice doesn't count it twice.
    rows = conn.execute(
        "select title, embedding from papers where embedding is not null and id in ("
        "  select paper_id from events where user_id = %s and kind = %s"
        "  order by created_at desc limit 200)",
        (user_id, kind),
    ).fetchall()
    return [r[0] for r in rows], np.array([r[1].to_numpy() for r in rows])


def feed(conn: Connection, user_id: UUID, categories: list[str], limit: int) -> list[Candidate]:
    saved_titles, saved = _feedback(conn, user_id, "save")
    _, dismissed = _feedback(conn, user_id, "dismiss")
    profile = profile_vector(saved, dismissed)
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
    items = rerank(candidates, set(categories), datetime.now(UTC))[:limit]
    explain(items, saved_titles, saved)
    return items
