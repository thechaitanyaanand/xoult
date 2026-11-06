import json
from datetime import date

from xoult import bluesky

CANDS = [
    {"id": f"2611.0000{i}", "title": f"Paper {i}", "tldr": f"Does thing {i}.",
     "category": "cs.HC" if i % 2 else "cs.LG", "upvotes": 10 - i}
    for i in range(5)
]  # fmt: skip
IDS = {c["id"] for c in CANDS}
GOOD_JSON = json.dumps(
    {
        "root": "Five fresh papers today 👀",
        "picks": [{"id": c["id"], "text": f"Neat: {c['title']}"} for c in CANDS[:3]],
    }
)


def test_validate_rejects_unknown_ids_and_long_text():
    ids = {"a", "b", "c"}
    picks = [{"id": i, "text": "x"} for i in "abc"]
    ok = json.dumps({"root": "Hi", "picks": picks})
    assert bluesky.validate(ok, ids) is not None
    assert bluesky.validate(ok, {"a", "b"}) is None  # an invented paper
    assert bluesky.validate(json.dumps({"root": "x" * 281, "picks": picks}), ids) is None
    assert bluesky.validate(json.dumps({"root": "Hi", "picks": picks[:2]}), ids) is None
    same = json.dumps({"root": "Hi", "picks": [picks[0]] * 3})
    assert bluesky.validate(same, ids) is None  # one paper three times
    tags = json.dumps({"root": "Hi #a #b", "picks": picks})
    assert bluesky.validate(tags, ids) is None  # two hashtags
    assert bluesky.validate("not json", ids) is None
    assert bluesky.validate("```json\n" + ok + "\n```", ids) is not None  # fenced


def test_build_post_counts_graphemes_and_attaches_card():
    assert bluesky.graphemes("café — 👩‍🔬") == 8
    assert bluesky.graphemes("café") == 4  # e + combining accent is one
    rec = bluesky.build_post(
        "Résumé of the day", "https://arxiv.org/abs/2611.00001", "T", "D", None
    )
    assert rec["$type"] == "app.bsky.feed.post" and rec["text"] == "Résumé of the day"
    assert rec["embed"]["$type"] == "app.bsky.embed.external"
    assert rec["embed"]["external"]["uri"] == "https://arxiv.org/abs/2611.00001"
    assert "reply" not in rec
    parent = {"uri": "at://x/app.bsky.feed.post/1", "cid": "c1"}
    reply = bluesky.build_post("Next", "https://a.b", "T", "D", {"root": parent, "parent": parent})
    assert reply["reply"]["root"] == parent


def test_write_thread_falls_back_after_two_bad_answers(monkeypatch):
    calls = []
    monkeypatch.setattr(bluesky.llm, "complete", lambda *a, **k: calls.append(1) or "nonsense")
    t = bluesky.write_thread(CANDS, date(2026, 11, 6), 691)
    assert len(calls) == 2
    assert "691 new arXiv papers" in t["root"]
    assert [p["id"] for p in t["picks"]] == [c["id"] for c in CANDS[:3]]


def test_write_thread_uses_gemini_first(monkeypatch):
    seen = {}

    def fake(messages, name, models=None, **kwargs):
        seen.update(name=name, models=models, user=messages[-1]["content"])
        return GOOD_JSON

    monkeypatch.setattr(bluesky.llm, "complete", fake)
    t = bluesky.write_thread(CANDS, date(2026, 11, 6), 691)
    assert seen["name"] == "bsky-v1" and seen["models"][0] == "gemini/gemini-3.5-flash"
    assert "2611.00001 | cs.HC" in seen["user"]
    assert t == json.loads(GOOD_JSON)


def test_post_daily_skips_without_credentials(monkeypatch):
    monkeypatch.setattr(bluesky.settings, "bluesky_handle", None)
    assert bluesky.post_daily() is None
