import math

from evals.metrics import ndcg_at_k, recall_at_k


def test_ndcg_perfect_and_empty():
    assert ndcg_at_k(["a", "b", "x"], {"a", "b"}) == 1.0
    assert ndcg_at_k(["x", "y"], {"a"}) == 0.0
    assert ndcg_at_k(["a"], set()) == 0.0


def test_ndcg_rewards_position():
    # one relevant item at rank 2: dcg = 1/log2(3), ideal = 1
    assert math.isclose(ndcg_at_k(["x", "a"], {"a"}), 1 / math.log2(3))
    assert ndcg_at_k(["a", "x"], {"a"}) > ndcg_at_k(["x", "a"], {"a"})


def test_ndcg_ignores_hits_past_k():
    assert ndcg_at_k(["x"] * 10 + ["a"], {"a"}, k=10) == 0.0


def test_recall():
    assert recall_at_k(["a", "x", "b"], {"a", "b", "c", "d"}, k=2) == 0.25
    assert recall_at_k([], set()) == 0.0
