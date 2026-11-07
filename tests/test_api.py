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
        conn.execute(
            "truncate bluesky_posts, folder_papers, folders, follows, events, users, papers"
        )
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


def test_discovery_files(api, browser, monkeypatch):
    page = browser.get("/").text
    assert 'property="og:image" content="http://testserver/static/og.png?v=2"' in page
    assert "<noscript>" in page and "{{origin}}" not in page
    assert browser.get("/static/og.png").headers["content-type"] == "image/png"

    robots = browser.get("/robots.txt").text
    assert "Disallow: /me" in robots and "Sitemap: http://testserver/sitemap.xml" in robots
    sitemap = browser.get("/sitemap.xml").text
    assert "<loc>http://testserver/</loc>" in sitemap
    assert "<loc>http://testserver/papers/cs.IR</loc>" in sitemap
    assert "<loc>http://testserver/privacy</loc>" in sitemap
    privacy = browser.get("/privacy").text
    assert "<h1>Privacy</h1>" in privacy and "{{" not in privacy
    assert 'href="/privacy"' in page  # linked from the landing page
    assert "fonts.googleapis.com" not in page and "fonts.g" not in api.CSP
    assert browser.get("/static/fonts/stix-two-text.woff2").status_code == 200
    assert "Thank you to arXiv" in page and "Thank you to arXiv" in privacy

    # The daily lists are plain HTML: every paper in the category, none from others.
    with get_pool().connection() as conn:
        conn.execute("update papers set tldr = 'A TLDR.' where id = %s", (paper(0),))
    latest = browser.get("/papers/cs.IR")
    assert latest.text.count("AI summary") == 1  # only on the LLM-written summary
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
    # Limits follow the address Cloudflare saw, not a header the client can make up.
    fake = {"x-forwarded-for": "203.0.113.9"}
    assert browser.post("/users", json={"categories": ["cs.IR"]}, headers=fake).status_code == 429
    other = {"cf-connecting-ip": "198.51.100.7"}
    assert browser.post("/users", json={"categories": ["cs.IR"]}, headers=other).status_code == 201


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


def test_add_any_arxiv_paper_to_saved(api, browser, monkeypatch):
    attention = {
        "id": "1706.03762", "title": "Attention Is All You Need", "abstract": "Transformers.",
        "authors": ["Ashish Vaswani"], "categories": ["cs.CL", "cs.LG"],
        "published_at": datetime(2017, 6, 12, tzinfo=UTC), "tldr": None,
    }  # fmt: skip
    calls, wakeups = [], []
    monkeypatch.setattr(api, "request_embedding", lambda: wakeups.append(1))
    monkeypatch.setattr(api.arxiv, "search", lambda q: calls.append(q) or [attention])
    monkeypatch.setattr(
        api.arxiv,
        "get_many",
        lambda ids: calls.extend(ids) or [{**attention, "id": i} for i in ids],
    )

    assert browser.get("/arxiv", params={"q": "attention"}).status_code == 401
    start(browser)
    found = browser.get("/arxiv", params={"q": "attention is all you need"})
    assert found.headers["cache-control"] == "no-store"
    assert [p["title"] for p in found.json()] == ["Attention Is All You Need"]

    r = browser.post("/saved", json={"paper_id": "https://arxiv.org/abs/1706.03762v7"})
    assert r.status_code == 201 and r.json()["id"] == "1706.03762" and r.json()["new"]
    assert [p["id"] for p in browser.get("/saved").json()] == ["1706.03762"]
    assert browser.get("/me").json()["saved"] == 1
    # A 2017 paper never shows up in the feed, and arXiv is asked only once for it.
    assert "1706.03762" not in {i["id"] for i in browser.get("/feed").json()["items"]}
    again = browser.post("/saved", json={"paper_id": "1706.03762"})
    assert again.status_code == 201 and not again.json()["new"]  # no duplicate save
    assert browser.get("/me").json()["saved"] == 1
    assert calls == ["attention is all you need", "1706.03762"]
    # Papers already stored need no arXiv request and are already embedded.
    assert browser.post("/saved", json={"paper_id": paper(5)}).status_code == 201
    assert len(calls) == 2 and len(wakeups) == 1

    assert browser.post("/saved", json={"paper_id": "not an id"}).status_code == 422
    monkeypatch.setattr(api.arxiv, "get_many", lambda ids: [])
    assert browser.post("/saved", json={"paper_id": "2601.99999"}).status_code == 404

    def down(_):
        raise api.arxiv.Unavailable("429")

    monkeypatch.setattr(api.arxiv, "search", down)
    assert browser.get("/arxiv", params={"q": "anything"}).status_code == 503

    # Caps that stop one session filling the database with permanent rows.
    monkeypatch.setattr(api.arxiv, "get_many", lambda ids: [{**attention, "id": i} for i in ids])
    monkeypatch.setattr(api.new_papers_daily_limit, "limit", 1)
    api.new_papers_daily_limit.counts.clear()  # the adds above counted too
    assert browser.post("/saved", json={"paper_id": "2601.00001"}).status_code == 201
    assert browser.post("/saved", json={"paper_id": "2601.00002"}).status_code == 429
    monkeypatch.setattr(api, "MAX_SAVED", 3)  # 1706.03762, paper(5), 2601.00001
    assert browser.post("/saved", json={"paper_id": paper(7)}).status_code == 409


