from datetime import UTC, datetime, timedelta

import numpy as np

from xoult.rank import Candidate, Weights, profile_vector, rerank

NOW = datetime(2026, 10, 14, tzinfo=UTC)


def paper(id: str, similarity: float, age_days: float = 0, categories=("cs.IR",)) -> Candidate:
    return Candidate(
        id=id,
        title=id,
        tldr=None,
        abstract="",
        authors=[],
        categories=list(categories),
        published_at=NOW - timedelta(days=age_days),
        similarity=similarity,
    )


def test_profile_vector_moves_away_from_dismissed():
    saved = np.array([[1.0, 0.0], [1.0, 0.2]])
    dismissed = np.array([[0.0, 1.0]])
    v = profile_vector(saved, dismissed)
    assert np.isclose(np.linalg.norm(v), 1.0)
    assert v[1] < profile_vector(saved, np.empty((0, 2)))[1]


def test_profile_vector_cold_start():
    assert profile_vector(np.empty((0, 2)), np.empty((0, 2))) is None


def test_rerank_relevance_beats_small_recency_gap():
    ranked = rerank(
        [paper("fresh", 0.80, age_days=0), paper("relevant", 0.86, age_days=1)], {"cs.IR"}, NOW
    )
    assert [c.id for c in ranked] == ["relevant", "fresh"]


def test_rerank_recency_breaks_ties():
    ranked = rerank([paper("old", 0.8, age_days=6), paper("new", 0.8, age_days=0)], {"cs.IR"}, NOW)
    assert [c.id for c in ranked] == ["new", "old"]


def test_rerank_category_match():
    w = Weights(similarity=0, recency=0, category=1)
    ranked = rerank(
        [paper("other", 0.8, categories=["math.CO"]), paper("mine", 0.8)], {"cs.IR"}, NOW, w
    )
    assert ranked[0].id == "mine"


def test_rerank_empty():
    assert rerank([], set(), NOW) == []
