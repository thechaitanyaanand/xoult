import math


def ndcg_at_k(ranked: list, relevant: set, k: int = 10) -> float:
    """Binary-relevance NDCG: hits near the top count more, normalized by the best possible."""
    dcg = sum(1 / math.log2(i + 2) for i, item in enumerate(ranked[:k]) if item in relevant)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal if ideal else 0.0


def recall_at_k(ranked: list, relevant: set, k: int = 50) -> float:
    return len(relevant.intersection(ranked[:k])) / len(relevant) if relevant else 0.0