def test_saves_in_a_burst_wake_the_embedder_once(api, monkeypatch):
    dispatched = []
    monkeypatch.setattr(settings, "github_token", "token")
    monkeypatch.setattr(api, "EMBED_DELAY", 0.2)
    monkeypatch.setattr(api, "dispatch_embed", lambda: dispatched.append(1))
    for _ in range(5):
        api.request_embedding()
    api._embed_timer.join()
    assert dispatched == [1]
    api.request_embedding()  # a save after the run started gets a run of its own
    api._embed_timer.join()
    assert dispatched == [1, 1]


def test_import_own_papers_and_follow_authors(api, browser, monkeypatch):
    import_calls = []
    monkeypatch.setattr(api, "request_embedding", lambda: None)
    monkeypatch.setattr(
        api.arxiv,
        "find_authors",
        lambda q: [{"id": "A123", "name": q, "works": 9, "institution": None}],
    )
    monkeypatch.setattr(
        api.arxiv,
        "authored_arxiv_ids",
        lambda **who: import_calls.append(who) or [paper(2), "2601.55555"],
    )
    fetched = []
    monkeypatch.setattr(
        api.arxiv,
        "get_many",
        lambda ids: fetched.extend(ids)
        or [
            {"id": i, "title": "My preprint", "abstract": "Mine.", "authors": ["A. Author", "Me"],
             "categories": ["cs.IR"], "published_at": datetime(2026, 1, 5, tzinfo=UTC),
             "tldr": None}
            for i in ids
        ],
    )  # fmt: skip
    start(browser, categories=("cs.IR",))

    # Import: by name (pick yourself), or by ORCID iD. Stored papers cost no arXiv request.
    assert browser.get("/import/authors", params={"q": "Jane Doe"}).json()[0]["id"] == "A123"
    assert browser.get("/import/papers").status_code == 422
    assert browser.get("/import/papers", params={"author": "Robert'); drop"}).status_code == 422
    r = browser.get("/import/papers", params={"orcid": "https://orcid.org/0000-0002-9322-3515"})
    assert [p["id"] for p in r.json()] == [paper(2), "2601.55555"]
    assert import_calls[-1] == {"orcid": "0000-0002-9322-3515", "author": None, "limit": 20}
    assert fetched == ["2601.55555"]

    # Save them in one go; again is a no-op.
    ids = [p["id"] for p in r.json()]
    saved = browser.post("/saved/batch", json={"paper_ids": ids})
    assert saved.status_code == 201 and all(p["new"] for p in saved.json())
    assert not any(p["new"] for p in browser.post("/saved/batch", json={"paper_ids": ids}).json())
    assert browser.get("/me").json()["saved"] == 2
    assert browser.post("/saved/batch", json={"paper_ids": ["x"] * 21}).status_code == 422

    # "A. Author" wrote both saved papers, so they're suggested; following moves them over.
    suggested = browser.get("/authors").json()["suggested"]
    assert {"name": "A. Author", "saved": 2} in suggested
    assert browser.post("/follows", json={"author": "A. Author"}).status_code == 204
    authors = browser.get("/authors").json()
    assert authors["following"] == ["A. Author"]
    assert "A. Author" not in {a["name"] for a in authors["suggested"]}

    # Their new papers lead the feed (any category, not already saved), at most three,
    # and are measured as their own ranker.
    items = browser.get("/feed").json()["items"]
    assert [i["followed"] for i in items[:3]] == ["A. Author"] * 3
    assert all(i["followed"] is None for i in items[3:])
    assert not {i["id"] for i in items} & set(ids)
    assert "follows" in {row["ranker"] for row in browser.get("/stats").json()}
    assert browser.get("/me/export").json()["follows"][0]["author"] == "A. Author"

    # A hand-added preprint newer than the latest arXiv batch isn't "the announcement".
    newest = datetime.now(UTC) - timedelta(minutes=5)
    with get_pool().connection() as conn:
        conn.execute("update papers set published_at = %s where id = '2601.55555'", (newest,))
    api._latest = (0.0, (None, 0))  # it's cached for five minutes
    assert browser.get("/feed").json()["latest_count"] == 40
    assert sum(c["daily"] for c in browser.get("/config").json()["categories"]) == 40

    assert browser.delete("/follows", params={"author": "A. Author"}).status_code == 204
    assert all(i["followed"] is None for i in browser.get("/feed").json()["items"])
    monkeypatch.setattr(api, "MAX_FOLLOWS", 0)
    assert browser.post("/follows", json={"author": "Me"}).status_code == 409


