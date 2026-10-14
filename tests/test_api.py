"""End-to-end API flow against a real Postgres + pgvector.

Runs only when TEST_DATABASE_URL is set, because it truncates every table. CI points it
at a throwaway service container; locally use a separate database, e.g.
`docker compose exec db createdb -U xoult xoult_test`.
"""

import os
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient

from xoult.config import settings
from xoult.db import apply_schema, get_pool

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="needs TEST_DATABASE_URL (it truncates tables)")

NOBODY = "00000000-0000-0000-0000-000000000000"


def retrieval(i: int) -> str:
    return f"2610.{i:05d}"


@pytest.fixture(scope="module")
def client():
    settings.database_url = URL
    get_pool.cache_clear()
    apply_schema()

    # Two clean topics: even papers point along axis 0 (retrieval, cs.IR), odd ones
    # along axis 1 (vision, cs.LG), with a little noise so they aren't identical.
    rng = np.random.default_rng(0)
    published = datetime.now(UTC) - timedelta(hours=2)
    rows = []
    for i in range(40):
        v = rng.normal(0, 0.05, 384)
        v[i % 2] += 1
        topic, category = ("Retrieval", "cs.IR") if i % 2 == 0 else ("Vision", "cs.LG")
        rows.append(
            (retrieval(i), f"{topic} paper {i}", "An abstract.", ["A. Author"], [category],
             published, v / np.linalg.norm(v))
        )  # fmt: skip
    with get_pool().connection() as conn:
        conn.execute("truncate events, users, papers")
        with conn.cursor() as cur:
            cur.executemany(
                "insert into papers (id, title, abstract, authors, categories, published_at,"
                " embedding) values (%s, %s, %s, %s, %s, %s, %s)",
                rows,
            )

    from xoult.api import app

    with TestClient(app) as c:
        yield c


def test_rejects_bad_input(client):
    assert client.post("/users", json={"categories": []}).status_code == 422
    assert client.post("/users", json={"categories": ["drop table"]}).status_code == 422
    assert client.get("/feed", params={"user_id": NOBODY}).status_code == 404
    assert client.get("/saved", params={"user_id": NOBODY}).status_code == 404
    impression = {"user_id": NOBODY, "paper_id": "x", "kind": "impression"}
    assert client.post("/events", json=impression).status_code == 422


def test_feed_learns_from_feedback(client):
    assert client.get("/health").json() == {"status": "ok"}
    uid = client.post("/users", json={"categories": ["cs.IR", "cs.LG"]}).json()["id"]
    unknown = {"user_id": uid, "paper_id": "9999.99999", "kind": "save"}
    assert client.post("/events", json=unknown).status_code == 404

    cold = client.get("/feed", params={"user_id": uid}).json()
    assert len(cold["items"]) == 20
    assert cold["latest_count"] == 40
    assert all(item["because"] is None for item in cold["items"])

    saves = [retrieval(0), retrieval(2), retrieval(4)]
    for paper in saves:
        event = {"user_id": uid, "paper_id": paper, "kind": "save"}
        assert client.post("/events", json=event).status_code == 204
    for paper in [retrieval(1), retrieval(3)]:
        client.post("/events", json={"user_id": uid, "paper_id": paper, "kind": "dismiss"})

    warm = client.get("/feed", params={"user_id": uid, "limit": 10}).json()["items"]
    ids = [item["id"] for item in warm]
    assert all(item["title"].startswith("Retrieval") for item in warm)
    assert not set(ids) & {*saves, retrieval(1), retrieval(3)}
    saved_titles = {"Retrieval paper 0", "Retrieval paper 2", "Retrieval paper 4"}
    assert all(item["because"] in saved_titles for item in warm)

    saved = client.get("/saved", params={"user_id": uid}).json()
    assert [p["id"] for p in saved] == saves[::-1]  # most recently saved first

    undo = {"user_id": uid, "paper_id": retrieval(4), "kind": "save"}
    assert client.delete("/events", params=undo).status_code == 204
    assert client.delete("/events", params=undo).status_code == 204  # idempotent
    assert [p["id"] for p in client.get("/saved", params={"user_id": uid}).json()] == saves[1::-1]

    [row] = client.get("/stats").json()
    assert row["ranker"] == "v1-linear"
    assert row["impressions"] == 30
    assert row["save_rate"] == round(2 / 30, 4)
