import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from xoult import lexical


def test_tokenize_drops_stopwords_and_punctuation():
    assert lexical.tokenize("The ColBERT-v2 model, and LoRA!") == ["colbert", "v2", "model", "lora"]


def test_matches_sklearn_ranking():
    saved = ["dense retrieval with late interaction colbert", "retrieval augmented generation"]
    candidates = [
        "colbert late interaction compression for retrieval",
        "diffusion models for image generation",
        "a survey of reinforcement learning",
        "retrieval augmented generation benchmarks",
    ]
    ours = lexical.scores(
        [lexical.tokenize(s) for s in saved], [lexical.tokenize(c) for c in candidates]
    )

    vec = TfidfVectorizer(sublinear_tf=True, token_pattern=r"[a-z][a-z0-9]+",
                          stop_words=list(lexical.STOP))  # fmt: skip
    m = vec.fit_transform(saved + candidates)
    centroid = np.asarray(m[: len(saved)].sum(axis=0)).ravel()
    reference = m[len(saved) :] @ centroid
    assert np.allclose(ours, reference)
    assert set(np.argsort(-ours)[:2]) == {0, 3}  # the two retrieval papers beat the others


def test_no_saves_means_no_signal():
    assert lexical.scores([], [["a"], ["b"]]).tolist() == [0.0, 0.0]