def test_folders_hold_saved_papers(browser):
    start(browser)
    save(browser, paper(0), paper(2))
    f = browser.post("/folders", json={"name": "Thesis"}).json()
    assert browser.put(f"/folders/{f['id']}/papers/{paper(0)}").status_code == 204
    assert browser.put(f"/folders/{f['id']}/papers/{paper(0)}").status_code == 204  # idempotent
    assert browser.put(f"/folders/{f['id']}/papers/{paper(4)}").status_code == 404  # not saved
    assert browser.get("/folders").json() == [
        {"id": f["id"], "name": "Thesis", "shared": None, "paper_ids": [paper(0)]}
    ]
    browser.delete(f"/events?paper_id={paper(0)}&kind=save")
    assert browser.get("/folders").json()[0]["paper_ids"] == []
    assert "Thesis" in str(browser.get("/me/export").json()["folders"])
    assert browser.delete(f"/folders/{f['id']}").status_code == 204
    assert browser.get("/folders").json() == []
    assert browser.get("/me").json()["saved"] == 1  # deleting a folder keeps saves


def test_folders_are_private_to_their_owner(api, browser):
    start(browser)
    save(browser, paper(0))
    f = browser.post("/folders", json={"name": "Mine"}).json()
    with TestClient(api.app) as other:
        start(other)
        save(other, paper(0))
        assert other.put(f"/folders/{f['id']}/papers/{paper(0)}").status_code == 404
        assert other.delete(f"/folders/{f['id']}").status_code == 404
        assert other.get("/folders").json() == []
    assert len(browser.get("/folders").json()) == 1


def test_folder_name_rules(api, browser, monkeypatch):
    start(browser)
    assert browser.post("/folders", json={"name": "  "}).status_code == 422
    assert browser.post("/folders", json={"name": "x" * 61}).status_code == 422
    assert browser.post("/folders", json={"name": "Thesis"}).status_code == 201
    assert browser.post("/folders", json={"name": "Thesis "}).status_code == 409
    monkeypatch.setattr(api, "MAX_FOLDERS", 2)
    assert browser.post("/folders", json={"name": "Two"}).status_code == 201
    assert browser.post("/folders", json={"name": "Three"}).status_code == 409


def test_share_folder_link_lifecycle(api, browser):
    start(browser)
    save(browser, paper(0))
    f = browser.post("/folders", json={"name": "Week 12"}).json()
    browser.put(f"/folders/{f['id']}/papers/{paper(0)}")
    token = browser.post(f"/folders/{f['id']}/share").json()["share_id"]
    assert browser.post(f"/folders/{f['id']}/share").json()["share_id"] == token
    assert browser.get("/folders").json()[0]["shared"] == token
    with TestClient(api.app) as stranger:  # no session at all
        page = stranger.get(f"/f/{token}")
        assert page.status_code == 200 and "Week 12" in page.text and paper(0) in page.text
        assert '<meta name="robots" content="noindex">' in page.text
        assert page.headers["cache-control"] == "no-store"
        assert browser.delete(f"/folders/{f['id']}/share").status_code == 204
        assert stranger.get(f"/f/{token}").status_code == 404
    assert f"/f/{token}" not in browser.get("/sitemap.xml").text
    daily = browser.get("/papers/cs.IR")
    assert "noindex" not in daily.text and "max-age=3600" in daily.headers["cache-control"]


def test_shared_folder_page_escapes_and_hides_owner(browser):
    owner = start(browser)
    save(browser, paper(0))
    f = browser.post("/folders", json={"name": "<script>x</script> & co"}).json()
    token = browser.post(f"/folders/{f['id']}/share").json()["share_id"]
    text = browser.get(f"/f/{token}").text
    assert "<script>x</script>" not in text and "&lt;script&gt;" in text
    assert owner not in text


def test_shared_folder_page_after_unsave(browser):
    start(browser)
    save(browser, paper(0))
    f = browser.post("/folders", json={"name": "Empty soon"}).json()
    browser.put(f"/folders/{f['id']}/papers/{paper(0)}")
    token = browser.post(f"/folders/{f['id']}/share").json()["share_id"]
    browser.delete(f"/events?paper_id={paper(0)}&kind=save")
    page = browser.get(f"/f/{token}")
    assert page.status_code == 200 and "0 papers" in page.text


