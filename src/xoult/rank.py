from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import numpy as np
from pgvector import Vector
from psycopg import Connection
from psycopg.rows import class_row

from xoult import lexical

RANKER = "v2-hybrid"
FOLLOWS = "follows"  # the ranker label on picks from followed authors
MAX_FOLLOWED = 3
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
    followed: str | None = None  # the followed author this paper is shown for


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


# Share of TF-IDF in relevance. On CiteULike, reranking the top 200 exactly as below:
# 0.2 → +6.9% NDCG@10, 0.3 → +9.4%, 0.5 → +11.3%, 0.6 → +9.6%, 0.7 → +7.3%.
LEXICAL_WEIGHT = 0.5


def relevance(similarity: np.ndarray, lexical: np.ndarray, weight: float = LEXICAL_WEIGHT):
    """Blend embedding and TF-IDF similarity over one candidate set.

    Each is z-scored within the candidates first: the two live on unrelated scales, and
    without it whichever spreads wider would decide the order alone.
    """

    def z(x: np.ndarray) -> np.ndarray:
        spread = x.std()
        return (x - x.mean()) / spread if spread > 0 else np.zeros_like(x, dtype=float)

    return (1 - weight) * z(similarity) + weight * z(lexical)


def rerank(
    candidates: list[Candidate],
    categories: set[str],
    now: datetime,
    w: Weights = DEFAULT_WEIGHTS,
    relevance: np.ndarray | None = None,
) -> list[Candidate]:
    """Order candidates by relevance (embedding similarity unless given), recency, category."""
    if not candidates:
        return []
    sim = np.array([c.similarity for c in candidates]) if relevance is None else relevance
    # Each embedding model has its own cosine range (bge-small bunches near 0.8, MiniLM
    # spreads over roughly -0.1..0.4), and blended relevance is in z-scores, so raw values
    # would make the weights depend on the scale. Rescale within the candidate set.
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

# Filter first, then rank exactly. A week of papers in someone's categories is a few
# thousand rows, so exact distances take milliseconds; the HNSW index would apply the
# category filter only after its approximate search and silently return too few.
BY_SIMILARITY = f"""
with pool as materialized (
    select id, title, tldr, abstract, authors, categories, published_at, embedding
    from papers
    where embedding is not null
      and categories && %(categories)s
      and published_at > now() - make_interval(days => %(days)s)
      and id not in ({SEEN})
)
select id, title, tldr, abstract, authors, categories, published_at,
       1 - (embedding <=> %(profile)s) as similarity
from pool
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


# New papers by authors the reader follows, from any category: they follow the person,
# not the topic. The newest few lead the feed whatever their similarity score.
FROM_FOLLOWED = f"""
select id, title, tldr, abstract, authors, categories, published_at, 0.0 as similarity,
       (select a from unnest(authors) a where a = any(%(follows)s) limit 1) as followed
from papers
where authors && %(follows)s
  and published_at > now() - make_interval(days => %(days)s)
  and id not in ({SEEN})
order by published_at desc
limit %(n)s
"""


@dataclass
class Feedback:
    saved_titles: list[str]
    saved_texts: list[str]
    saved: np.ndarray
    dismissed: np.ndarray


# Saves and dismisses in one round trip: the database may be a region away. Distinct
# papers, so saving something twice doesn't count twice; the most recent 400 actions.
FEEDBACK = """
select e.kind, p.title, p.abstract, p.embedding
from (
    select distinct on (paper_id, kind) paper_id, kind, created_at
    from events
    where user_id = %s and kind in ('save', 'dismiss')
    order by paper_id, kind, created_at desc
) e
join papers p on p.id = e.paper_id
where p.embedding is not null
order by e.created_at desc
limit 400
"""


def _feedback(conn: Connection, user_id: UUID) -> Feedback:
    rows = conn.execute(FEEDBACK, (user_id,)).fetchall()
    saves = [r for r in rows if r[0] == "save"]
    return Feedback(
        saved_titles=[r[1] for r in saves],
        saved_texts=[lexical.document_text(r[1], r[2]) for r in saves],
        saved=np.array([r[3].to_numpy() for r in saves]),
        dismissed=np.array([r[3].to_numpy() for r in rows if r[0] == "dismiss"]),
    )


def feed(conn: Connection, user_id: UUID, categories: list[str], limit: int) -> list[Candidate]:
    fb = _feedback(conn, user_id)
    profile = profile_vector(fb.saved, fb.dismissed)
    params = {
        "user": user_id,
        "profile": profile,
        "categories": categories,
        "days": WINDOW_DAYS,
        "k": CANDIDATES,
    }
    with conn.cursor(row_factory=class_row(Candidate)) as cur:
        cur.execute(BY_SIMILARITY if profile is not None else BY_CATEGORY, params)
        candidates = cur.fetchall()

    blended = None
    if profile is not None and candidates:
        lex = lexical.scores(
            [lexical.tokenize(t) for t in fb.saved_texts],
            [lexical.tokenize(lexical.document_text(c.title, c.abstract)) for c in candidates],
        )
        blended = relevance(np.array([c.similarity for c in candidates]), lex)
    items = rerank(candidates, set(categories), datetime.now(UTC), relevance=blended)[:limit]

    follows = [
        a for (a,) in conn.execute("select author from follows where user_id = %s", (user_id,))
    ]
    if follows:
        with conn.cursor(row_factory=class_row(Candidate)) as cur:
            cur.execute(FROM_FOLLOWED, {**params, "follows": follows, "n": MAX_FOLLOWED})
            pinned = cur.fetchall()
        shown = {c.id for c in pinned}
        items = (pinned + [c for c in items if c.id not in shown])[:limit]

    if len(fb.saved) and items:
        # Embeddings only for what is shown: 20 vectors, not 200, cross the network.
        vectors = dict(
            conn.execute(
                "select id, embedding from papers where id = any(%s)", ([c.id for c in items],)
            ).fetchall()
        )
        for c in items:
            c.embedding = vectors.get(c.id)
        explain(items, fb.saved_titles, fb.saved)
    return items
