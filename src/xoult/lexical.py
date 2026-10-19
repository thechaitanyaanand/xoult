"""TF-IDF similarity between a reader's saved papers and candidate papers.

Embeddings capture topic; exact terms (method names, datasets, "ColBERT", "LoRA") are
what TF-IDF captures, and the eval shows the two together beat either alone. This is
dependency-free because the API image has no scikit-learn; the formulas match
scikit-learn's TfidfVectorizer(sublinear_tf=True) so the offline numbers carry over.
IDF comes from the documents being compared, which is all a request has.
"""

import math
import re
from collections import Counter

import numpy as np

TOKEN = re.compile(r"[a-z][a-z0-9]+")
# A standard English stop list. No domain words ("paper", "method"): the eval decides
# what helps, and it was measured without them.
STOP = frozenset(
    """a about above after again against all also am an and any are as at be because been
    before being below between both but by can could did do does doing down during each few
    for from further had has have having he her here hers herself him himself his how i if in
    into is it its itself just me more most my myself no nor not now of off on once only or
    other our ours ourselves out over own same she should so some such than that the their
    theirs them themselves then there these they this those through to too under until up
    very was we were what when where which while who whom why will with you your yours
    yourself yourselves""".split()
)


def document_text(title: str, abstract: str) -> str:
    """The text a paper is represented by, for embeddings and TF-IDF alike."""
    return f"{title}. {abstract}"


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN.findall(text.lower()) if t not in STOP]


def scores(saved: list[list[str]], candidates: list[list[str]]) -> np.ndarray:
    """Similarity of each candidate to the centroid of the saved papers (higher = closer)."""
    if not saved or not candidates:
        return np.zeros(len(candidates))
    docs = [Counter(d) for d in saved + candidates]
    df = Counter(term for d in docs for term in d)
    n = len(docs)
    idf = {t: math.log((1 + n) / (1 + c)) + 1 for t, c in df.items()}  # smoothed, as sklearn

    def unit(doc: Counter) -> dict[str, float]:
        v = {t: (1 + math.log(c)) * idf[t] for t, c in doc.items()}  # sublinear tf
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    vectors = [unit(d) for d in docs]
    centroid: dict[str, float] = {}
    for v in vectors[: len(saved)]:
        for t, x in v.items():
            centroid[t] = centroid.get(t, 0.0) + x
    # The centroid's length is the same for every candidate, so the dot product ranks
    # exactly like cosine similarity.
    return np.array(
        [sum(centroid.get(t, 0.0) * x for t, x in v.items()) for v in vectors[len(saved) :]]
    )