def test_sign_in_keeps_folders_and_follows(api, browser, monkeypatch):
    monkeypatch.setattr(
        api, "verify_google_token",
        lambda c: {"sub": "g-777", "email": "f@example.com", "email_verified": True},
    )  # fmt: skip
    # The account already exists, with a "Thesis" folder.
    start(browser)
    save(browser, paper(0))
    thesis = browser.post("/folders", json={"name": "Thesis"}).json()
    browser.put(f"/folders/{thesis['id']}/papers/{paper(0)}")
    browser.post("/auth/google", json={"credential": "x"})

    # Another device browses anonymously, then signs in to that account.
    with TestClient(api.app) as phone:
        start(phone)
        save(phone, paper(2))
        mine = phone.post("/folders", json={"name": "Thesis"}).json()
        phone.put(f"/folders/{mine['id']}/papers/{paper(2)}")
        lab = phone.post("/folders", json={"name": "Lab"}).json()
        phone.put(f"/folders/{lab['id']}/papers/{paper(2)}")
        token = phone.post(f"/folders/{lab['id']}/share").json()["share_id"]
        phone.post("/follows", json={"author": "A. Author"})
        assert phone.post("/auth/google", json={"credential": "x"}).status_code == 200

        folders = {f["name"]: f for f in phone.get("/folders").json()}
        assert set(folders) == {"Thesis", "Lab"}
        assert set(folders["Thesis"]["paper_ids"]) == {paper(0), paper(2)}
        assert phone.get(f"/f/{token}").status_code == 200
        assert "A. Author" in phone.get("/authors").json()["following"]


def test_feed_and_saved_carry_hf_fields(browser):
    with get_pool().connection() as conn:
        conn.execute(
            "update papers set hf_upvotes = 5, code_url = 'https://x.org' where id = %s",
            (paper(0),),
        )
    start(browser, categories=("cs.IR",))
    items = {i["id"]: i for i in browser.get("/feed?limit=50").json()["items"]}
    assert items[paper(0)]["hf_upvotes"] == 5 and items[paper(0)]["code_url"] == "https://x.org"
    assert items[paper(2)]["hf_upvotes"] is None
    save(browser, paper(0))
    (saved,) = browser.get("/saved").json()
    assert saved["hf_upvotes"] == 5 and saved["code_url"] == "https://x.org"
    assert "on Hugging Face" in browser.get("/papers/cs.IR").text


def test_bluesky_due_day_needs_tldrs_and_no_prior_post(browser):
    from xoult import bluesky

    day = datetime.now(UTC).replace(microsecond=0) + timedelta(days=2)  # newest day
    rows = [(f"2699.{i:05d}", f"New {i}", "A.", ["B. Author"], ["cs.HC"], day) for i in range(20)]
    # A reader hand-added a paper with a later date of its own; it isn't a batch.
    rows.append(("2699.99999", "Added", "A.", ["C."], ["cs.LG"], day + timedelta(days=1)))
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "insert into papers (id, title, abstract, authors, categories, published_at)"
                " values (%s, %s, %s, %s, %s, %s)",
                rows,
            )
        conn.execute("update papers set tldr = 'T.' where id like '2699.%%' and id < '2699.00016'")
        assert bluesky.due_day(conn) is None  # 16 of 20 is 80%
        conn.execute("update papers set tldr = 'T.' where id like '2699.%%' and id < '2699.00019'")
        assert bluesky.due_day(conn) == day.date()  # 19 of 20 is 95%
        cands = bluesky.candidates(conn, day.date())
        assert 0 < len(cands) <= 25 and len({c["id"] for c in cands}) == len(cands)
        assert all(c["tldr"] for c in cands) and any(c["category"] == "cs.HC" for c in cands)
        conn.execute("insert into bluesky_posts (day, uri) values (%s, 'at://x')", (day.date(),))
        assert bluesky.due_day(conn) is None
        conn.execute("delete from papers where id like '2699.%%'")


def test_old_style_arxiv_ids_go_in_folders(browser):
    start(browser)
    with get_pool().connection() as conn:
        conn.execute(
            "insert into papers (id, title, abstract, authors, categories, published_at) "
            "values ('hep-th/9901001', 'Old', 'A.', '{X.}', '{hep-th}', '1999-01-01') "
            "on conflict do nothing"
        )
    save(browser, "hep-th/9901001")
    f = browser.post("/folders", json={"name": "Classics"}).json()
    assert browser.put(f"/folders/{f['id']}/papers/hep-th%2F9901001").status_code == 204
    assert browser.get("/folders").json()[-1]["paper_ids"] == ["hep-th/9901001"]
    assert browser.delete(f"/folders/{f['id']}/papers/hep-th%2F9901001").status_code == 204
    assert browser.get("/folders").json()[-1]["paper_ids"] == []
