"""End-to-end API flows against a real Postgres + pgvector.

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


def paper(i: int) -> str:
    return f"2610.{i:05d}"


@pytest.fixture(scope="module")
def api():
    settings.database_url = URL
    settings.google_client_id = "test-client"
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
            (paper(i), f"{topic} paper {i}", "An abstract.", ["A. Author"], [category],
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

    from xoult import api

    return api


@pytest.fixture
def browser(api):
    """A fresh browser: its own cookie jar, rate limits reset."""
    for limit in api.LIMITS:
        limit.counts.clear()
    with TestClient(api.app) as client:
        yield client


def start(client, categories=("cs.IR", "cs.LG")) -> str:
    r = client.post("/users", json={"categories": list(categories)})
    assert r.status_code == 201
    return r.json()["id"]


def save(client, *ids, kind="save"):
    for i in ids:
        assert client.post("/events", json={"paper_id": i, "kind": kind}).status_code == 204


def test_no_session_and_bad_input(browser):
    for path in ("/feed", "/saved", "/me", "/me/export"):
        assert browser.get(path).status_code == 401, path
    assert browser.post("/events", json={"paper_id": paper(0), "kind": "save"}).status_code == 401
    assert browser.post("/users", json={"categories": []}).status_code == 422
    assert browser.post("/users", json={"categories": ["drop table"]}).status_code == 422
    start(browser)
    assert browser.post("/events", json={"paper_id": "x", "kind": "impression"}).status_code == 422
    assert browser.post("/events", json={"paper_id": "9999.9", "kind": "save"}).status_code == 404


def test_security_headers_and_cookie(browser):
    r = browser.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "cache-control" not in r.headers  # the public page may be cached
    start(browser)
    assert browser.get("/feed").headers["cache-control"] == "no-store"
    assert browser.get("/me").headers["cache-control"] == "no-store"
    cookie = browser.post("/users", json={"categories": ["cs.IR"]}).headers["set-cookie"]
    assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()


def test_discovery_files(browser, monkeypatch):
    page = browser.get("/").text
    assert 'property="og:image" content="http://testserver/static/og.png"' in page
    assert "<noscript>" in page and "{{origin}}" not in page
    assert browser.get("/static/og.png").headers["content-type"] == "image/png"

    robots = browser.get("/robots.txt").text
    assert "Disallow: /me" in robots and "Sitemap: http://testserver/sitemap.xml" in robots
    sitemap = browser.get("/sitemap.xml").text
    assert "<loc>http://testserver/</loc>" in sitemap
    assert "<loc>http://testserver/papers/cs.IR</loc>" in sitemap

    # The daily lists are plain HTML: every paper in the category, none from others.
    latest = browser.get("/papers/cs.IR")
    assert latest.status_code == 200 and "public" in latest.headers["cache-control"]
    assert "Retrieval paper 0" in latest.text and "Vision paper 1" not in latest.text
    assert 'rel="canonical" href="http://testserver/papers/cs.IR"' in latest.text
    day = sitemap.split("/papers/cs.IR/")[1][:10]
    assert "Retrieval paper 2" in browser.get(f"/papers/cs.IR/{day}").text
    assert browser.get("/papers/cs.IR/2001-01-01").status_code == 404
    assert browser.get("/papers/math.AG").status_code == 404
    assert browser.get("/papers/cs.IR/not-a-date").status_code == 422
    llms = browser.get("/llms.txt").text
    assert llms.startswith("# xoult") and "cs.CV (Computer Vision)" in llms

    # A configured public URL wins over whatever Host header a request arrives with.
    monkeypatch.setattr(settings, "public_url", "https://xoult.example")
    assert "Sitemap: https://xoult.example/sitemap.xml" in browser.get("/robots.txt").text


def test_config_lists_categories_with_volume(browser):
    config = browser.get("/config").json()
    assert config["google_client_id"] == "test-client"
    by_id = {c["id"]: c for c in config["categories"]}
    assert by_id["cs.IR"] == {"id": "cs.IR", "name": "Information Retrieval", "daily": 20}


def test_feed_learns_from_feedback(browser):
    start(browser)
    cold = browser.get("/feed").json()
    assert len(cold["items"]) == 20
    assert cold["latest_count"] == 40
    assert all(item["because"] is None for item in cold["items"])

    saves = [paper(0), paper(2), paper(4)]
    save(browser, *saves)
    save(browser, paper(1), paper(3), kind="dismiss")

    warm = browser.get("/feed", params={"limit": 10}).json()["items"]
    assert all(item["title"].startswith("Retrieval") for item in warm)
    assert not {item["id"] for item in warm} & {*saves, paper(1), paper(3)}
    assert {item["because"] for item in warm} <= {"Retrieval paper 0", "Retrieval paper 2",
                                                  "Retrieval paper 4"}  # fmt: skip

    assert [p["id"] for p in browser.get("/saved").json()] == saves[::-1]
    undo = {"paper_id": paper(4), "kind": "save"}
    assert browser.delete("/events", params=undo).status_code == 204
    assert browser.delete("/events", params=undo).status_code == 204  # idempotent
    assert [p["id"] for p in browser.get("/saved").json()] == saves[1::-1]
    assert browser.get("/me").json()["saved"] == 2


def test_change_categories_export_and_delete(browser):
    uid = start(browser, ["cs.IR"])
    save(browser, paper(0))  # a retrieval save, so the feed is personalized from here on
    assert browser.patch("/me", json={"categories": ["cs.LG"]}).status_code == 204
    assert browser.get("/me").json()["categories"] == ["cs.LG"]
    # Unfollowing a category removes it even though the saved paper points at it.
    items = browser.get("/feed").json()["items"]
    assert items and all(p["categories"] == ["cs.LG"] for p in items)

    save(browser, paper(1))
    export = browser.get("/me/export")
    assert "attachment" in export.headers["content-disposition"]
    data = export.json()
    assert data["user"]["id"] == uid
    assert {"kind": "save", "paper_id": paper(1)}.items() <= data["events"][-1].items()

    assert browser.delete("/me").status_code == 204
    assert browser.get("/me").status_code == 401
    with get_pool().connection() as conn:
        assert (
            conn.execute("select count(*) from events where user_id = %s", (uid,)).fetchone()[0]
            == 0
        )


def test_google_sign_in_links_devices(api, browser, monkeypatch):
    accounts = {
        "good-token": {"sub": "g-123", "email": "a@example.com", "email_verified": True},
        "unverified": {"sub": "g-9", "email": "b@example.com", "email_verified": False},
    }

    def verify(credential):
        if credential not in accounts:
            raise ValueError("bad signature")
        return accounts[credential]

    monkeypatch.setattr(api, "verify_google_token", verify)
    sign_in = lambda c, token: c.post("/auth/google", json={"credential": token})  # noqa: E731

    assert sign_in(browser, "forged").status_code == 401
    assert sign_in(browser, "unverified").status_code == 401

    # Laptop: browse anonymously, save a paper, then sign in. The saves stay.
    laptop = browser
    first = start(laptop)
    save(laptop, paper(0))
    assert sign_in(laptop, "good-token").json()["id"] == first
    me = laptop.get("/me").json()
    assert me["signed_in"] and me["email"] == "a@example.com" and me["saved"] == 1

    # Phone: a separate anonymous session saves another paper, then signs in to the
    # same Google account. Both saves end up on one account.
    with TestClient(api.app) as phone:
        second = start(phone)
        save(phone, paper(2))
        assert sign_in(phone, "good-token").json()["id"] == first
        assert {p["id"] for p in phone.get("/saved").json()} == {paper(0), paper(2)}
    with get_pool().connection() as conn:
        assert (
            conn.execute("select count(*) from users where id = %s", (second,)).fetchone()[0] == 0
        )

    # Signing out ends the session; signing in again restores it.
    assert laptop.post("/auth/logout").status_code == 204
    assert laptop.get("/me").status_code == 401
    assert sign_in(laptop, "good-token").json()["id"] == first

    # A brand-new Google account with no anonymous history starts with no categories,
    # which the page treats as "finish setup".
    accounts["new"] = {"sub": "g-new", "email": "c@example.com", "email_verified": True}
    with TestClient(api.app) as fresh:
        sign_in(fresh, "new")
        assert fresh.get("/me").json()["categories"] == []


def test_rate_limits(browser):
    for _ in range(5):
        assert browser.post("/users", json={"categories": ["cs.IR"]}).status_code == 201
    blocked = browser.post("/users", json={"categories": ["cs.IR"]})
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "60"


def test_global_signup_cap_stops_distributed_bots(api, browser, monkeypatch):
    # Pretend the hourly allowance is nearly spent by bots on other IPs.
    monkeypatch.setattr(api.signup_global_limit, "limit", 2)
    for _ in range(2):
        assert browser.post("/users", json={"categories": ["cs.IR"]}).status_code == 201
    api.signup_limit.counts.clear()  # a "new IP": the per-IP limit alone would allow it
    blocked = browser.post("/users", json={"categories": ["cs.IR"]})
    assert blocked.status_code == 429 and blocked.headers["retry-after"] == "3600"


def test_prune_keeps_what_people_use(browser):
    from xoult.ingest import prune

    keeper, idle = start(browser), None
    old = datetime.now(UTC) - timedelta(days=60)
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "insert into papers (id, title, abstract, categories, published_at)"
                " values (%s, 'Old', 'An abstract.', '{cs.IR}', %s)",
                [("old.saved", old), ("old.ignored", old)],
            )
        conn.execute(
            "insert into events (user_id, paper_id, kind) values (%s, 'old.saved', 'save')",
            (keeper,),
        )
        conn.execute(
            "insert into events (user_id, paper_id, kind, created_at)"
            " values (%s, %s, 'impression', %s)",
            (keeper, paper(0), old - timedelta(days=60)),
        )
        (idle,) = conn.execute(
            "insert into users (categories, last_seen_at) values ('{cs.IR}', %s) returning id",
            (old,),
        ).fetchone()

    prune()

    with get_pool().connection() as conn:
        ids = {r[0] for r in conn.execute("select id from papers where id like 'old.%'")}
        users = {r[0] for r in conn.execute("select id from users")}
        old_impressions = conn.execute(
            "select count(*) from events where kind = 'impression' and created_at < %s", (old,)
        ).fetchone()[0]
    assert ids == {"old.saved"}  # saved papers outlive the retention window
    assert str(idle) not in {str(u) for u in users} and keeper in {str(u) for u in users}
    assert old_impressions == 0


def test_stats_counts_each_shown_paper_once(browser):
    with get_pool().connection() as conn:
        conn.execute("delete from events")
    start(browser)
    shown = [p["id"] for p in browser.get("/feed", params={"limit": 5}).json()["items"]]
    browser.get("/feed", params={"limit": 5})  # a reload shows the same papers again
    save(browser, shown[0])
    [row] = browser.get("/stats").json()
    assert row["ranker"] == "v2-hybrid"
    assert row["impressions"] == 5  # not 10: the reload is not five more chances
    assert row["save_rate"] == 0.2
